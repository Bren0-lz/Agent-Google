"""Generate the synthetic invoice dataset and its ground truth.

    python -m scripts.synthetic.generate

Writes, under data/synthetic/:
    invoices/<account>-<period>.pdf   the documents the extractor will read
    contracts/<account>.json          the contracted truth, for Firestore seeding
    ground_truth.json                 expected extraction + expected findings

The run is seeded, so the same seed reproduces the same dataset byte for byte —
which is what lets content hashes in ground_truth.json stay meaningful.
"""

from __future__ import annotations

import argparse
import datetime
import json
import random
from decimal import Decimal
from pathlib import Path

from invoice_sentinel.anomaly import AnomalyType
from invoice_sentinel.schema import (
    ChargeCategory,
    ExtractedInvoice,
    UsageMetric,
    content_hash,
)

from .render import render_invoice
from .scenarios import AccountScenario, PlantedAnomaly, all_scenarios, build_invoice, money

DEFAULT_OUT = Path("data/synthetic")
DEFAULT_SEED = 20260826


def _cheapest_plan_covering(scenario: AccountScenario, consumed_mb: Decimal):
    """The cheapest contracted plan whose data allowance covers this consumption.

    This is the counterfactual every plan-sizing finding is measured against.
    """
    candidates = []
    for plan in scenario.plans:
        allowance = plan.allowance_for(UsageMetric.DATA_MB)
        if allowance is not None and allowance.included >= consumed_mb:
            candidates.append(plan)
    if not candidates:
        return None
    return min(candidates, key=lambda p: p.monthly_rate)


def _chronic_overage_recovery(
    scenario: AccountScenario, invoices: list[ExtractedInvoice], line_id: str
) -> Decimal:
    """What the customer overpaid by staying on a plan they always exceed.

    Per cycle: what they actually paid for the line (subscription + overage)
    minus what the right-sized contracted plan would have cost. Never negative
    — a cycle where the current plan happened to win is not a recovery.
    """
    total = Decimal(0)
    for invoice in invoices:
        paid = sum(
            (
                charge.amount
                for charge in invoice.charges
                if charge.line_id == line_id
                and charge.category in (ChargeCategory.SUBSCRIPTION, ChargeCategory.USAGE)
            ),
            Decimal(0),
        )
        consumed = next(
            (
                record.consumed
                for record in invoice.usage_records
                if record.line_id == line_id and record.metric is UsageMetric.DATA_MB
            ),
            Decimal(0),
        )
        alternative = _cheapest_plan_covering(scenario, consumed)
        if alternative is None:
            continue
        total += max(paid - alternative.monthly_rate, Decimal(0))
    return money(total)


def _resolve_recovery(
    planted: PlantedAnomaly, scenario: AccountScenario, invoices: list[ExtractedInvoice]
) -> Decimal:
    """Fill in recoveries that depend on the generated (random) consumption."""
    if planted.type is AnomalyType.CHRONIC_OVERAGE:
        return _chronic_overage_recovery(scenario, invoices, planted.line_id)
    return planted.recovered_amount


def _sanity_check(scenario: AccountScenario, invoices: list[ExtractedInvoice]) -> list[str]:
    """Catch a dataset that quietly contradicts its own ground truth.

    A control account that accidentally grows an overage, or a total that does
    not match its own charges, would silently corrupt every metric measured
    against this dataset later.
    """
    problems: list[str] = []
    planted_lines = {p.line_id for p in scenario.planted}

    for invoice in invoices:
        period = f"{invoice.header.billing_period_end:%Y-%m}"

        warnings = invoice.consistency_warnings()
        if warnings:
            problems.append(f"{scenario.account_id} {period}: {warnings}")

        for record in invoice.usage_records:
            if record.metric is not UsageMetric.DATA_MB:
                continue
            if record.overage > 0 and record.line_id not in planted_lines:
                problems.append(
                    f"{scenario.account_id} {period}: unplanned overage on line "
                    f"{record.line_id} ({record.overage} MB) — this line is supposed to be clean"
                )
    return problems


def _serialise_anomaly(planted: PlantedAnomaly, recovered: Decimal) -> dict:
    return {
        "type": planted.type.value,
        "line_id": planted.line_id,
        "months_affected": planted.months_affected,
        "recovered_amount": format(recovered, "f"),
        "rationale": planted.rationale,
    }


def generate(out_dir: Path, seed: int) -> dict:
    invoices_dir = out_dir / "invoices"
    contracts_dir = out_dir / "contracts"
    invoices_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    ground_truth: dict = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "seed": seed,
        "disclaimer": "Fully synthetic. No real customer data. Generated by "
                      "scripts/synthetic/generate.py.",
        "accounts": [],
    }

    problems: list[str] = []
    pdf_count = 0
    american_count = 0
    total_recovery = Decimal(0)

    for scenario in all_scenarios():
        # One RNG per account, seeded from the run seed and the account id, so
        # adding an account does not reshuffle every other account's history.
        rng = random.Random(f"{seed}:{scenario.account_id}")

        invoices = [build_invoice(scenario, period, rng) for period in scenario.periods]
        problems.extend(_sanity_check(scenario, invoices))

        contract = scenario.contract()
        contract_path = contracts_dir / f"{scenario.account_id}.json"
        contract_path.write_text(contract.model_dump_json(indent=2), encoding="utf-8")

        invoice_entries = []
        for index, invoice in enumerate(invoices):
            period = f"{invoice.header.billing_period_end:%Y-%m}"
            pdf_path = invoices_dir / f"{scenario.account_id}-{period}.pdf"
            pdf_bytes = render_invoice(invoice, scenario.profile, pdf_path)
            pdf_count += 1
            if scenario.profile.country != "BR":
                american_count += 1

            invoice_entries.append(
                {
                    "file": pdf_path.relative_to(out_dir).as_posix(),
                    "period": period,
                    "content_hash": content_hash(pdf_bytes),
                    "bytes": len(pdf_bytes),
                    # The last cycle is what the auditor runs against; the
                    # earlier ones exist to make its history real.
                    "is_audit_target": index == len(invoices) - 1,
                    "expected_invoice": json.loads(invoice.model_dump_json()),
                }
            )

        resolved = [(p, _resolve_recovery(p, scenario, invoices)) for p in scenario.planted]
        account_recovery = sum((amount for _, amount in resolved), Decimal(0))
        total_recovery += account_recovery

        ground_truth["accounts"].append(
            {
                "account_id": scenario.account_id,
                "customer": scenario.customer,
                "carrier": scenario.profile.carrier_name,
                "country": scenario.profile.country,
                "currency": scenario.profile.currency,
                "profile_key": scenario.profile.profile_key,
                "note": scenario.note,
                "contract_file": contract_path.relative_to(out_dir).as_posix(),
                "contract": json.loads(contract.model_dump_json()),
                "invoices": invoice_entries,
                "expected_anomalies": [_serialise_anomaly(p, a) for p, a in resolved],
                "expected_recovery_total": format(money(account_recovery), "f"),
            }
        )

    ground_truth["totals"] = {
        "accounts": len(ground_truth["accounts"]),
        "invoices": pdf_count,
        "american_invoices": american_count,
        "expected_anomalies": sum(
            len(a["expected_anomalies"]) for a in ground_truth["accounts"]
        ),
        "expected_recovery_total": format(money(total_recovery), "f"),
    }

    (out_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if problems:
        raise SystemExit(
            "Dataset contradicts its own ground truth:\n  " + "\n  ".join(problems)
        )

    return ground_truth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    result = generate(args.out, args.seed)
    totals = result["totals"]

    print(f"Wrote {totals['invoices']} invoices "
          f"({totals['american_invoices']} American) across {totals['accounts']} accounts")
    for account in result["accounts"]:
        findings = ", ".join(
            f"{a['type']}({a['recovered_amount']})" for a in account["expected_anomalies"]
        ) or "clean"
        print(f"  {account['account_id']:<14} {account['country']}  "
              f"{len(account['invoices'])} cycles  {findings}")
    print(f"Expected recoverable total: {totals['expected_recovery_total']}")
    print(f"Ground truth: {(args.out / 'ground_truth.json')}")


if __name__ == "__main__":
    main()
