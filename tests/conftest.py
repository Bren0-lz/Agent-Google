"""Fixture builders for rule tests.

Rule tests need invoices that isolate one behaviour, which real-looking
fixtures make hard to read: the interesting fact ends up buried in forty lines
of plausible billing detail. These builders take the few numbers a rule
actually keys on and fill in everything else consistently.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from invoice_sentinel.contract import (
    Contract,
    ContractedAddon,
    ContractedAllowance,
    ContractedLine,
    ContractedPlan,
)
from invoice_sentinel.schema import (
    ChargeCategory,
    ChargeItem,
    ExtractedInvoice,
    InvoiceHeader,
    LineStatus,
    ServiceLine,
    UsageMetric,
    UsageRecord,
)

GB = Decimal(1024)
ACCOUNT = "ACC-TEST-0001"
CARRIER = "Test Telecom"


@dataclass
class LineInput:
    """One line's behaviour in one cycle, reduced to what the rules read."""

    line_id: str
    plan_name: str
    rate: Decimal
    included_mb: Decimal
    consumed_mb: Decimal
    overage_rate: Decimal = Decimal("0.02")
    status: LineStatus = LineStatus.ACTIVE
    voice_consumed: Decimal = Decimal(0)
    voice_included: Decimal = Decimal(1000)
    addons: list[tuple[str, Decimal]] = field(default_factory=list)


def month_end(year: int, month: int) -> datetime.date:
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def periods(count: int, *, last: tuple[int, int] = (2026, 7)) -> list[datetime.date]:
    """`count` consecutive month-ends, oldest first, ending at `last`."""
    year, month = last
    result = []
    for offset in reversed(range(count)):
        total = (year * 12 + month - 1) - offset
        result.append(month_end(total // 12, total % 12 + 1))
    return result


def build_cycle(period_end: datetime.date, lines: list[LineInput]) -> ExtractedInvoice:
    """One invoice, with charges and usage derived from the line inputs."""
    service_lines: list[ServiceLine] = []
    charges: list[ChargeItem] = []
    usage: list[UsageRecord] = []
    period_label = f"{period_end:%Y-%m}"

    for line in lines:
        service_lines.append(
            ServiceLine(line_id=line.line_id, label=f"Line {line.line_id}",
                        plan_name=line.plan_name, status=line.status)
        )
        charges.append(
            ChargeItem(line_id=line.line_id, category=ChargeCategory.SUBSCRIPTION,
                       description=f"Monthly plan — {line.plan_name}",
                       unit_amount=line.rate, amount=line.rate, period=period_label)
        )

        overage = max(line.consumed_mb - line.included_mb, Decimal(0))
        usage.append(
            UsageRecord(line_id=line.line_id, metric=UsageMetric.DATA_MB,
                        included=line.included_mb, consumed=line.consumed_mb, overage=overage)
        )
        if overage > 0:
            charges.append(
                ChargeItem(line_id=line.line_id, category=ChargeCategory.USAGE,
                           description=f"Data overage ({overage:.0f} MB)", quantity=overage,
                           unit_amount=line.overage_rate,
                           amount=(overage * line.overage_rate).quantize(Decimal("0.01")),
                           period=period_label)
            )

        usage.append(
            UsageRecord(line_id=line.line_id, metric=UsageMetric.VOICE_MIN,
                        included=line.voice_included, consumed=line.voice_consumed,
                        overage=Decimal(0))
        )

        for name, amount in line.addons:
            charges.append(
                ChargeItem(line_id=line.line_id, category=ChargeCategory.ADDON,
                           description=name, unit_amount=amount, amount=amount,
                           period=period_label)
            )

    total = sum((charge.amount for charge in charges), Decimal(0))
    return ExtractedInvoice(
        header=InvoiceHeader(
            carrier=CARRIER, account_id=ACCOUNT,
            billing_period_start=period_end.replace(day=1), billing_period_end=period_end,
            issue_date=period_end + datetime.timedelta(days=1),
            due_date=period_end + datetime.timedelta(days=10),
            currency="BRL", total_amount=total,
        ),
        service_lines=service_lines,
        charges=charges,
        usage_records=usage,
    )


def build_contract(
    *,
    plans: dict[str, tuple[str, Decimal]],
    lines: dict[str, str],
    addons: list[tuple[str, Decimal, list[str]]] | None = None,
    overage_rate: Decimal = Decimal("0.02"),
) -> Contract:
    """`plans` maps plan name to (monthly rate, included MB); `lines` maps line to plan."""
    return Contract(
        account_id=ACCOUNT,
        carrier=CARRIER,
        currency="BRL",
        effective_from=datetime.date(2025, 1, 1),
        plans=[
            ContractedPlan(
                plan_name=name,
                monthly_rate=Decimal(rate),
                allowances=[
                    ContractedAllowance(metric=UsageMetric.DATA_MB, included=included,
                                        overage_unit_rate=overage_rate)
                ],
            )
            for name, (rate, included) in plans.items()
        ],
        lines=[
            ContractedLine(line_id=line_id, plan_name=plan_name,
                           activated_on=datetime.date(2025, 2, 1))
            for line_id, plan_name in lines.items()
        ],
        addons=[
            ContractedAddon(addon_name=name, monthly_rate=rate, line_ids=line_ids)
            for name, rate, line_ids in (addons or [])
        ],
    )


#: A three-tier catalogue, enough for every plan-sizing rule to have somewhere
#: cheaper and somewhere bigger to point at.
TIERS = {
    "Small 5GB": ("59.90", 5 * GB),
    "Medium 10GB": ("89.90", 10 * GB),
    "Large 20GB": ("129.90", 20 * GB),
}


@pytest.fixture
def tiers() -> dict[str, tuple[str, Decimal]]:
    return dict(TIERS)
