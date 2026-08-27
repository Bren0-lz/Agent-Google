"""Unit tests for the five anomaly rules.

Each rule gets a case that must fire and a case that must not. The negative
cases carry most of the weight: a rule that finds everything is worthless to a
customer who has to defend each finding to their carrier, and a false positive
in a dispute letter costs more credibility than a missed anomaly costs money.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from invoice_sentinel.anomaly import AnomalyType
from invoice_sentinel.config import PATTERN_CYCLES
from invoice_sentinel.rules import (
    AuditContext,
    chronic_overage,
    orphan_addon,
    plan_tier_mismatch,
    rate_drift,
    run_all_rules,
    run_rule_family,
    zombie_line,
)
from tests.conftest import GB, LineInput, build_contract, build_cycle, periods

LINE = "11900000001"


def context(cycles, contract) -> AuditContext:
    return AuditContext(invoice=cycles[-1], contract=contract, history=cycles[:-1])


def scenario(line_inputs_per_cycle, contract) -> AuditContext:
    """Build a context from one LineInput list per cycle, oldest first."""
    dates = periods(len(line_inputs_per_cycle))
    cycles = [build_cycle(date, lines) for date, lines in zip(dates, line_inputs_per_cycle)]
    return context(cycles, contract)


# --- ZombieLine --------------------------------------------------------------


def test_zombie_line_fires_when_billed_and_unused(tiers):
    contract = build_contract(plans=tiers, lines={LINE: "Small 5GB"})
    dormant = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, Decimal(2),
                        voice_consumed=Decimal(1))
    ctx = scenario([[dormant]] * 4, contract)

    findings = zombie_line(ctx)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.type is AnomalyType.ZOMBIE_LINE
    assert finding.line_id == LINE
    assert finding.months_affected == 4
    assert finding.recovered_amount == Decimal("239.60")   # 59.90 x 4
    assert finding.evidence, "a finding with no evidence cannot be disputed"


def test_zombie_line_ignores_a_line_that_is_actually_used(tiers):
    contract = build_contract(plans=tiers, lines={LINE: "Small 5GB"})
    busy = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, Decimal(3000),
                     voice_consumed=Decimal(400))

    assert zombie_line(scenario([[busy]] * 4, contract)) == []


def test_zombie_line_waits_for_the_pattern_to_establish(tiers):
    """Two dormant cycles is a quiet month, not a dead line."""
    contract = build_contract(plans=tiers, lines={LINE: "Small 5GB"})
    dormant = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, Decimal(0))

    short = scenario([[dormant]] * (PATTERN_CYCLES - 1), contract)
    assert zombie_line(short) == []


def test_zombie_line_requires_the_dormancy_to_reach_the_audited_cycle(tiers):
    """A line dormant last year but busy now is not a zombie."""
    contract = build_contract(plans=tiers, lines={LINE: "Small 5GB"})
    dormant = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, Decimal(0))
    busy = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, Decimal(3000),
                     voice_consumed=Decimal(300))

    assert zombie_line(scenario([[dormant]] * 3 + [[busy]], contract)) == []


# --- PlanTierMismatch --------------------------------------------------------


def test_plan_tier_mismatch_fires_on_a_plan_never_approached(tiers):
    contract = build_contract(plans=tiers, lines={LINE: "Large 20GB"})
    # 1 GB against a 20 GB allowance; the 5 GB tier covers it with headroom.
    oversized = LineInput(LINE, "Large 20GB", Decimal("129.90"), 20 * GB, GB)
    ctx = scenario([[oversized]] * 4, contract)

    findings = plan_tier_mismatch(ctx)

    assert len(findings) == 1
    assert findings[0].recovered_amount == Decimal("280.00")   # (129.90 - 59.90) x 4
    assert findings[0].months_affected == 4


def test_plan_tier_mismatch_leaves_a_comfortably_used_plan_alone(tiers):
    """Half-used is not wasteful. Only a plan nobody approaches is."""
    contract = build_contract(plans=tiers, lines={LINE: "Medium 10GB"})
    reasonable = LineInput(LINE, "Medium 10GB", Decimal("89.90"), 10 * GB, 5 * GB)

    assert plan_tier_mismatch(scenario([[reasonable]] * 4, contract)) == []


def test_plan_tier_mismatch_respects_the_utilisation_bar(tiers):
    """40% of the allowance is real use, even though a cheaper plan would fit.

    Without this bar the rule would nag customers to shave every plan down to
    the smallest one their peak happens to fit in, which is how a tool starts
    producing findings nobody acts on.
    """
    contract = build_contract(plans=tiers, lines={LINE: "Large 20GB"})
    # 8 GB of 20 GB. The 10 GB tier would cover it and cost less — but 40% is
    # above the bar, so this is a plan being used, not a plan being wasted.
    used = LineInput(LINE, "Large 20GB", Decimal("129.90"), 20 * GB, 8 * GB)

    assert plan_tier_mismatch(scenario([[used]] * 4, contract)) == []


def test_plan_tier_mismatch_keeps_headroom_when_right_sizing(tiers):
    """Usage at 4.5 GB fits inside 5 GB, but not with the headroom margin.

    Recommending that move would create a chronic overage next quarter.
    """
    contract = build_contract(plans=tiers, lines={LINE: "Large 20GB"})
    # 22% of a 20 GB allowance — under the utilisation bar, but 4.5 x 1.25 > 5 GB.
    borderline = LineInput(LINE, "Large 20GB", Decimal("129.90"), 20 * GB,
                           Decimal(int(Decimal("4.5") * GB)))
    findings = plan_tier_mismatch(scenario([[borderline]] * 4, contract))

    assert len(findings) == 1
    # Right-sized to 10GB rather than 5GB, so the saving is smaller.
    assert findings[0].recovered_amount == Decimal("160.00")   # (129.90 - 89.90) x 4


# --- ChronicOverage ----------------------------------------------------------


def test_chronic_overage_fires_when_the_bigger_plan_is_cheaper(tiers):
    contract = build_contract(plans=tiers, lines={LINE: "Medium 10GB"})
    # 15 GB on a 10 GB plan: 5120 MB overage at 0.02 = 102.40 on top of 89.90.
    bursting = LineInput(LINE, "Medium 10GB", Decimal("89.90"), 10 * GB, 15 * GB)
    ctx = scenario([[bursting]] * 3, contract)

    findings = chronic_overage(ctx)

    assert len(findings) == 1
    # Paid 192.30/cycle; Large 20GB costs 129.90. Saving 62.40 x 3.
    assert findings[0].recovered_amount == Decimal("187.20")
    assert findings[0].months_affected == 3


def test_chronic_overage_stays_silent_when_the_upgrade_costs_more(tiers):
    """A few megabytes over is cheaper than moving up a tier."""
    contract = build_contract(plans=tiers, lines={LINE: "Medium 10GB"})
    # 100 MB overage = 2.00, against a 40.00 price difference to the next tier.
    marginal = LineInput(LINE, "Medium 10GB", Decimal("89.90"), 10 * GB, 10 * GB + 100)

    assert chronic_overage(scenario([[marginal]] * 4, contract)) == []


def test_chronic_overage_needs_consecutive_cycles(tiers):
    """One clean month breaks the streak — that is seasonality, not a wrong plan."""
    contract = build_contract(plans=tiers, lines={LINE: "Medium 10GB"})
    bursting = LineInput(LINE, "Medium 10GB", Decimal("89.90"), 10 * GB, 15 * GB)
    within = LineInput(LINE, "Medium 10GB", Decimal("89.90"), 10 * GB, 6 * GB)

    assert chronic_overage(scenario([[bursting], [bursting], [within], [bursting]], contract)) == []


# --- OrphanAddon -------------------------------------------------------------


def test_orphan_addon_fires_when_the_contract_grants_it_to_another_line(tiers):
    contract = build_contract(
        plans=tiers, lines={LINE: "Small 5GB"},
        addons=[("Roaming Pack", Decimal("19.99"), ["11900000999"])],
    )
    billed = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, GB,
                       addons=[("Roaming Pack", Decimal("19.99"))])
    ctx = scenario([[billed]] * 4, contract)

    findings = orphan_addon(ctx)

    assert len(findings) == 1
    assert findings[0].recovered_amount == Decimal("79.96")   # 19.99 x 4
    assert findings[0].months_affected == 4


def test_orphan_addon_accepts_an_entitled_addon(tiers):
    contract = build_contract(
        plans=tiers, lines={LINE: "Small 5GB"},
        addons=[("Roaming Pack", Decimal("19.99"), [LINE])],
    )
    billed = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, GB,
                       addons=[("Roaming Pack", Decimal("19.99"))])

    assert orphan_addon(scenario([[billed]] * 4, contract)) == []


def test_orphan_addon_fires_on_the_first_cycle(tiers):
    """An add-on nobody agreed to is wrong immediately, unlike the pattern rules."""
    contract = build_contract(plans=tiers, lines={LINE: "Small 5GB"}, addons=[])
    billed = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, GB,
                       addons=[("Mystery Service", Decimal("12.00"))])

    findings = orphan_addon(scenario([[billed]], contract))

    assert len(findings) == 1
    assert findings[0].months_affected == 1
    assert findings[0].recovered_amount == Decimal("12.00")


# --- RateDrift ---------------------------------------------------------------


def test_rate_drift_fires_when_the_billed_rate_exceeds_the_contract(tiers):
    contract = build_contract(plans=tiers, lines={LINE: "Medium 10GB"})
    overbilled = LineInput(LINE, "Medium 10GB", Decimal("94.90"), 10 * GB, 4 * GB)
    ctx = scenario([[overbilled]] * 4, contract)

    findings = rate_drift(ctx)

    assert len(findings) == 1
    assert findings[0].recovered_amount == Decimal("20.00")   # 5.00 x 4
    assert findings[0].confidence >= 0.95, "two numbers that must match is the surest finding"


def test_rate_drift_accepts_the_contracted_rate(tiers):
    contract = build_contract(plans=tiers, lines={LINE: "Medium 10GB"})
    correct = LineInput(LINE, "Medium 10GB", Decimal("89.90"), 10 * GB, 4 * GB)

    assert rate_drift(scenario([[correct]] * 4, contract)) == []


def test_rate_drift_does_not_report_an_undercharge(tiers):
    """Being billed less than agreed is the carrier's problem, not a dispute."""
    contract = build_contract(plans=tiers, lines={LINE: "Medium 10GB"})
    underbilled = LineInput(LINE, "Medium 10GB", Decimal("79.90"), 10 * GB, 4 * GB)

    assert rate_drift(scenario([[underbilled]] * 4, contract)) == []


def test_rate_drift_catches_an_inflated_overage_unit_rate(tiers):
    contract = build_contract(plans=tiers, lines={LINE: "Medium 10GB"},
                              overage_rate=Decimal("0.02"))
    # Billed at 0.05/MB against a contracted 0.02 on 1024 MB of overage.
    inflated = LineInput(LINE, "Medium 10GB", Decimal("89.90"), 10 * GB, 11 * GB,
                         overage_rate=Decimal("0.05"))

    findings = [f for f in rate_drift(scenario([[inflated]] * 3, contract))
                if "per unit" in f.summary]

    assert len(findings) == 1
    assert findings[0].recovered_amount == Decimal("30.72")   # 0.03 x 1024


# --- Orchestration -----------------------------------------------------------


def test_a_dormant_line_is_not_also_reported_as_oversized(tiers):
    """The advice is to cancel the line, not to move it to a smaller plan.

    Both rules would otherwise fire on the same money, and a dispute that
    double-counts hands the carrier a reason to reject all of it.
    """
    contract = build_contract(plans=tiers, lines={LINE: "Large 20GB"})
    dormant = LineInput(LINE, "Large 20GB", Decimal("129.90"), 20 * GB, Decimal(1))
    ctx = scenario([[dormant]] * 4, contract)

    assert {f.type for f in plan_tier_mismatch(ctx)} == {AnomalyType.PLAN_TIER_MISMATCH}
    assert {f.type for f in run_all_rules(ctx)} == {AnomalyType.ZOMBIE_LINE}


def test_findings_come_back_ranked_by_money(tiers):
    contract = build_contract(
        plans=tiers, lines={"1": "Small 5GB", "2": "Large 20GB"},
        addons=[("Roaming Pack", Decimal("19.99"), ["999"])],
    )
    lines = [
        LineInput("1", "Small 5GB", Decimal("59.90"), 5 * GB, Decimal(1),
                  addons=[("Roaming Pack", Decimal("19.99"))]),
        LineInput("2", "Large 20GB", Decimal("129.90"), 20 * GB, GB),
    ]
    findings = run_all_rules(scenario([lines] * 4, contract))

    amounts = [f.recovered_amount for f in findings]
    assert amounts == sorted(amounts, reverse=True)
    assert len(findings) >= 2


def test_unknown_rule_family_is_an_error_not_an_empty_result(tiers):
    """Silently returning nothing would read as 'this account is clean'."""
    contract = build_contract(plans=tiers, lines={LINE: "Small 5GB"})
    ctx = scenario([[LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, GB)]], contract)

    with pytest.raises(ValueError, match="unknown rule family"):
        run_rule_family("does_not_exist", ctx)


def test_low_confidence_findings_are_flagged_for_a_human(tiers):
    """An add-on on a suspended line is a judgement call, not a certainty."""
    from invoice_sentinel.schema import LineStatus

    contract = build_contract(
        plans=tiers, lines={LINE: "Small 5GB"},
        addons=[("Roaming Pack", Decimal("19.99"), [LINE])],
    )
    suspended = LineInput(LINE, "Small 5GB", Decimal("59.90"), 5 * GB, Decimal(0),
                          status=LineStatus.SUSPENDED,
                          addons=[("Roaming Pack", Decimal("19.99"))])
    findings = orphan_addon(scenario([[suspended]] * 4, contract))

    assert len(findings) == 1
    assert findings[0].confidence < 0.85
