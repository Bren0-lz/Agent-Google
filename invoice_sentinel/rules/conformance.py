"""Rules that compare the invoice against the contract, line by line.

These two are the most objective findings the engine produces: they do not
reason about behaviour or patterns, they compare two numbers that were supposed
to be equal, or look for an entitlement that was supposed to exist. That is why
they carry the highest confidence.

The contract is the only source of contractual truth here. An invoice that
overcharges will state the wrong rate as fact, so the rate on the page can never
be used to validate the rate on the page.
"""

from __future__ import annotations

from decimal import Decimal

from ..anomaly import Anomaly, AnomalyType, Evidence
from ..config import RATE_DRIFT_TOLERANCE
from ..schema import ChargeCategory, ExtractedInvoice, LineStatus, UsageMetric
from .base import AuditContext, money, trailing_streak


def _billed_in(invoice: ExtractedInvoice, line_id: str, description: str,
               category: ChargeCategory) -> Decimal:
    """Total billed for one named charge on one line in one cycle."""
    return sum(
        (
            charge.amount
            for charge in invoice.charges
            if charge.line_id == line_id
            and charge.category is category
            and charge.description == description
        ),
        Decimal(0),
    )


def orphan_addon(ctx: AuditContext) -> list[Anomaly]:
    """An add-on billed with no entitlement, or attached to a dead line.

    Unlike the pattern rules, a single cycle is enough: an add-on nobody agreed
    to is wrong the first time it appears. The streak is still measured, because
    it sets how much money is on the table.
    """
    findings: list[Anomaly] = []
    service_lines = {line.line_id: line for line in ctx.invoice.service_lines}

    for charge in ctx.invoice.charges:
        if charge.category is not ChargeCategory.ADDON or charge.line_id is None:
            continue

        entitlement = ctx.contract.addon_for(charge.description, charge.line_id)
        service_line = service_lines.get(charge.line_id)
        parent_dead = service_line is not None and service_line.status in (
            LineStatus.CANCELLED,
            LineStatus.SUSPENDED,
        )

        if entitlement is not None and not parent_dead:
            continue

        streak = trailing_streak(
            ctx.cycles,
            lambda inv, c=charge: _billed_in(inv, c.line_id, c.description,
                                             ChargeCategory.ADDON) > 0,
        )
        affected = ctx.cycles[-streak:] if streak else [ctx.invoice]
        recovered = money(
            sum(
                (
                    _billed_in(inv, charge.line_id, charge.description, ChargeCategory.ADDON)
                    for inv in affected
                ),
                Decimal(0),
            )
        )
        if recovered <= 0:
            continue

        window = f"{AuditContext.period(affected[0])}..{AuditContext.period(affected[-1])}"
        if entitlement is None:
            reason = "no entitlement for this add-on exists on this line in the contract"
            entitled_to = ctx.contract.addon_for(charge.description)
            contract_evidence = Evidence(
                claim=f"Contract entitlement for {charge.description!r}",
                value=(
                    f"granted to lines {entitled_to.line_ids}" if entitled_to is not None
                    else "not present in the contract at all"
                ),
                source="contract",
            )
            confidence = 0.92
        else:
            reason = f"the parent line is {service_line.status.value}"
            contract_evidence = Evidence(
                claim="Status of the line carrying the add-on",
                value=service_line.status.value,
                source=f"invoice {AuditContext.period(ctx.invoice)}",
            )
            confidence = 0.80

        findings.append(
            Anomaly(
                type=AnomalyType.ORPHAN_ADDON,
                line_id=charge.line_id,
                account_id=ctx.account_id,
                summary=(
                    f"{charge.description} has been billed on line {charge.line_id} for "
                    f"{max(streak, 1)} cycle(s) totalling {recovered}, but {reason}."
                ),
                evidence=[
                    Evidence(claim="Add-on billed on the audited invoice",
                             value=f"{charge.description} at {charge.amount}",
                             source=f"invoice {AuditContext.period(ctx.invoice)}"),
                    contract_evidence,
                    Evidence(claim="Cycles billed, consecutive",
                             value=str(max(streak, 1)), source=f"invoices {window}"),
                ],
                confidence=confidence,
                recovered_amount=recovered,
                months_affected=max(streak, 1),
            )
        )

    return findings


def rate_drift(ctx: AuditContext) -> list[Anomaly]:
    """The price charged is not the price agreed.

    Checks both the recurring plan rate and the per-unit overage rate. Only
    overcharges are reported: an undercharge is the carrier's problem, and
    raising it is not a service to the customer.
    """
    findings: list[Anomaly] = []

    for line_id in ctx.line_ids():
        plan = ctx.contract.plan_for_line(line_id)
        if plan is None:
            continue  # line is not in the contract; nothing to compare against

        # --- recurring plan rate ---
        for charge in ctx.charges_for(ctx.invoice, line_id, (ChargeCategory.SUBSCRIPTION,)):
            drift = charge.amount - plan.monthly_rate
            if drift <= RATE_DRIFT_TOLERANCE:
                continue

            streak = trailing_streak(
                ctx.cycles,
                lambda inv, lid=line_id, amount=charge.amount: any(
                    c.amount == amount
                    for c in ctx.charges_for(inv, lid, (ChargeCategory.SUBSCRIPTION,))
                ),
            )
            streak = max(streak, 1)
            affected = ctx.cycles[-streak:]
            recovered = money(drift * streak)

            window = f"{AuditContext.period(affected[0])}..{AuditContext.period(affected[-1])}"
            findings.append(
                Anomaly(
                    type=AnomalyType.RATE_DRIFT,
                    line_id=line_id,
                    account_id=ctx.account_id,
                    summary=(
                        f"Line {line_id} is billed {charge.amount} per cycle for "
                        f"{plan.plan_name}, which is contracted at {plan.monthly_rate}. "
                        f"The difference totals {recovered} over {streak} cycles."
                    ),
                    evidence=[
                        Evidence(claim="Rate charged per cycle", value=str(charge.amount),
                                 source=f"invoice {AuditContext.period(ctx.invoice)}"),
                        Evidence(claim=f"Rate contracted for {plan.plan_name}",
                                 value=str(plan.monthly_rate), source="contract"),
                        Evidence(claim="Difference per cycle", value=str(money(drift)),
                                 source="computed"),
                        Evidence(claim="Cycles billed at the drifted rate",
                                 value=str(streak), source=f"invoices {window}"),
                    ],
                    confidence=0.97,
                    recovered_amount=recovered,
                    months_affected=streak,
                )
            )

        # --- per-unit overage rate ---
        allowance = plan.allowance_for(UsageMetric.DATA_MB)
        if allowance is None:
            continue
        for charge in ctx.charges_for(ctx.invoice, line_id, (ChargeCategory.USAGE,)):
            if charge.unit_amount is None:
                continue
            drift = charge.unit_amount - allowance.overage_unit_rate
            if drift <= 0:
                continue

            overcharged = money(drift * charge.quantity)
            if overcharged <= RATE_DRIFT_TOLERANCE:
                continue

            findings.append(
                Anomaly(
                    type=AnomalyType.RATE_DRIFT,
                    line_id=line_id,
                    account_id=ctx.account_id,
                    summary=(
                        f"Line {line_id} was billed {charge.unit_amount} per unit of overage "
                        f"against a contracted {allowance.overage_unit_rate}, overcharging "
                        f"{overcharged} on this invoice."
                    ),
                    evidence=[
                        Evidence(claim="Overage unit rate charged",
                                 value=str(charge.unit_amount),
                                 source=f"invoice {AuditContext.period(ctx.invoice)}"),
                        Evidence(claim="Overage unit rate contracted",
                                 value=str(allowance.overage_unit_rate), source="contract"),
                        Evidence(claim="Units billed at the drifted rate",
                                 value=f"{charge.quantity:g}",
                                 source=f"invoice {AuditContext.period(ctx.invoice)}"),
                    ],
                    confidence=0.97,
                    recovered_amount=overcharged,
                    months_affected=1,
                )
            )

    return findings
