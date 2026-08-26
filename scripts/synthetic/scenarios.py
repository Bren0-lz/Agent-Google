"""Synthetic accounts, contracts and the anomalies planted in them.

Ground truth is produced by construction, not annotated by hand: every invoice
is built from a CanonicalInvoice-shaped object and then rendered to PDF, so the
expected extraction is exactly what went in, and every `recovered_amount` below
is arithmetic the Day-2 rule engine has to reproduce independently.

Deliberate composition, per the dataset plan:
  * four billing cycles per account, so patterns needing three cycles are
    actually testable;
  * one entirely clean account, to measure false positives;
  * two accounts carrying two anomalies each, one carrying a single anomaly;
  * an American carrier alongside a Brazilian one, in a different layout.

All data is invented. No real customer information appears anywhere.
"""

from __future__ import annotations

import datetime
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from invoice_sentinel.anomaly import AnomalyType
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
    ExtractionProfile,
    InvoiceHeader,
    LineStatus,
    ServiceLine,
    UsageMetric,
    UsageRecord,
)

GB = Decimal(1024)


def money(value: Decimal | str | int) -> Decimal:
    """Round to cents the way a billing system does — half up, never banker's."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- Carrier profiles --------------------------------------------------------

VANTEL = ExtractionProfile(
    profile_key="br-vantel-empresas",
    carrier_name="Vantel Empresas",
    country="BR",
    currency="BRL",
    decimal_separator=",",
    thousands_separator=".",
    date_format="%d/%m/%Y",
    data_unit="GB",
    tax_labels=["ICMS", "FUST", "FUNTTEL"],
    prompt_hints=[
        "Amounts use a comma as decimal separator and a dot for thousands.",
        "Dates are DD/MM/YYYY.",
        "'Franquia' is the included allowance, 'Excedente' is overage.",
        "Taxes (ICMS, FUST, FUNTTEL) are account-level and have no line_id.",
    ],
)

NORTHWIND = ExtractionProfile(
    profile_key="us-northwind-wireless",
    carrier_name="Northwind Wireless",
    country="US",
    currency="USD",
    decimal_separator=".",
    thousands_separator=",",
    date_format="%m/%d/%Y",
    data_unit="GB",
    tax_labels=["Federal Universal Service Fund", "Regulatory Recovery Fee", "State Sales Tax"],
    prompt_hints=[
        "Amounts use a dot as decimal separator and a comma for thousands.",
        "Dates are MM/DD/YYYY.",
        "The usage summary appears before the charge detail.",
        "Taxes, fees and surcharges are account-level and have no line_id.",
    ],
)


# --- Scenario specification --------------------------------------------------


@dataclass
class LineSpec:
    """One service line and how it behaves across the billing cycles."""

    line_id: str
    label: str
    plan_name: str
    charged_rate: Decimal          # what the invoice actually bills per cycle
    included_mb: Decimal
    overage_rate: Decimal          # charged price per MB above the allowance
    usage: str                     # normal | zero | tiny | overage
    assigned_to: str | None = None
    status: LineStatus = LineStatus.ACTIVE
    voice_included: Decimal = Decimal(1000)
    addons: list[tuple[str, Decimal]] = field(default_factory=list)
    discount: tuple[str, Decimal] | None = None


@dataclass
class PlantedAnomaly:
    """An error deliberately introduced, with the recovery it is worth."""

    type: AnomalyType
    line_id: str
    months_affected: int
    recovered_amount: Decimal
    rationale: str


@dataclass
class AccountScenario:
    account_id: str
    customer: str
    profile: ExtractionProfile
    plans: list[ContractedPlan]
    lines: list[LineSpec]
    contracted_lines: list[ContractedLine]
    contracted_addons: list[ContractedAddon]
    periods: list[datetime.date]        # last day of each billing cycle
    planted: list[PlantedAnomaly]
    note: str

    def contract(self) -> Contract:
        return Contract(
            account_id=self.account_id,
            carrier=self.profile.carrier_name,
            currency=self.profile.currency,
            effective_from=datetime.date(2025, 1, 1),
            plans=self.plans,
            lines=self.contracted_lines,
            addons=self.contracted_addons,
        )


# --- Invoice construction ----------------------------------------------------


def _consumption(spec: LineSpec, rng: random.Random) -> Decimal:
    """Data consumed this cycle, in MB, according to the line's behaviour."""
    if spec.usage == "zero":
        # Not literally zero: a dormant SIM still emits a little background
        # traffic, and a rule that only fires on exact 0 would never fire in
        # production.
        return Decimal(rng.randint(0, 3))
    if spec.usage == "tiny":
        return (spec.included_mb * Decimal(rng.uniform(0.02, 0.06))).quantize(Decimal("1"))
    if spec.usage == "overage":
        return (spec.included_mb * Decimal(rng.uniform(1.28, 1.55))).quantize(Decimal("1"))
    return (spec.included_mb * Decimal(rng.uniform(0.35, 0.78))).quantize(Decimal("1"))


def _taxes(profile: ExtractionProfile, subtotal: Decimal, line_count: int) -> list[ChargeItem]:
    """Account-level taxes and fees, computed from the service subtotal.

    Anomaly recovery figures below are stated pre-tax. Taxes follow the
    disputed charge, so removing the charge removes its tax too — recovering
    them is a consequence of the dispute, not a separate claim.
    """
    if profile.country == "BR":
        return [
            ChargeItem(category=ChargeCategory.TAX, description="ICMS (20,00%)",
                       amount=money(subtotal * Decimal("0.20"))),
            ChargeItem(category=ChargeCategory.TAX, description="FUST (0,50%)",
                       amount=money(subtotal * Decimal("0.005"))),
            ChargeItem(category=ChargeCategory.TAX, description="FUNTTEL (0,30%)",
                       amount=money(subtotal * Decimal("0.003"))),
        ]
    return [
        ChargeItem(category=ChargeCategory.FEE,
                   description="Federal Universal Service Fund (3.20%)",
                   amount=money(subtotal * Decimal("0.032"))),
        ChargeItem(category=ChargeCategory.FEE, description="Regulatory Recovery Fee",
                   quantity=Decimal(line_count), unit_amount=money(Decimal("1.50")),
                   amount=money(Decimal("1.50") * line_count)),
        ChargeItem(category=ChargeCategory.TAX, description="State Sales Tax (6.50%)",
                   amount=money(subtotal * Decimal("0.065"))),
    ]


def build_invoice(
    scenario: AccountScenario, period_end: datetime.date, rng: random.Random
) -> ExtractedInvoice:
    """Assemble one cycle's invoice for an account."""
    profile = scenario.profile
    period_start = period_end.replace(day=1)
    issue_date = period_end + datetime.timedelta(days=1)
    due_date = issue_date + datetime.timedelta(days=9)
    period_label = f"{period_end:%Y-%m}"

    service_lines: list[ServiceLine] = []
    charges: list[ChargeItem] = []
    usage_records: list[UsageRecord] = []

    for spec in scenario.lines:
        service_lines.append(
            ServiceLine(
                line_id=spec.line_id,
                label=spec.label,
                plan_name=spec.plan_name,
                assigned_to=spec.assigned_to,
                status=spec.status,
            )
        )

        subscription_label = (
            "Assinatura mensal" if profile.country == "BR" else "Monthly plan charge"
        )
        charges.append(
            ChargeItem(
                line_id=spec.line_id,
                category=ChargeCategory.SUBSCRIPTION,
                description=f"{subscription_label} — {spec.plan_name}",
                unit_amount=money(spec.charged_rate),
                amount=money(spec.charged_rate),
                period=period_label,
            )
        )

        consumed = _consumption(spec, rng)
        overage = max(consumed - spec.included_mb, Decimal(0))
        usage_records.append(
            UsageRecord(line_id=spec.line_id, metric=UsageMetric.DATA_MB,
                        included=spec.included_mb, consumed=consumed, overage=overage)
        )
        if overage > 0:
            overage_label = (
                "Excedente de dados" if profile.country == "BR" else "Data overage"
            )
            charges.append(
                ChargeItem(
                    line_id=spec.line_id,
                    category=ChargeCategory.USAGE,
                    description=f"{overage_label} ({overage:.0f} MB)",
                    quantity=overage,
                    unit_amount=spec.overage_rate,
                    amount=money(overage * spec.overage_rate),
                    period=period_label,
                )
            )

        voice_used = (
            Decimal(rng.randint(0, 5)) if spec.usage == "zero"
            else Decimal(rng.randint(60, int(spec.voice_included * Decimal("0.8"))))
        )
        usage_records.append(
            UsageRecord(line_id=spec.line_id, metric=UsageMetric.VOICE_MIN,
                        included=spec.voice_included, consumed=voice_used, overage=Decimal(0))
        )

        for addon_name, addon_rate in spec.addons:
            charges.append(
                ChargeItem(
                    line_id=spec.line_id,
                    category=ChargeCategory.ADDON,
                    description=addon_name,
                    unit_amount=money(addon_rate),
                    amount=money(addon_rate),
                    period=period_label,
                )
            )

        if spec.discount is not None:
            label, value = spec.discount
            charges.append(
                ChargeItem(line_id=spec.line_id, category=ChargeCategory.DISCOUNT,
                           description=label, amount=money(-abs(value)), period=period_label)
            )

    subtotal = sum((c.amount for c in charges), Decimal(0))
    charges.extend(_taxes(profile, subtotal, len(scenario.lines)))
    total = sum((c.amount for c in charges), Decimal(0))

    return ExtractedInvoice(
        header=InvoiceHeader(
            carrier=profile.carrier_name,
            account_id=scenario.account_id,
            billing_period_start=period_start,
            billing_period_end=period_end,
            issue_date=issue_date,
            due_date=due_date,
            currency=profile.currency,
            total_amount=money(total),
        ),
        service_lines=service_lines,
        charges=charges,
        usage_records=usage_records,
    )


# --- The accounts ------------------------------------------------------------

PERIODS_4 = [
    datetime.date(2026, 4, 30),
    datetime.date(2026, 5, 31),
    datetime.date(2026, 6, 30),
    datetime.date(2026, 7, 31),
]
PERIODS_3 = PERIODS_4[1:]

_VANTEL_CATALOG = [
    ContractedPlan(plan_name="Vantel Corp 5GB", monthly_rate=Decimal("59.90"),
                   allowances=[ContractedAllowance(metric=UsageMetric.DATA_MB,
                                                   included=5 * GB, overage_unit_rate=Decimal("0.02"))]),
    ContractedPlan(plan_name="Vantel Corp 10GB", monthly_rate=Decimal("89.90"),
                   allowances=[ContractedAllowance(metric=UsageMetric.DATA_MB,
                                                   included=10 * GB, overage_unit_rate=Decimal("0.02"))]),
    ContractedPlan(plan_name="Vantel Corp 20GB", monthly_rate=Decimal("129.90"),
                   allowances=[ContractedAllowance(metric=UsageMetric.DATA_MB,
                                                   included=20 * GB, overage_unit_rate=Decimal("0.02"))]),
    ContractedPlan(plan_name="Vantel Corp 50GB", monthly_rate=Decimal("189.90"),
                   allowances=[ContractedAllowance(metric=UsageMetric.DATA_MB,
                                                   included=50 * GB, overage_unit_rate=Decimal("0.02"))]),
]

_NORTHWIND_CATALOG = [
    ContractedPlan(plan_name="Northwind Business 5GB", monthly_rate=Decimal("45.00"),
                   allowances=[ContractedAllowance(metric=UsageMetric.DATA_MB,
                                                   included=5 * GB, overage_unit_rate=Decimal("0.01"))]),
    ContractedPlan(plan_name="Northwind Business 15GB", monthly_rate=Decimal("65.00"),
                   allowances=[ContractedAllowance(metric=UsageMetric.DATA_MB,
                                                   included=15 * GB, overage_unit_rate=Decimal("0.01"))]),
    ContractedPlan(plan_name="Northwind Business Unlimited", monthly_rate=Decimal("85.00"),
                   allowances=[ContractedAllowance(metric=UsageMetric.DATA_MB,
                                                   included=100 * GB, overage_unit_rate=Decimal("0.01"))]),
]


def _aurora() -> AccountScenario:
    """Two anomalies: a line nobody uses, and a line that never fits its plan."""
    lines = [
        LineSpec("11987650101", "Operação 01", "Vantel Corp 10GB", Decimal("89.90"),
                 10 * GB, Decimal("0.02"), "normal", assigned_to="Logística"),
        LineSpec("11987650102", "Operação 02", "Vantel Corp 10GB", Decimal("89.90"),
                 10 * GB, Decimal("0.02"), "overage", assigned_to="Rota Sul"),
        LineSpec("11987650103", "Reserva Frota", "Vantel Corp 5GB", Decimal("59.90"),
                 5 * GB, Decimal("0.02"), "zero", assigned_to=None),
        LineSpec("11987650104", "Diretoria", "Vantel Corp 20GB", Decimal("129.90"),
                 20 * GB, Decimal("0.02"), "normal", assigned_to="Diretoria"),
        LineSpec("11987650105", "Operação 05", "Vantel Corp 5GB", Decimal("59.90"),
                 5 * GB, Decimal("0.02"), "normal", assigned_to="Pátio"),
    ]
    contracted = [
        ContractedLine(line_id=spec.line_id, plan_name=spec.plan_name,
                       activated_on=datetime.date(2025, 3, 1))
        for spec in lines
    ]
    return AccountScenario(
        account_id="ACC-BR-1041",
        customer="Aurora Logística Ltda",
        profile=VANTEL,
        plans=_VANTEL_CATALOG,
        lines=lines,
        contracted_lines=contracted,
        contracted_addons=[],
        periods=PERIODS_4,
        planted=[
            PlantedAnomaly(
                type=AnomalyType.ZOMBIE_LINE,
                line_id="11987650103",
                months_affected=4,
                # Four cycles of a subscription for a line that carries no traffic.
                recovered_amount=money(Decimal("59.90") * 4),
                rationale="Line 11987650103 billed at 59.90/cycle with effectively no data or "
                          "voice usage across all four cycles.",
            ),
            PlantedAnomaly(
                type=AnomalyType.CHRONIC_OVERAGE,
                line_id="11987650102",
                months_affected=4,
                # Overage is intrinsically variable, so the exact figure is
                # computed from the rendered invoices, not asserted here.
                recovered_amount=Decimal("0"),
                rationale="Line 11987650102 exceeded its 10GB allowance every cycle; the "
                          "contracted 20GB plan (129.90) would have cost less than 89.90 plus "
                          "overage. Recovery is computed from the generated invoices.",
            ),
        ],
        note="Two findings, one of them with a variable recovery amount.",
    )


def _meridiano() -> AccountScenario:
    """Two anomalies: an oversized plan, and a rate that drifted off contract."""
    lines = [
        # Contracted on 50GB, uses almost nothing — should be on the 5GB tier.
        LineSpec("21987660201", "Recepção", "Vantel Corp 50GB", Decimal("189.90"),
                 50 * GB, Decimal("0.02"), "tiny", assigned_to="Recepção"),
        # Contracted at 89.90, billed at 94.90.
        LineSpec("21987660202", "Enfermagem", "Vantel Corp 10GB", Decimal("94.90"),
                 10 * GB, Decimal("0.02"), "normal", assigned_to="Enfermagem"),
        LineSpec("21987660203", "Plantão", "Vantel Corp 10GB", Decimal("89.90"),
                 10 * GB, Decimal("0.02"), "normal", assigned_to="Plantão"),
        LineSpec("21987660204", "Administração", "Vantel Corp 5GB", Decimal("59.90"),
                 5 * GB, Decimal("0.02"), "normal", assigned_to="Administrativo"),
    ]
    contracted = [
        ContractedLine(line_id=spec.line_id, plan_name=spec.plan_name,
                       activated_on=datetime.date(2025, 6, 1))
        for spec in lines
    ]
    return AccountScenario(
        account_id="ACC-BR-2087",
        customer="Meridiano Saúde S/A",
        profile=VANTEL,
        plans=_VANTEL_CATALOG,
        lines=lines,
        contracted_lines=contracted,
        contracted_addons=[],
        periods=PERIODS_4,
        planted=[
            PlantedAnomaly(
                type=AnomalyType.PLAN_TIER_MISMATCH,
                line_id="21987660201",
                months_affected=4,
                # 50GB plan at 189.90 where the 5GB tier at 59.90 covers the usage.
                recovered_amount=money((Decimal("189.90") - Decimal("59.90")) * 4),
                rationale="Line 21987660201 consumed under 6% of its 50GB allowance for four "
                          "cycles. The contracted 5GB tier at 59.90 covers that usage.",
            ),
            PlantedAnomaly(
                type=AnomalyType.RATE_DRIFT,
                line_id="21987660202",
                months_affected=4,
                # Billed 94.90 against a contracted 89.90.
                recovered_amount=money((Decimal("94.90") - Decimal("89.90")) * 4),
                rationale="Line 21987660202 is billed 94.90/cycle for Vantel Corp 10GB, "
                          "contracted at 89.90.",
            ),
        ],
        note="Two findings with exactly computable recoveries.",
    )


def _cortez() -> AccountScenario:
    """Entirely clean. Exists so false positives have somewhere to show up."""
    lines = [
        LineSpec("31987670301", "Sócio 01", "Vantel Corp 10GB", Decimal("89.90"),
                 10 * GB, Decimal("0.02"), "normal", assigned_to="Sócio"),
        LineSpec("31987670302", "Sócio 02", "Vantel Corp 10GB", Decimal("89.90"),
                 10 * GB, Decimal("0.02"), "normal", assigned_to="Sócio",
                 discount=("Desconto fidelidade 12 meses", Decimal("9.00"))),
        LineSpec("31987670303", "Secretaria", "Vantel Corp 5GB", Decimal("59.90"),
                 5 * GB, Decimal("0.02"), "normal", assigned_to="Secretaria"),
    ]
    contracted = [
        ContractedLine(line_id=spec.line_id, plan_name=spec.plan_name,
                       activated_on=datetime.date(2025, 9, 1))
        for spec in lines
    ]
    return AccountScenario(
        account_id="ACC-BR-3312",
        customer="Cortez Advocacia",
        profile=VANTEL,
        plans=_VANTEL_CATALOG,
        lines=lines,
        contracted_lines=contracted,
        contracted_addons=[],
        periods=PERIODS_3,
        planted=[],
        note="Control account. Any finding here is a false positive. Includes a "
             "legitimate discount line, which naive rules like to misread.",
    )


def _cascadia() -> AccountScenario:
    """American carrier. One anomaly: an add-on nobody ever agreed to."""
    lines = [
        LineSpec("2065550110", "Dispatch", "Northwind Business 15GB", Decimal("65.00"),
                 15 * GB, Decimal("0.01"), "normal", assigned_to="Dispatch",
                 voice_included=Decimal(2000)),
        # Billed an international pack with no entitlement in the contract.
        LineSpec("2065550111", "Yard Ops", "Northwind Business 5GB", Decimal("45.00"),
                 5 * GB, Decimal("0.01"), "normal", assigned_to="Yard",
                 voice_included=Decimal(2000),
                 addons=[("International Roaming Pack", Decimal("19.99"))]),
        LineSpec("2065550112", "Driver Pool A", "Northwind Business 5GB", Decimal("45.00"),
                 5 * GB, Decimal("0.01"), "normal", assigned_to="Drivers",
                 voice_included=Decimal(2000)),
        LineSpec("2065550113", "Operations Lead", "Northwind Business Unlimited",
                 Decimal("85.00"), 100 * GB, Decimal("0.01"), "normal",
                 assigned_to="Operations", voice_included=Decimal(5000)),
    ]
    contracted = [
        ContractedLine(line_id=spec.line_id, plan_name=spec.plan_name,
                       activated_on=datetime.date(2025, 11, 1))
        for spec in lines
    ]
    return AccountScenario(
        account_id="ACC-US-77120",
        customer="Cascadia Freight Co.",
        profile=NORTHWIND,
        plans=_NORTHWIND_CATALOG,
        lines=lines,
        contracted_lines=contracted,
        # The contract entitles a different line entirely — so the billed pack
        # is orphaned rather than merely unlisted.
        contracted_addons=[
            ContractedAddon(addon_name="International Roaming Pack",
                            monthly_rate=Decimal("19.99"), line_ids=["2065550113"])
        ],
        periods=PERIODS_4,
        planted=[
            PlantedAnomaly(
                type=AnomalyType.ORPHAN_ADDON,
                line_id="2065550111",
                months_affected=4,
                recovered_amount=money(Decimal("19.99") * 4),
                rationale="International Roaming Pack billed on line 2065550111 for four "
                          "cycles; the contract entitles only line 2065550113.",
            )
        ],
        note="American layout and number format. Single finding.",
    )


def all_scenarios() -> list[AccountScenario]:
    return [_aurora(), _meridiano(), _cortez(), _cascadia()]
