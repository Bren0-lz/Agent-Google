"""Rules about consumption the plan was never sized for.

The mirror image of PlanTierMismatch: instead of paying for headroom nobody
touches, the customer pays overage every single cycle for an allowance that has
been too small all along.
"""

from __future__ import annotations

from decimal import Decimal

from ..anomaly import Anomaly, AnomalyType, Evidence
from ..config import PATTERN_CYCLES
from ..schema import ChargeCategory, ExtractedInvoice, UsageMetric
from .base import AuditContext, money, trailing_streak

#: What the line actually costs to run at its current size: the plan, the
#: overage it keeps incurring, and any discount that offsets them.
_RUNNING_COST = (ChargeCategory.SUBSCRIPTION, ChargeCategory.USAGE, ChargeCategory.DISCOUNT)


def chronic_overage(ctx: AuditContext) -> list[Anomaly]:
    """Blowing past the allowance every cycle, when a bigger plan costs less.

    The recommendation is pinned to peak consumption rather than re-decided each
    cycle: telling a customer to switch plans monthly is not advice, and the
    plan has to cover the worst month to be worth moving to.

    Overage is only a finding when it is also expensive. A line that exceeds its
    allowance by a few megabytes costs less than the upgrade, and the rule stays
    silent about it.
    """
    findings: list[Anomaly] = []

    for line_id in ctx.line_ids():

        def over_allowance(invoice: ExtractedInvoice, line_id: str = line_id) -> bool:
            usage = ctx.usage_for(invoice, line_id, UsageMetric.DATA_MB)
            return usage is not None and usage.overage > 0

        streak = trailing_streak(ctx.cycles, over_allowance)
        if streak < PATTERN_CYCLES:
            continue

        affected = ctx.cycles[-streak:]
        usages = [
            usage
            for inv in affected
            if (usage := ctx.usage_for(inv, line_id, UsageMetric.DATA_MB)) is not None
        ]
        peak = max(usage.consumed for usage in usages)
        allowance = usages[-1].included

        target = ctx.cheapest_plan_covering(peak)
        if target is None:
            continue  # nothing in the contract is big enough; not a plan problem

        paid = sum(
            (ctx.billed_amount(inv, line_id, _RUNNING_COST) for inv in affected), Decimal(0)
        )
        alternative = target.monthly_rate * streak
        recovered = money(paid - alternative)
        if recovered <= 0:
            continue  # the overage is cheaper than the upgrade — leave it alone

        overage_total = money(
            sum(
                (
                    ctx.billed_amount(inv, line_id, (ChargeCategory.USAGE,))
                    for inv in affected
                ),
                Decimal(0),
            )
        )

        confidence = 0.85
        if streak > PATTERN_CYCLES:
            confidence += 0.05
        # A recovery that dwarfs a single cycle's plan fee is not a marginal call.
        if recovered >= target.monthly_rate:
            confidence += 0.05

        window = f"{AuditContext.period(affected[0])}..{AuditContext.period(affected[-1])}"
        findings.append(
            Anomaly(
                type=AnomalyType.CHRONIC_OVERAGE,
                line_id=line_id,
                account_id=ctx.account_id,
                summary=(
                    f"Line {line_id} exceeded its allowance in {streak} consecutive cycles, "
                    f"paying {overage_total} in overage. Moving to {target.plan_name} would "
                    f"have cost {recovered} less over the same period."
                ),
                evidence=[
                    Evidence(claim="Cycles with overage, consecutive",
                             value=str(streak), source=f"usage records {window}"),
                    Evidence(claim="Peak data usage against an allowance of "
                                   f"{allowance / Decimal(1024):.2f} GB",
                             value=f"{peak / Decimal(1024):.2f} GB",
                             source=f"usage records {window}"),
                    Evidence(claim="Overage actually billed",
                             value=str(overage_total), source=f"invoices {window}"),
                    Evidence(claim="Total paid to run the line at its current size",
                             value=f"{money(paid)} over {streak} cycles", source=f"invoices {window}"),
                    Evidence(claim=f"Contracted alternative ({target.plan_name})",
                             value=f"{target.monthly_rate} per cycle, "
                                   f"{money(alternative)} over {streak} cycles",
                             source="contract"),
                ],
                confidence=round(confidence, 2),
                recovered_amount=recovered,
                months_affected=streak,
            )
        )

    return findings
