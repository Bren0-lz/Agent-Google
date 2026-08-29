"""Signed contract PDF -> Contract, so an account can be audited at all.

Every rule compares the invoice against contracted terms, and `AuditContext`
takes a `Contract` as a required field. Until now the only way to get one was
`scripts/seed_firestore.py`, which knows the four synthetic accounts and nothing
else — so a person arriving with their own invoice had no path to an audit.
This module is that path: the customer sends the contract they signed, Gemini
transcribes it, and `store.save_contract` files it under the account id.

The same discipline as the invoice extractor applies, for the same reason:

  * temperature 0 — this is transcription, not composition;
  * the repair loop from `extractor.generate_validated`, shared rather than
    copied, so the two documents are read with identical machinery;
  * the model transcribes and nothing else.

Worth being explicit about the money, because this is the module where the
project's central claim looks most at risk: a rate transcribed from a contract
is *not* a computed amount, and it never becomes one. It is an input the rule
engine compares against, in `Decimal`, in pure Python. `rate_drift` subtracts
the contracted rate from the billed rate itself; the difference it disputes was
computed in `rules/conformance.py`, never quoted from a model. A contract
misread produces a finding with wrong evidence, which is why extraction
warnings escalate — but it cannot produce a monetary figure the engine did not
derive.
"""

from __future__ import annotations

from google.genai import Client, types

from . import config
from .contract import Contract
from .extractor import (
    BudgetExhausted,
    InvoiceSource,
    _default_client,
    generate_validated,
)
from .schema import ExtractionProfile


class ContractExtractionFailed(RuntimeError):
    """The model never produced a schema-valid contract.

    Separate from ExtractionFailed so a caller can tell which document defeated
    it: an unreadable invoice is one account's problem for one cycle, while an
    unreadable contract blocks every audit that account will ever get.
    """

    def __init__(self, source_uri: str, attempts: int, repair_notes: list[str]) -> None:
        last = repair_notes[-1] if repair_notes else "unknown"
        super().__init__(
            f"could not extract a contract from {source_uri} after {attempts} "
            f"attempt(s); last rejection: {last}"
        )
        self.source_uri = source_uri
        self.attempts = attempts
        self.repair_notes = repair_notes


SYSTEM_INSTRUCTION = """\
You transcribe signed telecom service contracts. You do not interpret them.

Rules, in order of importance:
1. Report only what the contract states. If a term is not stated, omit the
   optional field rather than inferring it. Never invent a plan, a line or a
   rate. A plan the contract does not price is not a plan.
2. Do not compute anything. Do not annualise a monthly rate, do not convert a
   discount into a price, do not derive an overage rate from a bundle price.
   Copy what is written.
3. Normalise every amount to a plain decimal string using a dot separator, with
   no currency symbol and no thousands separator: "1.234,56" and "1,234.56"
   both become "1234.56".
4. Normalise every date to YYYY-MM-DD.
5. Convert data allowances to megabytes. 1 GB is 1024 MB.
6. Every line listed under `lines` must name a plan that appears under `plans`.
   If a line's plan is not priced anywhere in the contract, omit the line: a
   line pointing at a plan that does not exist is rejected outright.
7. `effective_from` is the date the agreement takes effect. Leave
   `effective_to` null for an open-ended contract.
8. An add-on with no line restriction is account-wide: leave `line_ids` empty
   rather than listing every line.
9. One product is one entry. Do not file the same plan twice because another
   document abbreviates its name — a bill that says "Conecta 40 GB" for the
   contract's "Vantel Conecta Empresas 40 GB" is the same plan, and the name
   that belongs in the record is the contract's own. Matching the two wordings
   is not your problem to solve, and solving it by adding a plan puts a term in
   the agreement that nobody signed.

Return JSON matching the provided schema and nothing else."""


def build_prompt(profile: ExtractionProfile, account_id: str | None = None) -> str:
    """Fold the carrier quirks, and the account if we already know it, into the ask.

    The account id is worth passing when it is known: the contract is stored
    under it, and a document whose own header spells it differently would file
    the contract where `get_contract` will never look.
    """
    parts = [
        f"Carrier: {profile.carrier_name} ({profile.country}). "
        f"Amounts are in {profile.currency}."
    ]
    if profile.prompt_hints:
        parts.append("Layout notes for this carrier:")
        parts.extend(f"- {hint}" for hint in profile.prompt_hints)
    if account_id:
        parts.append(
            f"This contract belongs to account {account_id}. Use exactly that "
            "value for account_id, even if the document writes it differently."
        )
    parts.append("Transcribe this contract.")
    return "\n".join(parts)


def extract_contract(
    source: InvoiceSource,
    profile: ExtractionProfile,
    *,
    account_id: str | None = None,
    client: Client | None = None,
    model_id: str | None = None,
    max_repairs: int | None = None,
    max_transient_retries: int | None = None,
) -> Contract:
    """Transcribe one signed contract PDF into a validated Contract.

    Raises ContractExtractionFailed if the model cannot satisfy the schema
    within the repair budget. The budget matters more here than for an invoice:
    Contract rejects a line whose plan is not priced, so a partially-read
    contract is caught by the schema and re-asked rather than filed with a hole
    in it.
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

    generate_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=config.EXTRACTION_TEMPERATURE,
        response_mime_type="application/json",
        response_schema=Contract,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[
                source.as_part(pdf_bytes),
                types.Part.from_text(text=build_prompt(profile, account_id)),
            ],
        )
    ]

    try:
        contract, _attempts, _notes = generate_validated(
            client,
            model_id,
            contents,
            generate_config,
            Contract,
            budget=budget,
            retries=retries,
        )
    except BudgetExhausted as exhausted:
        raise ContractExtractionFailed(
            source.uri, exhausted.attempts, exhausted.repair_notes
        ) from exhausted

    return contract


__all__ = [
    "ContractExtractionFailed",
    "SYSTEM_INSTRUCTION",
    "build_prompt",
    "extract_contract",
]
