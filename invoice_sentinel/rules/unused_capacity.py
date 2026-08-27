"""Rules about capacity the customer pays for and does not use.

ZombieLine and PlanTierMismatch are close cousins — both are "you are buying
more than you need" — which is why they share a family and why the orchestrator
suppresses the second when the first fires. Right-sizing a line nobody uses is
the wrong advice; the advice is to cancel it.
"""

from __future__ import annotations

from decimal import Decimal

from ..anomaly import Anomaly, AnomalyType, Evidence
from ..config import (
    PATTERN_CYCLES,
    TIER_HEADROOM,
    TIER_MAX_UTILISATION,
    ZOMBIE_DATA_MB,
    ZOMBIE_VOICE_MIN,
)
from ..schema import ChargeCategory, ExtractedInvoice, UsageMetric
from .base import AuditContext, money, trailing_streak

#: Charges that disappear if the line is cancelled. The discount goes with it,
#: which is why it belongs in the total the customer would actually save.
_LINE_COST = (ChargeCategory.SUBSCRIPTION, ChargeCategory.ADDON, ChargeCategory.DISCOUNT)


def _gb(megabytes: Decimal) -> str:
    return f"{megabytes / Decimal(1024):.2f} GB"


def zombie_line(ctx: AuditContext) -> list[Anomaly]:
    """Billed every cycle, used by nobody.

    Fires when a line carries a recurring charge and effectively no traffic for
    at least PATTERN_CYCLES consecutive cycles ending with the audited invoice.
    """
    findings: list[Anomaly] = []

    for line_id in ctx.line_ids():

        def dormant(invoice: ExtractedInvoice, line_id: str = line_id) -> bool:
            data = ctx.usage_for(invoice, line_id, UsageMetric.DATA_MB)
            if data is None:
                return False
            if ctx.billed_amount(invoice, line_id, _LINE_COST) <= 0:
                return False  # not billed this cycle — nothing to recover
            voice = ctx.usage_for(invoice, line_id, UsageMetric.VOICE_MIN)
            return data.consumed <= ZOMBIE_DATA_MB and (
                voice is None or voice.consumed <= ZOMBIE_VOICE_MIN
            )

        streak = trailing_streak(ctx.cycles, dormant)
        if streak < PATTERN_CYCLES:
            continue

        affected = ctx.cycles[-streak:]
        recovered = money(
            sum((ctx.billed_amount(inv, line_id, _LINE_COST) for inv in affected), Decimal(0))
        )
        if recovered <= 0:
            continue

        peak_data = max(
            (
                usage.consumed
                for inv in affected
                if (usage := ctx.usage_for(inv, line_id, UsageMetric.DATA_MB)) is not None
            ),
            default=Decimal(0),
        )
        peak_voice = max(
            (
                usage.consumed
                for inv in affected
                if (usage := ctx.usage_for(inv, line_id, UsageMetric.VOICE_MIN)) is not None
            ),
            default=Decimal(0),
        )

        confidence = 0.85
        if streak > PATTERN_CYCLES:
            confidence += 0.05
        if peak_data == 0 and peak_voice == 0:
            confidence += 0.05

        window = f"{AuditContext.period(affected[0])}..{AuditContext.period(affected[-1])}"
        findings.append(
            Anomaly(
                type=AnomalyType.ZOMBIE_LINE,
                line_id=line_id,
                account_id=ctx.account_id,
                summary=(
                    f"Line {line_id} has been billed for {streak} consecutive cycles with "
                    f"effectively no usage, costing {recovered} over the period."
                ),
                evidence=[
                    Evidence(claim="Highest data usage in any affected cycle",
                             value=f"{peak_data:.0f} MB", source=f"usage records {window}"),
                    Evidence(claim="Highest voice usage in any affected cycle",
                             value=f"{peak_voice:.0f} min", source=f"usage records {window}"),
                    Evidence(claim="Recurring charges billed for the line",
                             value=f"{recovered} over {streak} cycles", source=f"invoices {window}"),
                    Evidence(claim="Dormancy threshold applied",
                             value=f"<= {ZOMBIE_DATA_MB} MB and <= {ZOMBIE_VOICE_MIN} min per cycle",
                             source="audit policy"),
                ],
                confidence=round(confidence, 2),
                recovered_amount=recovered,
                months_affected=streak,
            )
        )

    return findings


def plan_tier_mismatch(ctx: AuditContext) -> list[Anomaly]:
    """Paying for an allowance that is never approached.

    Requires consumption under TIER_MAX_UTILISATION of the allowance in every
    cycle of the streak, and a cheaper contracted plan that still covers peak
    usage with TIER_HEADROOM to spare. Both conditions matter: a plan that is
    merely half used is not waste, and moving someone onto a plan they would
    immediately exceed trades one finding for another.
    """
    findings: list[Anomaly] = []

    for line_id in ctx.line_ids():

        def underused(invoice: ExtractedInvoice, line_id: str = line_id) -> bool:
            data = ctx.usage_for(invoice, line_id, UsageMetric.DATA_MB)
            if data is None or data.included <= 0:
                return False
            if ctx.billed_amount(invoice, line_id, (ChargeCategory.SUBSCRIPTION,)) <= 0:
                return False
            return data.consumed <= data.included * TIER_MAX_UTILISATION

        streak = trailing_streak(ctx.cycles, underused)
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

        target = ctx.cheapest_plan_covering(peak * TIER_HEADROOM)
        if target is None:
            continue

        paid = sum(
            (ctx.billed_amount(inv, line_id, (ChargeCategory.SUBSCRIPTION,)) for inv in affected),
            Decimal(0),
        )
        alternative = target.monthly_rate * streak
        recovered = money(paid - alternative)
        if recovered <= 0:
            continue  # the cheaper allowance is not actually cheaper

        utilisation = peak / allowance
        confidence = 0.85
        if streak > PATTERN_CYCLES:
            confidence += 0.05
        if utilisation <= Decimal("0.10"):
            confidence += 0.05

        window = f"{AuditContext.period(affected[0])}..{AuditContext.period(affected[-1])}"
        current_plan = next(
            (line.plan_name for line in ctx.invoice.service_lines if line.line_id == line_id),
            "current plan",
        )
        findings.append(
            Anomaly(
                type=AnomalyType.PLAN_TIER_MISMATCH,
                line_id=line_id,
                account_id=ctx.account_id,
                summary=(
                    f"Line {line_id} is on {current_plan} but has used at most "
                    f"{utilisation:.0%} of its allowance for {streak} cycles. "
                    f"{target.plan_name} covers that usage and would have saved {recovered}."
                ),
                evidence=[
                    Evidence(claim="Peak data usage across the affected cycles",
                             value=_gb(peak), source=f"usage records {window}"),
                    Evidence(claim="Allowance being paid for",
                             value=_gb(allowance), source=f"invoice {AuditContext.period(affected[-1])}"),
                    Evidence(claim="Paid for the plan over the period",
                             value=f"{money(paid)} over {streak} cycles", source=f"invoices {window}"),
                    Evidence(claim=f"Contracted alternative ({target.plan_name})",
                             value=f"{target.monthly_rate} per cycle", source="contract"),
                    Evidence(claim="Headroom kept when right-sizing",
                             value=f"{TIER_HEADROOM}x peak usage", source="audit policy"),
                ],
                confidence=round(confidence, 2),
                recovered_amount=recovered,
                months_affected=streak,
            )
        )

    return findings
