"""The rule engine against the full synthetic dataset.

The unit tests prove each rule behaves on a fixture built to provoke it. This
proves the engine finds exactly the planted anomalies in fifteen invoices it
was not tuned against — same money, same lines, same number of cycles — and
finds nothing in the control account.

Ground truth was produced by the generator, which computes its expected
recoveries with its own arithmetic. Four of the five figures are fixed
constants written into the scenario. The chronic-overage figure depends on
randomly generated consumption, so both sides compute it — independently, from
opposite directions — and the test is that they agree.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_sentinel.contract import Contract
from invoice_sentinel.rules import AuditContext, run_all_rules
from invoice_sentinel.schema import ExtractedInvoice

GROUND_TRUTH = Path("data/synthetic/ground_truth.json")


def load_ground_truth() -> dict:
    if not GROUND_TRUTH.exists():
        pytest.skip(f"{GROUND_TRUTH} missing — run `python -m scripts.synthetic.generate`")
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    return load_ground_truth()


def audit(account: dict) -> list:
    """Run the engine over one account's latest cycle, with the rest as history."""
    invoices = [ExtractedInvoice.model_validate(i["expected_invoice"]) for i in account["invoices"]]
    return run_all_rules(
        AuditContext(
            invoice=invoices[-1],
            contract=Contract.model_validate(account["contract"]),
            history=invoices[:-1],
        )
    )


def accounts(ground_truth: dict) -> list[dict]:
    return ground_truth["accounts"]


def test_dataset_covers_all_five_rules(ground_truth):
    """A dataset that never exercises a rule cannot validate it."""
    planted = {
        anomaly["type"]
        for account in accounts(ground_truth)
        for anomaly in account["expected_anomalies"]
    }
    assert planted == {
        "zombie_line", "plan_tier_mismatch", "chronic_overage", "orphan_addon", "rate_drift"
    }


@pytest.mark.parametrize("account_id", [
    "ACC-BR-1041", "ACC-BR-2087", "ACC-BR-3312", "ACC-US-77120",
])
def test_engine_finds_exactly_the_planted_anomalies(ground_truth, account_id):
    account = next(a for a in accounts(ground_truth) if a["account_id"] == account_id)
    findings = audit(account)

    found = {(f.type.value, f.line_id): f for f in findings}
    expected = {(a["type"], a["line_id"]): a for a in account["expected_anomalies"]}

    assert set(found) == set(expected), (
        f"{account_id}: missed {sorted(set(expected) - set(found))}, "
        f"false positives {sorted(set(found) - set(expected))}"
    )

    for key, anomaly in expected.items():
        finding = found[key]
        assert finding.recovered_amount == Decimal(anomaly["recovered_amount"]), key
        assert finding.months_affected == anomaly["months_affected"], key


def test_control_account_stays_clean(ground_truth):
    """The account with nothing wrong must produce nothing.

    This is the false-positive measurement. It includes a legitimate loyalty
    discount, which is the kind of negative line a careless rule misreads.
    """
    control = next(a for a in accounts(ground_truth) if a["account_id"] == "ACC-BR-3312")
    assert control["expected_anomalies"] == [], "fixture drift: the control account has findings"
    assert audit(control) == []


def test_american_invoices_audit_identically(ground_truth):
    """The engine never learns which country it is auditing.

    Layout, currency and number format are the extractor's problem. If a rule
    needed to know, the abstraction would have leaked into the engine.
    """
    american = [a for a in accounts(ground_truth) if a["country"] == "US"]
    assert american, "the dataset is supposed to contain an American carrier"

    for account in american:
        findings = audit(account)
        assert len(findings) == len(account["expected_anomalies"])


def test_total_recovery_matches_ground_truth(ground_truth):
    """The headline number. This is what goes in the README and the video."""
    recovered = sum(
        (finding.recovered_amount for account in accounts(ground_truth) for finding in audit(account)),
        Decimal(0),
    )
    assert recovered == Decimal(ground_truth["totals"]["expected_recovery_total"])


def test_every_finding_carries_checkable_evidence(ground_truth):
    """A number with no evidence behind it cannot be disputed with a carrier."""
    for account in accounts(ground_truth):
        for finding in audit(account):
            assert finding.evidence, f"{finding.type} on {finding.line_id} has no evidence"
            assert any(e.source == "contract" or "invoice" in e.source or "usage" in e.source
                       for e in finding.evidence), "evidence must cite where it came from"
            assert finding.recovered_amount > 0
            assert finding.summary.strip()


def test_confident_findings_are_not_sent_to_a_human(ground_truth):
    """Escalating everything is the same as escalating nothing.

    Every planted anomaly is unambiguous, so none of them should land in the
    review queue. A rule that hedges on a clear case wastes the reviewer.
    """
    for account in accounts(ground_truth):
        for finding in audit(account):
            assert not finding.needs_human_review, (
                f"{finding.type.value} on {finding.line_id} escalated at "
                f"confidence {finding.confidence}"
            )
