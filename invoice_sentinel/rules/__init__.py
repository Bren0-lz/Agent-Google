"""The rule engine: five deterministic families of billing error.

Rules are grouped into families that can run concurrently — this is the seam
the auditor's ParallelAgent uses, and the unit `run_rule_family` exposes as a
tool. Grouping is by the question being asked, not by convenience:

    unused_capacity        paying for what nobody uses
    excess_usage           paying penalties for a plan that was always too small
    contract_conformance   the invoice disagreeing with the contract

`run_all_rules` is the only place that resolves conflicts between families and
decides what a human needs to see. Individual rules stay ignorant of each other.
"""

from __future__ import annotations

from ..anomaly import Anomaly, AnomalyType
from ..config import ESCALATION_CONFIDENCE_THRESHOLD
from .base import AuditContext, Rule, money, trailing_streak
from .conformance import orphan_addon, rate_drift
from .excess_usage import chronic_overage
from .unused_capacity import plan_tier_mismatch, zombie_line

RULE_FAMILIES: dict[str, tuple[Rule, ...]] = {
    "unused_capacity": (zombie_line, plan_tier_mismatch),
    "excess_usage": (chronic_overage,),
    "contract_conformance": (orphan_addon, rate_drift),
}

#: Flat view, for callers that just want everything.
ALL_RULES: tuple[Rule, ...] = tuple(
    rule for family in RULE_FAMILIES.values() for rule in family
)


def run_rule_family(family: str, ctx: AuditContext) -> list[Anomaly]:
    """Run one family. Raises on an unknown name rather than returning nothing.

    A tool that silently returns zero findings for a typo would let the auditor
    conclude the account is clean when nothing actually ran.
    """
    try:
        rules = RULE_FAMILIES[family]
    except KeyError:
        raise ValueError(
            f"unknown rule family {family!r}; expected one of {sorted(RULE_FAMILIES)}"
        ) from None
    return [anomaly for rule in rules for anomaly in rule(ctx)]


def _suppress_redundant(findings: list[Anomaly]) -> list[Anomaly]:
    """Drop findings that a stronger finding on the same line already covers.

    A dormant line should be cancelled, not moved to a smaller plan, and the two
    recoveries overlap — reporting both would double-count the money and hand
    the carrier an easy reason to reject the whole dispute.
    """
    zombie_lines = {
        anomaly.line_id for anomaly in findings if anomaly.type is AnomalyType.ZOMBIE_LINE
    }
    return [
        anomaly
        for anomaly in findings
        if not (
            anomaly.type is AnomalyType.PLAN_TIER_MISMATCH and anomaly.line_id in zombie_lines
        )
    ]


def run_all_rules(ctx: AuditContext) -> list[Anomaly]:
    """Every rule, deduplicated, ranked by money, flagged for review where unsure.

    Returned highest-recovery first, because that is the order a human reviewer
    and a dispute letter both want.
    """
    findings = [
        anomaly for family in RULE_FAMILIES for anomaly in run_rule_family(family, ctx)
    ]
    findings = _suppress_redundant(findings)

    for anomaly in findings:
        anomaly.needs_human_review = anomaly.confidence < ESCALATION_CONFIDENCE_THRESHOLD

    findings.sort(key=lambda anomaly: anomaly.recovered_amount, reverse=True)
    return findings


__all__ = [
    "ALL_RULES",
    "RULE_FAMILIES",
    "AuditContext",
    "Rule",
    "chronic_overage",
    "money",
    "orphan_addon",
    "plan_tier_mismatch",
    "rate_drift",
    "run_all_rules",
    "run_rule_family",
    "trailing_streak",
    "zombie_line",
]
