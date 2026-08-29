"""The auditor's tool layer, offline.

These tests are about what the tools refuse. The agent is a language model, and
the guarantees that matter here are the ones no prompt can talk its way past:

  * no tool accepts a monetary amount, so no amount can be invented;
  * an unknown finding id is rejected, not improvised into a finding;
  * a charge billed exactly as contracted cannot be sent to the carrier as a
    dispute, however convincingly the model argues for it.

The last one is the difference between a consultancy a carrier takes seriously
and one it learns to ignore.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from invoice_sentinel import audit_tools
from invoice_sentinel.anomaly import Anomaly, AnomalyType, Evidence, Remedy, remedy_for
from invoice_sentinel.audit_tools import (
    STATE_DECISIONS,
    STATE_FINDINGS,
    dismiss_finding,
    finding_id,
    flag_anomaly,
    list_findings,
    recommend_account_action,
)


class FakeToolContext:
    """Just the state bag - the only part of ToolContext these tools touch."""

    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}


def anomaly(
    kind: AnomalyType,
    line_id: str = "11987650101",
    amount: str = "100.00",
    confidence: float = 0.9,
) -> Anomaly:
    return Anomaly(
        type=kind,
        line_id=line_id,
        account_id="ACC-BR-1041",
        summary=f"{kind.value} on {line_id}",
        evidence=[Evidence(claim="something", value="1", source="invoice 2026-07")],
        confidence=confidence,
        recovered_amount=Decimal(amount),
        months_affected=4,
    )


@pytest.fixture
def context() -> FakeToolContext:
    findings = [
        anomaly(AnomalyType.RATE_DRIFT, "11987650101", "20.00", 0.97),
        anomaly(AnomalyType.ZOMBIE_LINE, "11987650103", "239.60", 0.90),
    ]
    return FakeToolContext(
        {STATE_FINDINGS: {finding_id(a): a.model_dump(mode="json") for a in findings}}
    )


# --- The guarantee: no amount enters through a tool --------------------------


@pytest.mark.parametrize(
    "tool",
    [flag_anomaly, recommend_account_action, dismiss_finding, audit_tools.escalate_for_review],
)
def test_no_tool_accepts_a_monetary_argument(tool):
    """The central rule of this project, enforced at the only surface where a
    model could break it."""
    banned = {"amount", "recovered_amount", "value", "total", "saving", "money"}
    parameters = set(inspect.signature(tool).parameters)
    assert not parameters & banned, (tool.__name__, parameters & banned)


def test_flagging_reports_the_engines_amount_not_a_supplied_one(context):
    result = flag_anomaly("rate_drift:11987650101", "Contract says 89.90.", context)
    assert result["status"] == "flagged_for_dispute"
    assert result["recovered_amount"] == "20.00"


# --- Unknown ids are refused -------------------------------------------------


def test_an_unknown_finding_is_refused_with_the_real_options(context):
    result = flag_anomaly("rate_drift:99999", "looks wrong", context)
    assert result["status"] == "unknown_finding"
    assert "zombie_line:11987650103" in result["available"]
    assert not context.state.get(STATE_DECISIONS)


def test_a_refused_call_records_no_decision(context):
    dismiss_finding("made_up:12345", "nothing here", context)
    assert not context.state.get(STATE_DECISIONS)


# --- Dispute and optimisation are not interchangeable ------------------------


def test_every_anomaly_type_has_a_remedy():
    for kind in AnomalyType:
        assert remedy_for(kind) in (Remedy.DISPUTE, Remedy.OPTIMISE)


def test_a_correctly_billed_charge_cannot_be_disputed(context):
    """A dormant line was billed exactly as contracted. Demanding a refund for
    it would be rejected and would weaken the claims that are real."""
    result = flag_anomaly("zombie_line:11987650103", "nobody uses it", context)

    assert result["status"] == "not_disputable"
    assert result["use_instead"] == "recommend_account_action"
    assert not context.state.get(STATE_DECISIONS)


def test_a_carrier_error_is_not_a_plan_change(context):
    """The customer cannot fix an overcharge by changing their own plan."""
    result = recommend_account_action(
        "rate_drift:11987650101", "downgrade", "cheaper plan", context
    )

    assert result["status"] == "not_an_optimisation"
    assert result["use_instead"] == "flag_anomaly"
    assert not context.state.get(STATE_DECISIONS)


def test_an_optimisation_records_the_monthly_saving(context):
    result = recommend_account_action(
        "zombie_line:11987650103", "cancel", "no traffic in four cycles", context
    )

    assert result["status"] == "recommended"
    assert result["action"] == "cancel"
    # 239.60 over four cycles, computed by the engine.
    assert result["monthly_saving"] == "59.90"


# --- Totals stay deterministic ----------------------------------------------


def test_totals_split_carrier_debt_from_customer_waste(context):
    flag_anomaly("rate_drift:11987650101", "rate does not match", context)
    recommend_account_action("zombie_line:11987650103", "cancel", "unused", context)

    assert audit_tools.disputed_total(context.state) == Decimal("20.00")
    assert audit_tools.optimisation_total(context.state) == Decimal("239.60")


def test_undecided_findings_count_towards_neither_total(context):
    flag_anomaly("rate_drift:11987650101", "rate does not match", context)

    assert audit_tools.disputed_total(context.state) == Decimal("20.00")
    assert audit_tools.optimisation_total(context.state) == Decimal(0)


# --- What the model is shown -------------------------------------------------


def test_findings_are_listed_with_their_remedy(context):
    listed = list_findings(context)["findings"]
    remedies = {item["finding_id"]: item["remedy"] for item in listed}

    assert remedies["rate_drift:11987650101"] == "dispute"
    assert remedies["zombie_line:11987650103"] == "optimise"


def test_a_decision_shows_up_on_the_next_listing(context):
    flag_anomaly("rate_drift:11987650101", "rate does not match", context)

    listed = {item["finding_id"]: item for item in list_findings(context)["findings"]}
    assert listed["rate_drift:11987650101"]["decided"] == "dispute"
    assert listed["zombie_line:11987650103"]["decided"] is None


def test_an_empty_audit_lists_nothing():
    assert list_findings(FakeToolContext())["findings"] == []
