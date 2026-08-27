"""PDF invoice -> CanonicalInvoice, the one place a language model is trusted.

The model is asked to do exactly one thing: transcribe what is printed. It does
not compute totals, it does not decide whether a charge is correct, and it never
supplies provenance. Everything a machine can derive exactly is derived in
Python, per the project's central rule: deterministic where correctness matters,
LLM where judgement matters. Transcription is the only step where judgement is
unavoidable, because the page layout is not known in advance.

The repair loop is deliberately plain control flow rather than an ADK LoopAgent.
Re-prompting on a schema violation is a retry, not reasoning, and dressing a
`for` loop up as agency would misrepresent the architecture. Keeping it a pure
function also means the Day-4 eval harness can call it without ADK session
plumbing, and the tests can drive it with a fake client and no tokens.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib
import time

from google.genai import Client, errors, types
from pydantic import ValidationError

from . import config
from .schema import (
    CanonicalInvoice,
    ExtractedInvoice,
    ExtractionProfile,
    ExtractionProvenance,
    content_hash,
)

PDF_MIME_TYPE = "application/pdf"


class ExtractionFailed(RuntimeError):
    """The model never produced output matching the schema.

    Carries every rejection so the caller can escalate a real diagnosis to a
    human instead of an opaque failure.
    """

    def __init__(self, source_uri: str, attempts: int, repair_notes: list[str]) -> None:
        last = repair_notes[-1] if repair_notes else "unknown"
        super().__init__(
            f"could not extract {source_uri} after {attempts} attempt(s); "
            f"last rejection: {last}"
        )
        self.source_uri = source_uri
        self.attempts = attempts
        self.repair_notes = repair_notes


# --- Where a PDF comes from --------------------------------------------------


@dataclasses.dataclass(frozen=True)
class InvoiceSource:
    """One PDF, addressable either on disk or in Cloud Storage.

    Both forms are needed, for different reasons. Production reads from GCS, so
    the URI recorded in provenance points at something durable. Development and
    the eval suite read the 15 committed fixtures straight off disk: requiring a
    bucket round-trip to iterate would make measuring extraction accuracy
    expensive enough that it would not get measured.

    The bytes are always fetched, GCS included, because content_hash is a
    SHA-256 of them and the whole idempotency story rests on that hash.
    """

    uri: str
    _data: bytes | None = None

    @classmethod
    def from_path(cls, path: str | pathlib.Path) -> "InvoiceSource":
        resolved = pathlib.Path(path).resolve()
        return cls(uri=resolved.as_uri(), _data=resolved.read_bytes())

    @classmethod
    def from_gcs(cls, gs_uri: str) -> "InvoiceSource":
        if not gs_uri.startswith("gs://"):
            raise ValueError(f"not a Cloud Storage URI: {gs_uri!r}")
        return cls(uri=gs_uri)

    @classmethod
    def resolve(cls, location: str | pathlib.Path) -> "InvoiceSource":
        """Accept whichever form the caller happens to hold."""
        text = str(location)
        return cls.from_gcs(text) if text.startswith("gs://") else cls.from_path(text)

    @property
    def is_gcs(self) -> bool:
        return self.uri.startswith("gs://")

    def read(self) -> bytes:
        if self._data is not None:
            return self._data
        from google.cloud import storage  # lazy: a local run needs no GCS client

        bucket_name, _, blob_name = self.uri[len("gs://") :].partition("/")
        if not blob_name:
            raise ValueError(f"Cloud Storage URI names no object: {self.uri!r}")
        client = storage.Client(project=config.PROJECT_ID)
        return client.bucket(bucket_name).blob(blob_name).download_as_bytes()

    def as_part(self, pdf_bytes: bytes) -> types.Part:
        """Hand the PDF to Gemini.

        A gs:// URI is passed by reference so the bytes do not travel through
        the request a second time; a local file has to be inlined.
        """
        if self.is_gcs:
            return types.Part.from_uri(file_uri=self.uri, mime_type=PDF_MIME_TYPE)
        return types.Part.from_bytes(data=pdf_bytes, mime_type=PDF_MIME_TYPE)


# --- Prompting ---------------------------------------------------------------

SYSTEM_INSTRUCTION = """\
You transcribe telecom invoices. You do not audit them.

Rules, in order of importance:
1. Report only what is printed on the page. If a value is not printed, omit the
   optional field rather than inferring it. Never invent a line, a charge or an
   amount.
2. Do not compute anything. Do not sum charges, do not recompute a total, do not
   derive overage from consumption. Copy the printed total even when the printed
   lines do not add up to it - a disagreement is a finding, and erasing it hides
   the very error this system exists to catch.
3. Normalise every number to a plain decimal string using a dot separator, with
   no currency symbol and no thousands separator: "1.234,56" and "1,234.56" both
   become "1234.56".
4. Normalise every date to YYYY-MM-DD.
5. Convert data volumes to megabytes. 1 GB is 1024 MB.
6. A charge tied to a service line covers the invoice billing period unless the
   page states another one for that charge: set period to the billing period as
   YYYY-MM. Account-level charges - taxes, fees, surcharges - get a null period,
   because they are levied on the invoice rather than on a period of service.
7. line_id identifies a service line. Charges belonging to the account rather
   than to one line - taxes, fees, account-level discounts - have a null
   line_id. Every non-null line_id must appear in service_lines.
8. Discounts and credits are negative amounts.
9. Set unit_amount only where the page prints a per-unit price in its own column
   for that charge. The taxes-and-fees section prints a description and a total
   and nothing else, so those charges have no unit_amount and no quantity -
   deriving one by dividing is computing, which rule 2 forbids.

Return JSON matching the provided schema and nothing else."""


def build_prompt(profile: ExtractionProfile) -> str:
    """Fold the carrier quirks into the instruction.

    Kept separate from the system instruction so the generic rules stay
    identical for every carrier and only the profile varies. That is what makes
    an extraction failure attributable to a profile rather than to the prompt as
    a whole.
    """
    parts = [
        f"Carrier: {profile.carrier_name} ({profile.country}). "
        f"Amounts are in {profile.currency}."
    ]
    if profile.prompt_hints:
        parts.append("Layout notes for this carrier:")
        parts.extend(f"- {hint}" for hint in profile.prompt_hints)
    if profile.tax_labels:
        labels = ", ".join(profile.tax_labels)
        parts.append(f"Lines labelled {labels} are taxes: category tax, null line_id.")
    if profile.fee_labels:
        labels = ", ".join(profile.fee_labels)
        parts.append(
            f"Lines labelled {labels} are fees, not taxes: category fee, null line_id."
        )
    parts.append("Transcribe this invoice.")
    return "\n".join(parts)


def repair_prompt(error: ValidationError) -> str:
    """Turn a rejection into an instruction.

    The validators in schema.py were written to be read by a model, not only by
    a developer: they name the field, the offending value and the fix. So the
    error text is forwarded rather than summarised.
    """
    complaints = "\n".join(
        "- {}: {}".format(".".join(str(part) for part in err["loc"]), err["msg"])
        for err in error.errors()
    )
    return (
        "Your previous response did not match the schema. Fix exactly these "
        "problems and return the corrected JSON for the same invoice:\n"
        f"{complaints}\n"
        "Change nothing else. Do not re-read values that were already correct."
    )


# --- Extraction --------------------------------------------------------------


def _is_transient(error: Exception) -> bool:
    """Whether a failed call is worth repeating unchanged.

    Server errors and rate limits are the API having a bad moment; a 400 means
    the request itself is wrong and repeating it just wastes the budget.
    """
    if isinstance(error, errors.ServerError):
        return True
    return isinstance(error, errors.ClientError) and getattr(error, "code", None) == 429


def _call_model(
    client: Client,
    model_id: str,
    contents: list[types.Content],
    generate_config: types.GenerateContentConfig,
    *,
    retries: int,
    backoff: float,
) -> str:
    """One model call, repeated only when the failure was the API's fault.

    Observed in practice: the same gs:// extraction returned 500 INTERNAL once
    and succeeded unchanged on the next call. Without this, a Pub/Sub delivery
    would land in the dead-letter queue over a blip.
    """
    attempt = 0
    while True:
        try:
            response = client.models.generate_content(
                model=model_id, contents=contents, config=generate_config
            )
            return response.text or ""
        except Exception as error:  # noqa: BLE001 - re-raised unless transient
            attempt += 1
            if attempt > retries or not _is_transient(error):
                raise
            time.sleep(backoff * (2 ** (attempt - 1)))


def _default_client() -> Client:
    """Vertex AI client on the global endpoint.

    The endpoint is not the infra region: Gemini 3.x is served from `global`
    while Cloud Run, Firestore and GCS live in us-central1. See config.py.
    """
    return Client(
        vertexai=True,
        project=config.PROJECT_ID,
        location=config.MODEL_LOCATION,
    )


def extract_invoice(
    source: InvoiceSource,
    profile: ExtractionProfile,
    *,
    client: Client | None = None,
    model_id: str | None = None,
    max_repairs: int | None = None,
    max_transient_retries: int | None = None,
) -> CanonicalInvoice:
    """Transcribe one invoice PDF into a validated canonical record.

    Raises ExtractionFailed if the model cannot produce schema-valid output
    within the repair budget. Soft inconsistencies never raise: they are
    recorded in provenance.warnings and carried forward, because a carrier that
    rounds its own total must not be able to spin the extractor.
    """
    client = client or _default_client()
    model_id = model_id or config.MODEL_ID
    budget = config.MAX_EXTRACTION_REPAIRS if max_repairs is None else max_repairs
    retries = (
        config.MAX_TRANSIENT_RETRIES
        if max_transient_retries is None
        else max_transient_retries
    )

    pdf_bytes = source.read()
    digest = content_hash(pdf_bytes)

    generate_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=config.EXTRACTION_TEMPERATURE,
        response_mime_type="application/json",
        response_schema=ExtractedInvoice,
        # The extractor has no tools by design. Saying so removes the SDK's
        # automatic-function-calling advisory from the logs, which otherwise
        # appears on every extraction and reads as an error during a demo.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[
                source.as_part(pdf_bytes),
                types.Part.from_text(text=build_prompt(profile)),
            ],
        )
    ]

    repair_notes: list[str] = []
    attempts = 0

    while True:
        attempts += 1
        payload = _call_model(
            client,
            model_id,
            contents,
            generate_config,
            retries=retries,
            backoff=config.TRANSIENT_RETRY_BACKOFF,
        )

        try:
            invoice = ExtractedInvoice.model_validate_json(payload)
        except ValidationError as error:
            repair_notes.append(str(error))
            if len(repair_notes) > budget:
                raise ExtractionFailed(source.uri, attempts, repair_notes) from error
            # Keep the rejected answer in the conversation: the model has to see
            # what it said in order to correct it rather than start over.
            contents.append(
                types.Content(role="model", parts=[types.Part.from_text(text=payload)])
            )
            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=repair_prompt(error))],
                )
            )
            continue

        break

    return CanonicalInvoice(
        # Every field here is computed, never generated. A hash, a timestamp and
        # a model ID are facts about this run, not facts on the page.
        provenance=ExtractionProvenance(
            content_hash=digest,
            source_uri=source.uri,
            profile_key=profile.profile_key,
            model_id=model_id,
            extracted_at=datetime.datetime.now(datetime.timezone.utc),
            attempts=attempts,
            repair_notes=repair_notes,
            warnings=invoice.consistency_warnings(),
        ),
        invoice=invoice,
    )
