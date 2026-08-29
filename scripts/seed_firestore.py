"""Load the synthetic account into Firestore so the auditor has something to read.

The auditor's tools never read the filesystem: get_contract and
get_usage_history go to Firestore, the same way they will in production when a
PDF arrives from Pub/Sub. So the dataset has to be there before any of it works.

Contracts come from data/synthetic/contracts/. Invoice history comes from the
extraction cache rather than from the ground truth, deliberately - seeding the
perfect objects would give the auditor a cleaner history than the pipeline
actually produces, and every downstream measurement would be flattered by it.

    python -m scripts.eval_extraction --cache data/extracted   # first
    python -m scripts.seed_firestore
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from invoice_sentinel import config, store
from invoice_sentinel.contract import Contract
from scripts import extraction_cache

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET = REPO_ROOT / "data" / "synthetic"


def seed_contracts(client=None) -> int:
    contracts = sorted((DATASET / "contracts").glob("*.json"))
    for path in contracts:
        contract = Contract.model_validate_json(path.read_text(encoding="utf-8"))
        store.save_contract(contract, client=client)
        print(f"  contract  {contract.account_id:14} {len(contract.lines)} line(s)")
    return len(contracts)


def seed_invoices(cache_dir: Path, client=None) -> tuple[int, int]:
    cached = extraction_cache.load_all(cache_dir)
    created = 0
    for canonical in sorted(
        cached.values(), key=lambda record: record.invoice.header.billing_period_end
    ):
        _, was_created = store.save_invoice(canonical, client=client)
        created += was_created
        header = canonical.invoice.header
        status = "created" if was_created else "already there"
        print(f"  invoice   {header.account_id:14} {header.billing_period_end:%Y-%m}  {status}")
    return len(cached), created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache",
        type=Path,
        default=REPO_ROOT / "data" / "extracted",
        help="Extractions to seed as invoice history",
    )
    parser.add_argument(
        "--contracts-only",
        action="store_true",
        help="Skip invoice history, e.g. when Pub/Sub will deliver the PDFs",
    )
    args = parser.parse_args()

    print(f"  project {config.PROJECT_ID}\n")
    contracts = seed_contracts()

    invoices = created = 0
    if not args.contracts_only:
        if not args.cache.exists():
            print(
                f"\nNo extraction cache at {args.cache}. Run:\n"
                f"  python -m scripts.eval_extraction --cache {args.cache}",
                file=sys.stderr,
            )
            return 2
        invoices, created = seed_invoices(args.cache)

    print(f"\n  {contracts} contract(s), {invoices} invoice(s) ({created} newly written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
