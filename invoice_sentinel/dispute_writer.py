"""Write the dispute letter and the customer summary, then check the numbers.

Same shape as the extractor, for the same reason: a deterministic function the
tests can drive with a fake client, wrapped in a thin agent. The repair loop
here is not about schema validity but about arithmetic honesty - if a figure in
the letter matches nothing the engine computed, the model is told exactly which
figure and asked again.

If it still cannot write the letter without inventing a number, the dispute is
stored as `blocked` rather than `draft`. That is deliberate. The failure mode
this guards against is a plausible, confident, wrong number in a document sent
to a carrier over the customer's name, and the right response to it is a person,
not another attempt.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import Client, types

from . import amount_guard, audit_tools, config, store
from .anomaly import Anomaly
from .audit_tools import STATE_CONTENT_HASH, STATE_INVOICE
from .dispute import Dispute, DisputeDocuments, DisputeStatus
from .extractor import _call_model, _default_client
from .schema import CanonicalInvoice

STATE_DISPUTE = "dispute"


SYSTEM_INSTRUCTION = """\
You draft billing correspondence for a telecom consultancy, on behalf of the
customer.

You write two documents about one audited invoice.

1. carrier_letter - formal, addressed to the carrier's billing department. It
   covers ONLY the disputed charges: things the carrier billed that the contract
   does not support. For each one, state what was billed, what the contract
   says, and over how many cycles. Close by requesting correction and a credit.
   Never mention the customer's plan choices here - the carrier is not
   responsible for those, and raising them invites the whole letter to be
   dismissed.

2. executive_summary - plain language, addressed to the customer. Covers both
   the disputed charges and the recommended plan changes, kept clearly apart:
   money the carrier owes, and money the customer is losing to their own plan
   configuration. Say what will happen next for each.

   Both documents are drafts awaiting review. Never write that anything has
   already been sent, submitted, filed or credited - none of it has, and telling
   a customer their dispute is already with the carrier is a claim they will
   repeat and later have to retract.

Rules about numbers, and they are absolute:

  * Use only the amounts given to you below. Copy them exactly.
  * Do not add up, subtract, average, annualise or otherwise derive any figure.
    If you want to state a total, use the total you were given.
  * Do not estimate. Do not write "approximately", "around" or "up to" in front
    of a number you were not given.

Every figure you write is checked against what was computed. An invented figure
means the document is withheld and a person has to intervene, so write no number
you were not handed.

Write in the language of the invoice: Portuguese for a Brazilian carrier,
English for an American one.
"""


def _findings_brief(anomalies: list[Anomaly], heading: str) -> str:
    """The facts the writer is allowed to use, and nothing else."""
    if not anomalies:
        return f"{heading}: none.\n"

    lines = [f"{heading}:"]
    for anomaly in anomalies:
        lines.append(
            f"  - [{anomaly.type.value}] line {anomaly.line_id or 'account-level'}: "
            f"{anomaly.summary}"
        )
        lines.append(
            f"      amount: {anomaly.recovered_amount}  "
            f"over {anomaly.months_affected} cycle(s)  "
            f"({anomaly.monthly_amount().quantize(Decimal('0.01'))} per cycle)"
        )
        for evidence in anomaly.evidence:
            lines.append(f"      evidence: {evidence.claim} = {evidence.value} ({evidence.source})")
    return "\n".join(lines) + "\n"


def build_prompt(
    invoice: CanonicalInvoice,
    disputed: list[Anomaly],
    optimisations: list[Anomaly],
    disputed_total: Decimal,
    optimisation_total: Decimal,
) -> str:
    header = invoice.invoice.header
    return "\n".join(
        [
            f"Carrier: {header.carrier}",
            f"Account: {header.account_id}",
            f"Billing cycle: {header.billing_period_start} to {header.billing_period_end}",
            f"Invoice total as printed: {header.total_amount} {header.currency}",
            "",
            _findings_brief(disputed, "DISPUTED CHARGES (the carrier's responsibility)"),
            _findings_brief(
                optimisations, "PLAN CHANGES TO RECOMMEND (the customer's own configuration)"
            ),
            f"Total to request from the carrier: {disputed_total}",
            f"Total the customer can save by changing plans: {optimisation_total}",
            "",
            "Write the two documents.",
        ]
    )


def _repair_prompt(offending: list[tuple[str, Decimal]]) -> str:
    quoted = ", ".join(sorted({raw for raw, _ in offending}))
    return (
        "Your draft contains figures that were not computed for this invoice: "
        f"{quoted}. Every amount must be copied from the findings above; none may "
        "be derived, summed or estimated. Rewrite both documents using only the "
        "amounts you were given. Change nothing else."
    )


def write_dispute(
    invoice: CanonicalInvoice,
    disputed: list[Anomaly],
    optimisations: list[Anomaly],
    *,
    client: Client | None = None,
    model_id: str | None = None,
    max_repairs: int | None = None,
) -> Dispute:
    """Draft both documents, verifying every figure before returning."""
    client = client or _default_client()
    model_id = model_id or config.MODEL_ID
    budget = config.MAX_EXTRACTION_REPAIRS if max_repairs is None else max_repairs

    disputed_total = sum((a.recovered_amount for a in disputed), Decimal(0))
    optimisation_total = sum((a.recovered_amount for a in optimisations), Decimal(0))
    allowed = amount_guard.allowed_amounts([*disputed, *optimisations])
    allowed |= {disputed_total, optimisation_total, invoice.invoice.header.total_amount}

    generate_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=config.EXTRACTION_TEMPERATURE,
        response_mime_type="application/json",
        response_schema=DisputeDocuments,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=build_prompt(
                        invoice, disputed, optimisations, disputed_total, optimisation_total
                    )
                )
            ],
        )
    ]

    attempts = 0
    offending: list[tuple[str, Decimal]] = []
    documents = DisputeDocuments(carrier_letter="", executive_summary="")

    while True:
        attempts += 1
        payload = _call_model(
            client,
            model_id,
            contents,
            generate_config,
            retries=config.MAX_TRANSIENT_RETRIES,
            backoff=config.TRANSIENT_RETRY_BACKOFF,
        )
        documents = DisputeDocuments.model_validate_json(payload)

        offending = amount_guard.unverified_amounts(
            documents.carrier_letter, allowed
        ) + amount_guard.unverified_amounts(documents.executive_summary, allowed)

        if not offending or attempts > budget:
            break

        contents.append(types.Content(role="model", parts=[types.Part.from_text(text=payload)]))
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=_repair_prompt(offending))])
        )

    header = invoice.invoice.header
    return Dispute(
        content_hash=invoice.content_hash,
        account_id=header.account_id,
        carrier=header.carrier,
        currency=header.currency,
        period=f"{header.billing_period_end:%Y-%m}",
        status=DisputeStatus.BLOCKED if offending else DisputeStatus.DRAFT,
        carrier_letter=documents.carrier_letter,
        executive_summary=documents.executive_summary,
        disputed_finding_ids=[audit_tools.finding_id(a) for a in disputed],
        optimisation_finding_ids=[audit_tools.finding_id(a) for a in optimisations],
        disputed_total=disputed_total,
        optimisation_total=optimisation_total,
        amounts_verified=not offending,
        unverified_amounts=sorted({raw for raw, _ in offending}),
        generated_at=datetime.datetime.now(datetime.timezone.utc),
        model_id=model_id,
        attempts=attempts,
    )


class DisputeWriterAgent(BaseAgent):
    """Turn the auditor's decisions into the two documents."""

    persist: bool = True

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        invoice_payload = state.get(STATE_INVOICE)
        if not invoice_payload:
            yield self._say(ctx, "No invoice in state; nothing to write about.")
            return

        disputed = audit_tools.decided_anomalies(state, "dispute")
        optimisations = audit_tools.decided_anomalies(state, "optimise")
        if not disputed and not optimisations:
            yield self._say(
                ctx,
                "Nothing was flagged for dispute or recommended for a plan change. "
                "No letter written.",
            )
            return

        invoice = CanonicalInvoice.model_validate(invoice_payload)
        dispute = write_dispute(invoice, disputed, optimisations)

        if self.persist:
            store.save_dispute(dispute)

        if dispute.amounts_verified:
            note = (
                f"Drafted a dispute for {dispute.account_id} ({dispute.period}): "
                f"{len(disputed)} charge(s) contested totalling {dispute.disputed_total} "
                f"{dispute.currency}, {len(optimisations)} plan change(s) worth "
                f"{dispute.optimisation_total}. Every figure in both documents was "
                f"checked against the rule engine's output."
            )
        else:
            note = (
                f"WITHHELD: the draft for {dispute.account_id} contains figure(s) the "
                f"rule engine never produced ({', '.join(dispute.unverified_amounts)}). "
                f"Stored as blocked for human review; nothing will be sent."
            )

        yield self._say(
            ctx, note, state_delta={STATE_DISPUTE: dispute.model_dump(mode="json")}
        )

    def _say(self, ctx: InvocationContext, text: str, state_delta=None) -> Event:
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta=state_delta or {}),
        )
