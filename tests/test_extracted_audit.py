"""The end-to-end claim: extraction good enough, findings still exact.

test_dataset_audit.py proves the rule engine is right about invoices that are
perfect by construction. scripts/eval_extraction.py measures how close the model
gets to perfect. This is the join between them, and it is the only test that
covers the failure the other two cannot see: an invoice read at 99.5% accuracy
producing a different audit than the same invoice read at 100%.

That failure would be quiet. A single misread `included` value turns a
chronic-overage finding into silence, and both other suites would stay green.

Runs against the committed extractions in data/extracted, so it needs no
credentials and costs no tokens - regenerate them with:

    python -m scripts.eval_extraction --cache data/extracted --refresh
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from scripts import extraction_cache
from scripts.eval_audit import audit_account, compare, cycles_from_cache

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET = REPO_ROOT / "data" / "synthetic"
EXTRACTED = REPO_ROOT / "data" / "extracted"


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    return json.loads((DATASET / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cache() -> dict:
    cached = extraction_cache.load_all(EXTRACTED)
    if not cached:
        pytest.skip(
            f"no extractions in {EXTRACTED} - run "
            "`python -m scripts.eval_extraction --cache data/extracted`"
        )
    return cached


@pytest.fixture(scope="module")
def audits(ground_truth: dict, cache: dict) -> dict:
    """One audit per account, run over the extracted invoices."""
    return {
        account["account_id"]: audit_account(account, cycles_from_cache(account, cache))
        for account in ground_truth["accounts"]
    }


def accounts(ground_truth: dict) -> list[dict]:
    return ground_truth["accounts"]


def test_every_invoice_was_extracted(ground_truth, cache):
    """A partial cache would make every assertion below weaker than it looks."""
    for account in ground_truth["accounts"]:
        for entry in account["invoices"]:
            assert entry["content_hash"] in cache, entry["file"]


def test_extraction_preserves_every_finding(ground_truth, audits):
    """Nothing planted goes missing once a model is doing the reading."""
    for account in ground_truth["accounts"]:
        outcome = compare(account["expected_anomalies"], audits[account["account_id"]])
        assert not outcome["missed"], (account["account_id"], outcome["missed"])


def test_extraction_preserves_every_amount(ground_truth, audits):
    """Finding the anomaly is not enough - a disputed amount that is off by two
    reais is a rejected dispute, not a partial success."""
    for account in ground_truth["accounts"]:
        outcome = compare(account["expected_anomalies"], audits[account["account_id"]])
        assert not outcome["wrong_amount"], (
            account["account_id"],
            outcome["wrong_amount"],
        )


def test_no_finding_is_invented_from_a_misread(ground_truth, audits):
    """A misread value inventing a finding is worse than one hiding a finding:
    it puts a claim the customer cannot defend in front of their carrier."""
    for account in ground_truth["accounts"]:
        outcome = compare(account["expected_anomalies"], audits[account["account_id"]])
        assert not outcome["false_positives"], (
            account["account_id"],
            outcome["false_positives"],
        )


def test_control_account_stays_clean_on_extracted_invoices(ground_truth, audits):
    """The false-positive control, now with the model in the loop."""
    control = next(a for a in ground_truth["accounts"] if not a["expected_anomalies"])
    assert audits[control["account_id"]] == []


def test_recovered_total_matches_to_the_cent(ground_truth, audits):
    total = sum(
        (anomaly.recovered_amount for findings in audits.values() for anomaly in findings),
        Decimal(0),
    )
    assert total == Decimal(ground_truth["totals"]["expected_recovery_total"])
