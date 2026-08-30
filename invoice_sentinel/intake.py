"""Turning a person with a PDF into a run the pipeline can execute.

Before this module the agent could only audit what was already in the bucket,
with a contract already seeded in Firestore. The state contract was set by
whoever created the session over HTTP, which is fine for the eval harness and
for a judge following the README, and useless for anybody else: the ADK Web UI
creates sessions with empty state, so every message reached an extractor with no
source_uri and produced a chain of "nothing to extract".

The intake stage closes that gap. It reads the PDFs attached to the message,
decides what each one is, and writes the state the rest of the graph already
expects — so the auditor, the rule engine and the dispute writer are untouched
by any of this. Filing a contract under its account id is enough for
load_audit_context to find it on the next run.

Three deliberate refusals, in the same spirit as profile_for() and
run_rule_family():

  * an ambiguous batch of attachments is a question, not a guess;
  * an unknown carrier is refused rather than read with another carrier's
    layout hints, because that produces plausible, wrong numbers;
  * a bill whose printed carrier is not the one named is turned away here,
    before an extraction is paid for.

Which carrier a run uses is read off the bill rather than assumed. The name is
printed on page one by definition, so a text layer and a regex settle it — no
model call, and no default standing in for an answer nobody gave.

State contract:

    out   invoice_attached    an invoice PDF is on this message
    out   uploaded_name       the filename the person sent, for provenance
    out   profile_key         the carrier profile chosen for this run
    out   contract_on_file    account id whose contract was just stored
"""

from __future__ import annotations

import dataclasses
import io
import re
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pypdf import PdfReader

from . import config, store
from .contract_extractor import ContractExtractionFailed, extract_contract
from .extractor import InvoiceSource
from .profiles import PROFILES, profile_for
from .schema import ExtractionProfile

PDF_MIME_TYPE = "application/pdf"

#: Word stems meaning "this attachment is the agreement, not a bill". Both
#: languages, because the carriers this project ships profiles for are Brazilian
#: and American and the person sending the file writes in their own.
CONTRACT_WORDS = ("contrato", "contract", "agreement", "acordo")


@dataclasses.dataclass(frozen=True)
class PdfAttachment:
    """One PDF that arrived in the conversation."""

    data: bytes
    name: str | None = None

    @property
    def label(self) -> str:
        return self.name or "the attached PDF"


def pdf_attachments(content: types.Content | None) -> list[PdfAttachment]:
    """Every PDF in a message, in the order it was attached.

    Non-PDF attachments are dropped rather than refused: someone pasting a
    screenshot alongside their invoice should not get an error about the
    screenshot. Anything with no bytes is skipped too — the UI sends one part
    per selected file, and a failed upload arrives empty.
    """
    if content is None or not content.parts:
        return []

    found: list[PdfAttachment] = []
    for part in content.parts:
        blob = getattr(part, "inline_data", None)
        if blob is None or not getattr(blob, "data", None):
            continue
        if (getattr(blob, "mime_type", "") or "").lower() != PDF_MIME_TYPE:
            continue
        found.append(
            PdfAttachment(data=blob.data, name=getattr(blob, "display_name", None))
        )
    return found


def message_text(content: types.Content | None) -> str:
    """Everything the person typed, as one lowercase string."""
    if content is None or not content.parts:
        return ""
    return " ".join(
        part.text for part in content.parts if getattr(part, "text", None)
    ).lower()


def profile_aliases(key: str, profile: ExtractionProfile) -> set[str]:
    """Every way somebody might name this carrier.

    The key, the full carrier name, and — the one that matters in practice —
    the brand on its own. Someone sending a Vantel bill types "vantel", not
    "br-vantel-empresas": the first deployment of this stage answered "you did
    not name a carrier" to a message that said exactly which carrier it was.

    Only the first word of the carrier name is taken as the brand. The rest is
    the kind of word every carrier shares — Empresas, Wireless — and matching on
    those would let "wireless" pick a carrier out of a sentence that never
    named one.
    """
    name = profile.carrier_name.lower()
    return {key.lower(), name, name.split()[0]}


def profile_keys_in(text: str) -> set[str]:
    """Every carrier profile named anywhere in a piece of text.

    A set, because the callers disagree about what more than one means. In a
    sentence somebody typed it is a contradiction to resolve; on the face of an
    invoice it is a document naming a carrier it was not issued by. Deciding
    that here would be this function guessing on behalf of both.
    """
    haystack = text.lower()
    return {
        key
        for key, profile in PROFILES.items()
        # Whole words only: a carrier called "TIM" must not be found inside
        # "estimativa", and this runs over free text somebody typed.
        if any(
            re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", haystack)
            for alias in profile_aliases(key, profile)
        )
    }


def profile_key_in(text: str) -> str | None:
    """The carrier profile named in the message, if any.

    Returns None rather than a default: choosing the default is the agent's
    decision to announce, not this function's to make silently. Two carriers
    named at once is also None — "vantel or northwind?" names no carrier, and
    answering with whichever came first in the registry would be a coin toss
    wearing a straight face.
    """
    named = profile_keys_in(text)
    return named.pop() if len(named) == 1 else None


def first_page_text(data: bytes) -> str:
    """The text layer of a PDF's first page, or "" when it has none.

    Page one is enough by construction: what identifies the issuer — the
    letterhead, the tax registration, the carrier's own name — is printed where
    the bill starts, and reading further would spend time to learn nothing.

    A scanned invoice has no text layer and comes back empty. That is an
    answer, not a failure, which is why a PDF pypdf cannot parse is caught into
    the same empty string: the model reads the raw bytes and may well manage
    where this cannot, so the caller should get to ask which carrier it is
    rather than watch intake crash on the way to the same question.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        if not reader.pages:
            return ""
        return reader.pages[0].extract_text() or ""
    except Exception:  # noqa: BLE001 - see docstring
        return ""


def carriers_printed_in(attachment: PdfAttachment) -> set[str]:
    """Every carrier profile whose name is printed on this PDF.

    The counterpart of profile_key_in: that one reads what the person typed,
    this one reads what the document says. Deterministic, in Python, with no
    model call — the same reasoning that keeps the rule families out of an
    LlmAgent applies to a lookup that a regex over a text layer settles.
    """
    return profile_keys_in(first_page_text(attachment.data))


def split_attachments(
    content: types.Content | None,
) -> tuple[list[PdfAttachment], list[PdfAttachment]]:
    """Sort a message's PDFs into (contracts, invoices).

    Shared by the intake agent, which decides and announces, and by the
    extractor, which needs the bytes. Both call it on the same message within
    one invocation, so both reach the same answer — which is why the raw PDF
    never has to travel through session state. That matters more than it
    looks: a state delta is serialised to JSON in the /run response, and bytes
    do not survive that trip.
    """
    attachments = pdf_attachments(content)
    if not attachments:
        return [], []

    # With one attachment the message is good evidence about it: "here is the
    # contract" can only mean that file. With several it is evidence about none
    # of them in particular — the same sentence would mark every attachment as
    # the contract — so only the filenames get a vote, and a batch that stays
    # undecided reaches the agent as a question.
    if len(attachments) == 1:
        only = attachments[0]
        if looks_like_contract(message_text(content), only):
            return [only], []
        return [], [only]

    contracts, invoices = [], []
    for attachment in attachments:
        bucket = contracts if looks_like_contract("", attachment) else invoices
        bucket.append(attachment)
    return contracts, invoices


def looks_like_contract(text: str, attachment: PdfAttachment) -> bool:
    """Whether this PDF is the signed agreement rather than a bill.

    Reads the message first and the filename second. Neither is authoritative,
    which is why the agent asks when a batch is ambiguous instead of deciding
    here — but between "the person said contract" and "the file is called
    contrato.pdf", both beat a coin toss, and a wrong guess is recoverable: a
    contract read as an invoice fails the invoice schema loudly rather than
    filing bad terms.
    """
    words = re.findall(r"\w+", f"{text} {attachment.name or ''}".lower())
    return any(word.startswith(stem) for word in words for stem in CONTRACT_WORDS)


def help_text() -> str:
    """What to say to someone who sent no PDF.

    The first version named a state key the person can neither set nor see. The
    second showed the message to send, because the detail people got wrong was
    that the PDF and the carrier's name had to travel together. They no longer
    do: the bill is read for its own letterhead, so naming a carrier went from
    the instruction to the exception, and the shortest true description of what
    to do is now "attach it".
    """
    carriers = "\n".join(
        f"- {profile.carrier_name} ({profile.country}) — say "
        f"'{profile.carrier_name.split()[0].lower()}' or '{key}'"
        for key, profile in sorted(PROFILES.items())
    )
    example = sorted(PROFILES.values(), key=lambda p: p.profile_key)[0]
    return (
        "I audit telecom invoices. Attach one as a PDF and I read every line, "
        "compare it against the signed contract and against the account's "
        "earlier cycles, and draft what is worth disputing.\n\n"
        "**Send it like this** — the PDF on its own is enough:\n\n"
        f"    [attach {example.carrier_name.split()[0].lower()}-july.pdf]\n\n"
        "I read the carrier's name off the bill. The ones I have a layout "
        "profile for:\n\n"
        f"{carriers}\n\n"
        # Unindented and in its own paragraph: the UI renders this as Markdown,
        # where an indented line after a list is swallowed as a continuation of
        # the last bullet — on screen it ran straight on from the last item.
        "Any other carrier I refuse rather than misread. If the name is not "
        "printed anywhere I can read it — a scan carries no text — I ask "
        "instead of assuming, and you can name it in the same message as the "
        "PDF. Name one the bill contradicts and I stop before extracting "
        "anything.\n\n"
        "**Two things worth knowing before you send one:**\n\n"
        "1. I audit against a signed contract. Without one I cannot tell an "
        "overcharge from a charge you agreed to, so I will say I could not "
        "audit it rather than call it clean. If I have never seen this "
        "account's contract, attach that PDF too and say 'contract' — I file "
        "it, and every invoice you send afterwards is audited against it.\n"
        f"2. One invoice is enough to catch a wrong rate or an add-on nobody "
        f"agreed to. Findings about plan sizing claim a *pattern*, so they stay "
        f"quiet until the account has {config.PATTERN_CYCLES} consecutive "
        f"cycles on file."
    )


class IntakeAgent(BaseAgent):
    """Read what the person attached and set up the run.

    A BaseAgent rather than an LlmAgent: deciding whether a PDF is a contract,
    and which profile was named, is pattern matching. A model call whose only
    job is to route would be tokens spent on nothing — the same reasoning that
    keeps the rule families out of LlmAgent.
    """

    persist: bool = True

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        text = message_text(ctx.user_content)
        attachments = pdf_attachments(ctx.user_content)

        if not attachments:
            # A session created over HTTP carries its own source_uri, and both
            # the eval harness and the README's curl flow depend on that path
            # staying open. Only speak up when there is genuinely nothing.
            if not state.get("source_uri"):
                yield self._say(ctx, help_text())
            return

        contracts, invoices = split_attachments(ctx.user_content)

        # One PDF with nothing said about it is an invoice. Two or more with no
        # way to tell them apart is a question: guessing which one is the
        # agreement risks auditing a bill against another bill.
        if not contracts and len(invoices) > 1:
            names = ", ".join(a.label for a in invoices)
            yield self._say(
                ctx,
                f"You attached {len(invoices)} PDFs ({names}) and I cannot tell "
                "which is which. Send the invoice on its own, or say which "
                "attachment is the contract.",
            )
            return

        # The message first, then the session: a caller that built the session
        # over HTTP named a carrier deliberately, but somebody typing one now is
        # answering about the file they just attached.
        asked_for = profile_key_in(text) or state.get("profile_key")
        if asked_for is not None:
            try:
                profile_for(asked_for)
            except ValueError as error:
                yield self._say(ctx, str(error))
                return

        # The invoice decides when there is one; a contract sent on its own is
        # filed under the same profile, and carries the carrier's name too.
        profile_key, note = self._profile_for_pdf(
            asked_for, invoices[0] if invoices else contracts[0]
        )
        if profile_key is None:
            yield self._say(ctx, note)
            return

        profile = profile_for(profile_key)
        notes: list[str] = [note]
        delta: dict = {}

        for attachment in contracts:
            account_id, note = self._store_contract(attachment, profile)
            notes.append(note)
            if account_id is not None:
                delta["contract_on_file"] = account_id

        if invoices:
            # Only the decision travels through state; the extractor re-reads
            # the bytes from the same message. See split_attachments().
            delta["invoice_attached"] = True
            delta["uploaded_name"] = invoices[0].name
            delta["profile_key"] = profile_key
        elif contracts:
            notes.append("Attach an invoice next and I will audit it against this.")

        yield self._say(ctx, "\n".join(notes), state_delta=delta)

    def _profile_for_pdf(
        self, asked_for: str | None, pdf: PdfAttachment
    ) -> tuple[str | None, str]:
        """Which profile to read this PDF with, and what to say about it.

        A None key means the second element is a refusal and the run stops
        here.

        The bill is the better witness, so it is read first. Until this
        existed the stage fell back to DEFAULT_PROFILE_KEY whenever nobody
        typed a carrier, which meant a Northwind invoice sent without a word
        paid for a whole extraction under Brazilian separator hints before
        ExtractorAgent._carrier_mismatch refused it — the right answer, reached
        the expensive way, and reached at all only because that check exists.
        Reading the printed name turns the assumption into a look, and it costs
        a regex over a text layer rather than a model call.

        What the person typed still wins where the page is not evidence: a
        scan has no text to read, and a bill that mentions two carriers this
        build knows has named one it was not issued by. Neither contradicts
        them, so their word stands and the printed carrier is checked again
        after extraction, where the model has actually read the page.
        """
        printed = carriers_printed_in(pdf)

        if len(printed) == 1:
            found = printed.pop()
            if asked_for is not None and asked_for != found:
                return None, (
                    f"Refusing to read this one. You named "
                    f"{profile_for(asked_for).carrier_name}, but {pdf.label} "
                    f"says it was issued by {profile_for(found).carrier_name}. "
                    f"Reading a carrier's layout with another carrier's profile "
                    f"produces figures that look right and are not, so I would "
                    f"rather stop before spending an extraction on it."
                )
            return found, (
                f"Reading {pdf.label} as a {profile_for(found).carrier_name} "
                f"document — that is the carrier printed on it."
            )

        if asked_for is not None:
            return asked_for, (
                f"Reading {pdf.label} as a {profile_for(asked_for).carrier_name} "
                f"document, as you named. I could not tell from the page itself, "
                f"so I check the carrier again once it has been read."
            )

        if printed:
            names = ", ".join(sorted(profile_for(k).carrier_name for k in printed))
            return None, (
                f"{pdf.label} names more than one carrier I have a profile for "
                f"({names}), so I cannot tell which one issued it. Say which and "
                f"send it again."
            )

        known = ", ".join(
            sorted(profile.carrier_name for profile in PROFILES.values())
        )
        return None, (
            f"I could not find a carrier name in {pdf.label} — a scanned PDF "
            f"carries no text to read. Tell me who issued it and send it again "
            f"({known}). I would rather ask than read it with the wrong layout, "
            f"which produces figures that look right and are not."
        )

    def _store_contract(
        self, attachment: PdfAttachment, profile: ExtractionProfile
    ) -> tuple[str | None, str]:
        """Transcribe a contract and file it, or explain why it could not be."""
        source = InvoiceSource.from_bytes(attachment.data, name=attachment.name)
        try:
            contract = extract_contract(source, profile)
        except ContractExtractionFailed as failure:
            return None, (
                f"I could not read {attachment.label} as a contract after "
                f"{failure.attempts} attempt(s). Nothing was filed — auditing "
                "against a half-read agreement is worse than not auditing."
            )

        if self.persist:
            store.save_contract(contract)

        return contract.account_id, (
            f"Filed the contract for account {contract.account_id}: "
            f"{len(contract.plans)} plan(s), {len(contract.lines)} line(s), "
            f"{len(contract.addons)} add-on(s), effective "
            f"{contract.effective_from:%Y-%m-%d}."
        )

    def _say(
        self, ctx: InvocationContext, text: str, state_delta: dict | None = None
    ) -> Event:
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta=state_delta or {}),
        )
