"""Measure extraction accuracy against the synthetic ground truth.

"It validated" is a weak claim: a JSON document can satisfy every validator and
still have read 89.90 as 8990. Because every fixture PDF was rendered from an
ExtractedInvoice that was already validated, ground_truth.json holds the exact
object that went in - so accuracy can be measured field by field rather than by
counting documents that happened to parse.

Two numbers come out, and they answer different questions:

    schema validity   how often the model produces something the pipeline can
                      use at all
    field accuracy    how often it produces the right value

Run it against the local fixtures while iterating on the prompt; the Day-4 eval
suite reports the same numbers in the README.

    python -m scripts.eval_extraction
    python -m scripts.eval_extraction --account ACC-US-77120
    python -m scripts.eval_extraction --gcs gs://bucket/invoice.pdf --persist
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from invoice_sentinel.extractor import ExtractionFailed, InvoiceSource, extract_invoice
from invoice_sentinel.profiles import profile_for
from invoice_sentinel.schema import CanonicalInvoice

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET = REPO_ROOT / "data" / "synthetic"

#: Fields the ground truth carries that the renderer never puts on the page.
#:
#: Scoring a model on information absent from its input measures nothing, so
#: these are excluded from accuracy and reported on their own line rather than
#: quietly dropped. Each entry is a gap in the fixtures, not a concession to the
#: model - print the field on the invoice and the entry goes away.
#:
#: Per profile, because it depends on the layout: the Brazilian template has a
#: "Situacao" column, the American one has no status column at all.
#:
#:   assigned_to  set in the scenario spec, used nowhere in render.py. The page
#:                shows the line label ("Socio 01"), not the cost centre.
#:   status       printed by the BR template, absent from the US one.
#:
#: One related gap is deliberately NOT excluded here. The US taxes-and-fees
#: section prints only Description and Amount, so the Regulatory Recovery Fee
#: loses its quantity (4) and unit price (1.50), and the model correctly reads
#: 1 / null - eight scored errors it does not deserve. Excluding
#: charges[].quantity wholesale would also stop scoring the per-line Qty and
#: Rate columns, which the US template does print and which a misread would
#: matter for. The fix belongs in render.py, printing those two columns in the
#: fee section; until then these eight are the known residual.
_ALWAYS_UNPRINTED = frozenset({"service_lines[].assigned_to"})

UNPRINTED_FIELDS: dict[str, frozenset[str]] = {
    "br-vantel-empresas": _ALWAYS_UNPRINTED,
    "us-northwind-wireless": _ALWAYS_UNPRINTED | {"service_lines[].status"},
}


def _unprinted_for(profile_key: str) -> frozenset[str]:
    return UNPRINTED_FIELDS.get(profile_key, _ALWAYS_UNPRINTED)

#: Fields the page prints at lower precision than the ground truth holds.
#:
#: The renderer shows data volumes in GB to two decimals, so 6929 MB is printed
#: as "6.77 GB" and reads back as 6932.48 MB. That gap is the page rounding, not
#: the model misreading, and the tolerance is exactly the half-interval of the
#: last printed decimal: 0.005 GB = 5.12 MB. Wider than that is a real error and
#: still scores as one - a model that reads 1.5 GB as 15 GB is nowhere near it.
DISPLAY_TOLERANCE: dict[str, Decimal] = {
    "usage_records[].included": Decimal("5.12"),
    "usage_records[].consumed": Decimal("5.12"),
    "usage_records[].overage": Decimal("5.12"),
}


def _generalise(path: str) -> str:
    """service_lines[3].assigned_to -> service_lines[].assigned_to"""
    return re.sub(r"\[\d+\]", "[]", path)


# --- Comparison --------------------------------------------------------------


def _same_value(expected: Any, actual: Any, path: str = "") -> bool:
    """Compare two leaf values, treating numbers as the decimals they are.

    "89.90" and "89.9" are the same amount of money and it would be dishonest to
    score one of them wrong. A few fields additionally carry a display tolerance;
    money is never one of them and compares exactly.
    """
    if expected == actual:
        return True
    try:
        expected_number = Decimal(str(expected))
        actual_number = Decimal(str(actual))
    except (InvalidOperation, ValueError, TypeError):
        return False

    if expected_number == actual_number:
        return True

    tolerance = DISPLAY_TOLERANCE.get(_generalise(path))
    return tolerance is not None and abs(expected_number - actual_number) <= tolerance


def _sort_key(item: dict) -> tuple:
    """Order a list of records so two extractions line up for comparison.

    The model is not obliged to emit charges in the order they were printed, and
    penalising a correct transcription for its ordering would make the accuracy
    number mean something other than accuracy.
    """
    return tuple(
        str(item.get(field, ""))
        for field in ("line_id", "metric", "category", "period", "description")
    )


def diff(expected: Any, actual: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Walk two invoice payloads together, returning every leaf that differs."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        found = []
        for key in sorted(set(expected) | set(actual)):
            where = f"{path}.{key}" if path else key
            if key not in expected:
                found.append((where, None, actual[key]))
            elif key not in actual:
                found.append((where, expected[key], None))
            else:
                found.extend(diff(expected[key], actual[key], where))
        return found

    if isinstance(expected, list) and isinstance(actual, list):
        if expected and isinstance(expected[0], dict):
            expected = sorted(expected, key=_sort_key)
            actual = sorted(actual, key=_sort_key) if actual and isinstance(actual[0], dict) else actual
        found = []
        for index in range(max(len(expected), len(actual))):
            where = f"{path}[{index}]"
            if index >= len(expected):
                found.append((where, None, actual[index]))
            elif index >= len(actual):
                found.append((where, expected[index], None))
            else:
                found.extend(diff(expected[index], actual[index], where))
        return found

    return [] if _same_value(expected, actual, path) else [(path, expected, actual)]


def leaf_count(node: Any) -> int:
    """How many comparable values a payload holds, for the accuracy denominator."""
    if isinstance(node, dict):
        return sum(leaf_count(value) for value in node.values())
    if isinstance(node, list):
        return sum(leaf_count(value) for value in node)
    return 1


# --- Runner ------------------------------------------------------------------


def evaluate_one(entry: dict, account: dict, *, persist: bool) -> dict:
    profile = profile_for(account["profile_key"])
    source = InvoiceSource.from_path(DATASET / entry["file"])

    started = time.monotonic()
    try:
        canonical = extract_invoice(source, profile)
    except ExtractionFailed as failure:
        return {
            "file": entry["file"],
            "account_id": account["account_id"],
            "country": account["country"],
            "valid": False,
            "attempts": failure.attempts,
            "error": failure.repair_notes[-1] if failure.repair_notes else str(failure),
            "seconds": round(time.monotonic() - started, 2),
        }

    elapsed = round(time.monotonic() - started, 2)

    if persist:
        from invoice_sentinel import store

        canonical, created = store.save_invoice(canonical)
    else:
        created = None

    expected = entry["expected_invoice"]
    actual = json.loads(canonical.invoice.model_dump_json())

    unprinted = _unprinted_for(account["profile_key"])
    all_differences = diff(expected, actual)
    unprintable = [d for d in all_differences if _generalise(d[0]) in unprinted]
    differences = [d for d in all_differences if _generalise(d[0]) not in unprinted]
    total = leaf_count(expected) - len(unprintable)

    return {
        "file": entry["file"],
        "account_id": account["account_id"],
        "country": account["country"],
        "valid": True,
        "attempts": canonical.provenance.attempts,
        "content_hash_matches": canonical.provenance.content_hash == entry["content_hash"],
        "fields": total,
        "fields_wrong": len(differences),
        "fields_unprintable": len(unprintable),
        "accuracy": round(1 - len(differences) / total, 4) if total else 0.0,
        "warnings": canonical.provenance.warnings,
        "differences": [
            {"path": p, "expected": e, "actual": a} for p, e, a in differences[:25]
        ],
        "created": created,
        "seconds": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="Only this account id")
    parser.add_argument("--limit", type=int, help="Stop after N invoices")
    parser.add_argument(
        "--audit-targets-only",
        action="store_true",
        help="Only the last cycle of each account, the one the auditor runs on",
    )
    parser.add_argument("--gcs", help="Extract a single gs:// object instead of the dataset")
    parser.add_argument("--profile", help="Profile key, required with --gcs")
    parser.add_argument("--persist", action="store_true", help="Write results to Firestore")
    parser.add_argument("--report", type=Path, help="Write the full JSON report here")
    args = parser.parse_args()

    if args.gcs:
        return _run_single_gcs(args)

    truth = json.loads((DATASET / "ground_truth.json").read_text(encoding="utf-8"))
    results = []

    for account in truth["accounts"]:
        if args.account and account["account_id"] != args.account:
            continue
        for entry in account["invoices"]:
            if args.audit_targets_only and not entry["is_audit_target"]:
                continue
            if args.limit and len(results) >= args.limit:
                break
            print(f"  {entry['file']} ... ", end="", flush=True)
            result = evaluate_one(entry, account, persist=args.persist)
            results.append(result)
            if result["valid"]:
                print(
                    f"ok  {result['accuracy']:.1%} fields  "
                    f"({result['attempts']} call(s), {result['seconds']}s)"
                )
            else:
                print(f"FAILED after {result['attempts']} call(s)")

    return _report(results, truth, args.report)


def _run_single_gcs(args: argparse.Namespace) -> int:
    if not args.profile:
        print("--gcs requires --profile", file=sys.stderr)
        return 2
    source = InvoiceSource.from_gcs(args.gcs)
    canonical = extract_invoice(source, profile_for(args.profile))
    created = None
    if args.persist:
        from invoice_sentinel import store

        canonical, created = store.save_invoice(canonical)
    print(canonical.model_dump_json(indent=2))
    print(f"\ncontent_hash : {canonical.content_hash}")
    print(f"model calls  : {canonical.provenance.attempts}")
    print(f"firestore    : {'created' if created else 'already present' if created is False else 'not persisted'}")
    return 0


def _report(results: list[dict], truth: dict, report_path: Path | None) -> int:
    if not results:
        print("Nothing to evaluate.", file=sys.stderr)
        return 1

    valid = [r for r in results if r["valid"]]
    american = [r for r in valid if r["country"] != "BR"]
    fields = sum(r["fields"] for r in valid)
    wrong = sum(r["fields_wrong"] for r in valid)
    repaired = [r for r in valid if r["attempts"] > 1]

    print()
    print(f"  schema validity : {len(valid)}/{len(results)} invoices")
    print(f"  american layout : {len(american)} valid")
    print(f"  field accuracy  : {(1 - wrong / fields):.2%}  ({fields - wrong}/{fields} values)")
    print(f"  needed a repair : {len(repaired)}")
    unprintable = sum(r["fields_unprintable"] for r in valid)
    if unprintable:
        print(f"  not on the page : {unprintable} values excluded (see UNPRINTED_FIELDS)")
    print(f"  hashes match GT : {sum(1 for r in valid if r['content_hash_matches'])}/{len(valid)}")

    imperfect = [r for r in valid if r["fields_wrong"]]
    if imperfect:
        print("\n  where it went wrong:")
        for result in imperfect:
            print(f"    {result['file']}")
            for difference in result["differences"][:5]:
                print(
                    f"      {difference['path']}: expected {difference['expected']!r}, "
                    f"got {difference['actual']!r}"
                )

    for result in (r for r in results if not r["valid"]):
        print(f"\n  FAILED {result['file']}:\n    {result['error'][:400]}")

    if report_path:
        payload = {
            "summary": {
                "invoices": len(results),
                "valid": len(valid),
                "american_valid": len(american),
                "fields": fields,
                "fields_wrong": wrong,
                "fields_unprintable": sum(r["fields_unprintable"] for r in valid),
                "field_accuracy": round(1 - wrong / fields, 4) if fields else 0.0,
                "needed_repair": len(repaired),
            },
            "results": results,
        }
        report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  report written to {report_path}")

    # Day-1 completion bar from the handoff: at least 12 of 15 usable.
    return 0 if len(valid) >= min(12, len(results)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
