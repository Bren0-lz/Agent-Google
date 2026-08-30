"""ADK wrapper around extract_invoice.

The agent is a thin shell on purpose. All of the behaviour worth testing lives
in extractor.py as a plain function; this class only reads where the PDF is,
calls it, persists the result and publishes it to session state for the auditor
downstream. Nothing here decides anything, which is why it is a BaseAgent and
not an LlmAgent: there is no prompt at this level to get wrong.

State contract, so the SequentialAgent assembled on Day 2 has something stable
to build against:

    in    source_uri      file:// or gs:// location of the invoice PDF
    in    invoice_attached  intake approved the PDF on this message
    in    (attachment)    a PDF on the message itself, when no source_uri is set
    in    profile_key     carrier profile; defaults to config.DEFAULT_PROFILE_KEY
    out   canonical_invoice   the CanonicalInvoice, as JSON-safe dict
    out   content_hash        Firestore document id of that record
    out   extraction_created  False when this PDF had already been processed
"""

from __future__ import annotations

import dataclasses
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from . import config, store
from .extractor import ExtractionFailed, InvoiceSource, extract_invoice
from .intake import profile_key_in, split_attachments
from .profiles import profile_for
from .schema import content_hash


class ExtractorAgent(BaseAgent):
    """Turn the invoice named in session state into a canonical record."""

    persist: bool = True

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        source = self._source(ctx, state)
        if source is None:
            # Nothing to work with. The intake stage has already said so, in
            # terms the person can act on, so repeating "no source_uri" here
            # would only add noise to a message that already explained itself.
            return

        profile = profile_for(state.get("profile_key") or config.DEFAULT_PROFILE_KEY)

        # The bytes are read once, here, so the content hash can be looked up
        # before the model is called: re-sending a PDF that has already been
        # extracted used to pay for a full extraction that save_invoice then
        # threw away as AlreadyExists. `replace` keeps the uri, so provenance
        # and as_part's gs:// branch are unaffected — it only carries the bytes
        # along so a GCS source is not downloaded a second time.
        pdf_bytes = source.read()
        digest = content_hash(pdf_bytes)
        source = dataclasses.replace(source, _data=pdf_bytes)

        canonical = store.get_invoice(digest) if self.persist else None
        created = canonical is None

        if canonical is None:
            try:
                canonical = extract_invoice(source, profile)
            except ExtractionFailed as failure:
                # Surfaced rather than swallowed: an invoice the model cannot read is
                # a case for a human, not a silently empty audit.
                yield self._say(
                    ctx,
                    f"Extraction failed for {failure.source_uri} after "
                    f"{failure.attempts} attempt(s). Escalating for human review.\n"
                    + "\n".join(failure.repair_notes[-1:]),
                )
                return

        mismatch = self._carrier_mismatch(canonical, profile)
        if mismatch is not None:
            # Nothing is persisted and nothing reaches state, so every stage
            # downstream stops on its own: the auditor has nothing_was_extracted
            # and the dispute writer returns without a canonical invoice.
            yield self._say(ctx, mismatch)
            return

        if self.persist and created:
            canonical, created = store.save_invoice(canonical)

        summary = (
            f"Extracted invoice {canonical.invoice.header.account_id} "
            f"({canonical.invoice.header.billing_period_end:%Y-%m}) from "
            f"{profile.carrier_name}: {len(canonical.invoice.service_lines)} line(s), "
            f"{len(canonical.invoice.charges)} charge(s), total "
            f"{canonical.invoice.header.total_amount} "
            f"{canonical.invoice.header.currency}. "
            f"{canonical.provenance.attempts} model call(s)."
        )
        if canonical.provenance.warnings:
            summary += "\nWarnings: " + "; ".join(canonical.provenance.warnings)
        if not created:
            summary += (
                "\nAlready in Firestore under this content hash; reused as stored "
                "and not re-extracted."
            )

        yield self._say(
            ctx,
            summary,
            state_delta={
                "canonical_invoice": canonical.model_dump(mode="json"),
                "content_hash": canonical.content_hash,
                "extraction_created": created,
            },
        )

    def _carrier_mismatch(self, canonical, profile) -> str | None:
        """Why this invoice must not be read with this profile, or None.

        profile_for() refuses a carrier it has no layout for rather than
        guessing, but nothing used to check that the carrier the caller named is
        the carrier printed on the page. Saying 'northwind' over a Vantel bill
        was accepted in production and audited without a word of warning: the
        American profile's separator hints read 1.234,56 as 1.234, which is
        exactly the plausible-and-wrong number that refusing exists to prevent.

        Matched with profile_key_in rather than string equality, because a bill
        prints VANTEL EMPRESAS S.A. where the profile says Vantel Empresas.
        """
        printed = canonical.invoice.header.carrier
        if profile_key_in(printed) == profile.profile_key:
            return None

        return (
            f"Refusing to audit this one. You asked me to read it as a "
            f"{profile.carrier_name} invoice, but the bill says it was issued by "
            f"{printed!r}. Reading a carrier's layout with another carrier's "
            f"profile produces figures that look right and are not, so I would "
            f"rather stop here. Name the right carrier and send it again."
        )

    def _source(self, ctx: InvocationContext, state) -> InvoiceSource | None:
        """Where this run's PDF comes from, or None if there is no PDF.

        `source_uri` wins when set, because that is what a caller who built the
        session deliberately asked for — the eval harness, the README's curl
        flow and Pub/Sub ingestion all take that path, and none of them attach
        anything to a message.

        Otherwise the PDF came in on the conversation. The bytes are read from
        the message rather than from state on purpose: state deltas are
        serialised to JSON on the way out of /run, and bytes do not survive it.
        Which does not make the attachment self-authorising: `invoice_attached`
        is intake's verdict on that same message, and reading around it would
        undo every refusal intake makes — a bill whose printed carrier
        contradicts the one named is turned away there precisely so nobody pays
        for the extraction, and this stage cannot see the attachment without
        also seeing that it was rejected. Sibling stages of a SequentialAgent
        cannot be skipped from outside, so each one asks.
        """
        location = state.get("source_uri")
        if location:
            return InvoiceSource.resolve(location)

        if not state.get("invoice_attached"):
            return None

        _contracts, invoices = split_attachments(ctx.user_content)
        if not invoices:
            return None

        attachment = invoices[0]
        return InvoiceSource.from_bytes(attachment.data, name=attachment.name)

    def _say(
        self, ctx: InvocationContext, text: str, state_delta: dict | None = None
    ) -> Event:
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta=state_delta or {}),
        )
