"""ADK wrapper around extract_invoice.

The agent is a thin shell on purpose. All of the behaviour worth testing lives
in extractor.py as a plain function; this class only reads where the PDF is,
calls it, persists the result and publishes it to session state for the auditor
downstream. Nothing here decides anything, which is why it is a BaseAgent and
not an LlmAgent: there is no prompt at this level to get wrong.

State contract, so the SequentialAgent assembled on Day 2 has something stable
to build against:

    in    source_uri      file:// or gs:// location of the invoice PDF
    in    profile_key     carrier profile; defaults to config.DEFAULT_PROFILE_KEY
    out   canonical_invoice   the CanonicalInvoice, as JSON-safe dict
    out   content_hash        Firestore document id of that record
    out   extraction_created  False when this PDF had already been processed
"""

from __future__ import annotations

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from . import config, store
from .extractor import ExtractionFailed, InvoiceSource, extract_invoice
from .profiles import profile_for


class ExtractorAgent(BaseAgent):
    """Turn the invoice named in session state into a canonical record."""

    persist: bool = True

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        location = state.get("source_uri")
        if not location:
            yield self._say(ctx, "No source_uri in session state; nothing to extract.")
            return

        profile = profile_for(state.get("profile_key") or config.DEFAULT_PROFILE_KEY)
        source = InvoiceSource.resolve(location)

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

        created = True
        if self.persist:
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
            summary += "\nAlready in Firestore under this content hash; not rewritten."

        yield self._say(
            ctx,
            summary,
            state_delta={
                "canonical_invoice": canonical.model_dump(mode="json"),
                "content_hash": canonical.content_hash,
                "extraction_created": created,
            },
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
