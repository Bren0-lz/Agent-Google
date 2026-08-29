"""Does the rule engine still work on what the model actually read?

Everything measured so far tests one half of the pipeline in isolation.
tests/test_dataset_audit.py runs the engine over `expected_invoice` - the
objects the fixtures were rendered from, perfect by construction.
scripts/eval_extraction.py measures how closely the model reproduces those
objects. Neither answers the question the whole system rests on:

    an invoice as the model read it - 99.5%, not 100% - does it still yield
    the same anomalies, for the same money, on the same lines?

That gap is where a project like this fails quietly. A single misread
`included` turns a chronic-overage finding into silence, and 99.5% extraction
accuracy would still be reported as a success. So this runs the engine over the
extracted invoices and compares the findings to the planted ones.

The comparison is strict on the things a dispute letter would cite: type, line,
money to the cent, cycles affected. Money especially - "we found the anomaly but
the amount is off by two reais" is a lost dispute, not a partial success.

    python -m scripts.eval_audit --cache data/extracted
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from invoice_sentinel.anomaly import Anomaly
from invoice_sentinel.contract import Contract
from invoice_sentinel.rules import AuditContext, run_all_rules
from invoice_sentinel.schema import CanonicalInvoice, ExtractedInvoice
from scripts import extraction_cache

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET = REPO_ROOT / "data" / "synthetic"


# --- Building the audit ------------------------------------------------------


def cycles_from_cache(account: dict, cache: dict[str, CanonicalInvoice]) -> list[ExtractedInvoice]:
    """The account's invoices as the model read them, oldest first.

    Ordered by the ground truth's own listing rather than by anything the model
    produced: a cycle mis-sorted by a misread date would silently change what
    "the trailing three cycles" means, and that belongs in the findings as an
    error, not in the setup as a shuffle.
    """
    cycles = []
    for entry in account["invoices"]:
        canonical = cache.get(entry["content_hash"])
        if canonical is None:
            raise KeyError(entry["file"])
        cycles.append(canonical.invoice)
    return cycles


def audit_account(account: dict, cycles: list[ExtractedInvoice]) -> list[Anomaly]:
    """Same shape as tests/test_dataset_audit.py: last cycle audited, rest is history."""
    return run_all_rules(
        AuditContext(
            invoice=cycles[-1],
            contract=Contract.model_validate(account["contract"]),
            history=cycles[:-1],
        )
    )


# --- Matching findings to planted anomalies ----------------------------------


def _key(anomaly_type: str, line_id: str | None) -> tuple[str, str | None]:
    return (anomaly_type, line_id)


def compare(expected: list[dict], found: list[Anomaly]) -> dict:
    """Match findings to planted anomalies by (type, line), then check the money.

    Matching on type and line rather than on amount is deliberate: it lets a
    finding be reported as "found, wrong amount" instead of vanishing into both
    a false negative and a false positive, which would misstate what broke.
    """
    by_key = {_key(anomaly.type.value, anomaly.line_id): anomaly for anomaly in found}
    matched, missed, wrong_amount = [], [], []

    for planted in expected:
        key = _key(planted["type"], planted["line_id"])
        anomaly = by_key.pop(key, None)
        if anomaly is None:
            missed.append(planted)
            continue

        expected_amount = Decimal(planted["recovered_amount"])
        record = {
            "type": planted["type"],
            "line_id": planted["line_id"],
            "expected_amount": planted["recovered_amount"],
            "actual_amount": format(anomaly.recovered_amount, "f"),
            "expected_months": planted["months_affected"],
            "actual_months": anomaly.months_affected,
        }
        if anomaly.recovered_amount != expected_amount:
            wrong_amount.append(record)
        else:
            matched.append(record)

    # Whatever is left never had a planted counterpart.
    false_positives = [
        {
            "type": anomaly.type.value,
            "line_id": anomaly.line_id,
            "amount": format(anomaly.recovered_amount, "f"),
            "summary": anomaly.summary,
        }
        for anomaly in by_key.values()
    ]

    return {
        "matched": matched,
        "wrong_amount": wrong_amount,
        "missed": missed,
        "false_positives": false_positives,
    }


# --- Runner ------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache",
        type=Path,
        default=REPO_ROOT / "data" / "extracted",
        help="Directory of extractions from `eval_extraction --cache`",
    )
    parser.add_argument(
        "--ground-truth",
        action="store_true",
        help="Audit the perfect expected_invoice objects instead of the extracted "
        "ones, to separate an extraction problem from a rule problem",
    )
    parser.add_argument("--report", type=Path, help="Write the full JSON report here")
    args = parser.parse_args()

    truth = json.loads((DATASET / "ground_truth.json").read_text(encoding="utf-8"))
    cache = {} if args.ground_truth else extraction_cache.load_all(args.cache)

    if not args.ground_truth and not cache:
        print(
            f"No extractions in {args.cache}. Run:\n"
            f"  python -m scripts.eval_extraction --cache {args.cache}",
            file=sys.stderr,
        )
        return 2

    source_label = "ground truth" if args.ground_truth else "extracted invoices"
    print(f"  Auditing {source_label}\n")

    results = []
    for account in truth["accounts"]:
        if args.ground_truth:
            cycles = [
                ExtractedInvoice.model_validate(entry["expected_invoice"])
                for entry in account["invoices"]
            ]
        else:
            try:
                cycles = cycles_from_cache(account, cache)
            except KeyError as missing:
                print(f"  {account['account_id']}: not extracted ({missing}) - skipped")
                continue

        found = audit_account(account, cycles)
        outcome = compare(account["expected_anomalies"], found)
        outcome["account_id"] = account["account_id"]
        outcome["recovered_total"] = format(
            sum((a.recovered_amount for a in found), Decimal(0)), "f"
        )
        outcome["expected_total"] = account["expected_recovery_total"]
        results.append(outcome)

        planted = len(account["expected_anomalies"])
        clean = "control account" if not planted else f"{len(outcome['matched'])}/{planted} exact"
        flags = []
        if outcome["wrong_amount"]:
            flags.append(f"{len(outcome['wrong_amount'])} wrong amount")
        if outcome["missed"]:
            flags.append(f"{len(outcome['missed'])} missed")
        if outcome["false_positives"]:
            flags.append(f"{len(outcome['false_positives'])} false positive")
        suffix = ("  <- " + ", ".join(flags)) if flags else ""
        print(f"  {account['account_id']:14} {clean}{suffix}")

    return _report(results, truth, args.report)


def _report(results: list[dict], truth: dict, report_path: Path | None) -> int:
    planted = sum(len(r["matched"]) + len(r["wrong_amount"]) + len(r["missed"]) for r in results)
    matched = sum(len(r["matched"]) for r in results)
    wrong = sum(len(r["wrong_amount"]) for r in results)
    missed = sum(len(r["missed"]) for r in results)
    false_positives = sum(len(r["false_positives"]) for r in results)

    detected = matched + wrong
    recall = detected / planted if planted else 0.0
    precision = detected / (detected + false_positives) if detected + false_positives else 0.0

    recovered = sum((Decimal(r["recovered_total"]) for r in results), Decimal(0))
    expected_total = Decimal(truth["totals"]["expected_recovery_total"])

    print()
    print(f"  anomalies found  : {detected}/{planted}   (recall {recall:.1%})")
    print(f"  exact on money   : {matched}/{planted}")
    print(f"  false positives  : {false_positives}    (precision {precision:.1%})")
    print(f"  recovered total  : {recovered} vs {expected_total} expected")

    for result in results:
        for item in result["wrong_amount"]:
            print(
                f"\n  AMOUNT OFF {result['account_id']} {item['type']} line {item['line_id']}"
                f"\n    expected {item['expected_amount']}, computed {item['actual_amount']}"
                f"  ({item['expected_months']} vs {item['actual_months']} cycles)"
            )
        for item in result["missed"]:
            print(f"\n  MISSED {result['account_id']} {item['type']} line {item['line_id']}")
        for item in result["false_positives"]:
            print(
                f"\n  FALSE POSITIVE {result['account_id']} {item['type']} "
                f"line {item['line_id']} ({item['amount']})\n    {item['summary']}"
            )

    if report_path:
        report_path.write_text(
            json.dumps(
                {
                    "summary": {
                        "planted": planted,
                        "detected": detected,
                        "exact_on_money": matched,
                        "wrong_amount": wrong,
                        "missed": missed,
                        "false_positives": false_positives,
                        "recall": round(recall, 4),
                        "precision": round(precision, 4),
                        "recovered_total": format(recovered, "f"),
                        "expected_recovery_total": format(expected_total, "f"),
                    },
                    "accounts": results,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"\n  report written to {report_path}")

    # The claim this project makes: every planted anomaly, exact money, nothing invented.
    return 0 if matched == planted and not false_positives else 1


if __name__ == "__main__":
    raise SystemExit(main())
