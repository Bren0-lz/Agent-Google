"""Shared machinery for the rule engine.

Every rule has the same shape: `(AuditContext) -> list[Anomaly]`, where the
context carries the three things an audit needs — the invoice under review, the
cycles before it, and the contract.

Deliberately no pandas here, despite the original plan. Pandas aggregates money
through float64, and the one rule this project will not bend is that no
monetary value is ever approximated. Over a handful of cycles per account there
is nothing to vectorise anyway; Decimal arithmetic in plain Python is both
exact and faster at this size.

Nothing in this package imports an LLM client. That is the point: the agent
decides which lines are worth investigating and whether the evidence supports a
dispute, but every number it cites was computed here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..anomaly import Anomaly
from ..contract import Contract, ContractedPlan
from ..schema import ChargeCategory, ChargeItem, ExtractedInvoice, UsageMetric, UsageRecord


def money(value: Decimal) -> Decimal:
    """Round to cents, half up — the way a billing system does it."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class AuditContext:
    """Everything a rule is allowed to look at.

    `history` holds earlier cycles oldest-first and excludes the invoice under
    audit. Rules that claim a pattern read `cycles`, which puts the audited
    invoice last, so "the trailing N cycles" always ends at the invoice being
    disputed.
    """

    invoice: ExtractedInvoice
    contract: Contract
    history: Sequence[ExtractedInvoice] = ()

    @property
    def account_id(self) -> str:
        return self.invoice.header.account_id

    @property
    def cycles(self) -> list[ExtractedInvoice]:
        """History followed by the audited invoice, oldest first."""
        return [*self.history, self.invoice]

    @staticmethod
    def period(invoice: ExtractedInvoice) -> str:
        return f"{invoice.header.billing_period_end:%Y-%m}"

    @property
    def period_range(self) -> str:
        """Human label for the whole window, for use in evidence sources."""
        cycles = self.cycles
        if len(cycles) == 1:
            return self.period(cycles[0])
        return f"{self.period(cycles[0])}..{self.period(cycles[-1])}"

    # --- lookups -------------------------------------------------------------

    def line_ids(self) -> list[str]:
        """Lines billed on the audited invoice, in the order they appear."""
        return [line.line_id for line in self.invoice.service_lines]

    @staticmethod
    def charges_for(
        invoice: ExtractedInvoice,
        line_id: str,
        categories: Iterable[ChargeCategory] | None = None,
    ) -> list[ChargeItem]:
        wanted = set(categories) if categories is not None else None
        return [
            charge
            for charge in invoice.charges
            if charge.line_id == line_id and (wanted is None or charge.category in wanted)
        ]

    @staticmethod
    def usage_for(
        invoice: ExtractedInvoice, line_id: str, metric: UsageMetric
    ) -> UsageRecord | None:
        return next(
            (
                record
                for record in invoice.usage_records
                if record.line_id == line_id and record.metric is metric
            ),
            None,
        )

    def billed_amount(
        self,
        invoice: ExtractedInvoice,
        line_id: str,
        categories: Iterable[ChargeCategory] | None = None,
    ) -> Decimal:
        """What this line cost in this cycle, for the given charge categories."""
        return sum(
            (charge.amount for charge in self.charges_for(invoice, line_id, categories)),
            Decimal(0),
        )

    # --- contract helpers ----------------------------------------------------

    def cheapest_plan_covering(self, data_mb: Decimal) -> ContractedPlan | None:
        """Cheapest contracted plan whose data allowance covers this much usage.

        The counterfactual behind every plan-sizing finding: not "a smaller plan
        exists" but "a plan the customer already has access to would have cost
        less and still fit".
        """
        candidates = [
            plan
            for plan in self.contract.plans
            if (allowance := plan.allowance_for(UsageMetric.DATA_MB)) is not None
            and allowance.included >= data_mb
        ]
        return min(candidates, key=lambda plan: plan.monthly_rate) if candidates else None


def trailing_streak(
    cycles: Sequence[ExtractedInvoice], holds: Callable[[ExtractedInvoice], bool]
) -> int:
    """How many cycles in a row, counting back from the audited one, satisfy `holds`.

    Counting backwards matters. A line that was dormant last year and is busy
    now is not a zombie, and a rule that counted matching cycles anywhere in the
    window would call it one.
    """
    streak = 0
    for invoice in reversed(cycles):
        if not holds(invoice):
            break
        streak += 1
    return streak


#: What every rule looks like.
Rule = Callable[[AuditContext], list[Anomaly]]
