"""The contracted truth an invoice is audited against.

An invoice on its own cannot be wrong — it can only disagree with something.
This module holds that something: what the customer actually agreed to pay.
Rules like RateDrift and PlanTierMismatch are meaningless without it.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schema import Money, UsageMetric


class ContractedAllowance(BaseModel):
    """How much of a metered dimension the plan includes, and the overage rate."""

    model_config = ConfigDict(extra="forbid")

    metric: UsageMetric
    included: Decimal = Field(ge=0, description="Allowance in canonical units")
    overage_unit_rate: Money = Field(
        description="Contracted price per canonical unit above the allowance"
    )


class ContractedPlan(BaseModel):
    """A plan as sold, independent of any particular line."""

    model_config = ConfigDict(extra="forbid")

    plan_name: str
    monthly_rate: Money = Field(description="Contracted recurring price per line")
    allowances: list[ContractedAllowance] = Field(default_factory=list)

    def allowance_for(self, metric: UsageMetric | str) -> ContractedAllowance | None:
        # Coerced rather than compared by identity: callers legitimately hold a
        # plain string here, straight off a deserialised usage record.
        wanted = UsageMetric(metric)
        return next((a for a in self.allowances if a.metric is wanted), None)


class ContractedAddon(BaseModel):
    """An add-on the customer is entitled to, and on which lines."""

    model_config = ConfigDict(extra="forbid")

    addon_name: str
    monthly_rate: Money
    line_ids: list[str] = Field(
        default_factory=list,
        description="Lines entitled to this add-on. Empty means account-wide.",
    )


class ContractedLine(BaseModel):
    """Which plan a given line is contracted on, and since when."""

    model_config = ConfigDict(extra="forbid")

    line_id: str
    plan_name: str
    activated_on: datetime.date
    cancelled_on: datetime.date | None = None

    def is_active_on(self, day: datetime.date) -> bool:
        if day < self.activated_on:
            return False
        return self.cancelled_on is None or day <= self.cancelled_on


class Contract(BaseModel):
    """Everything the auditor is allowed to treat as agreed.

    Stored per account in Firestore. `get_contract` hands this to the auditor;
    the auditor never infers contractual terms from the invoice itself, because
    an invoice that overcharges will happily state the wrong rate as fact.
    """

    model_config = ConfigDict(extra="forbid")

    account_id: str
    carrier: str
    currency: str
    effective_from: datetime.date
    effective_to: datetime.date | None = None

    plans: list[ContractedPlan] = Field(default_factory=list)
    lines: list[ContractedLine] = Field(default_factory=list)
    addons: list[ContractedAddon] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_plan_references(self) -> "Contract":
        known_plans = {plan.plan_name for plan in self.plans}
        for line in self.lines:
            if line.plan_name not in known_plans:
                raise ValueError(
                    f"contracted line {line.line_id} is on plan {line.plan_name!r}, "
                    f"which is not among the contracted plans {sorted(known_plans)}"
                )
        return self

    def plan_for_line(self, line_id: str) -> ContractedPlan | None:
        contracted = next((line for line in self.lines if line.line_id == line_id), None)
        if contracted is None:
            return None
        return next((p for p in self.plans if p.plan_name == contracted.plan_name), None)

    def line(self, line_id: str) -> ContractedLine | None:
        return next((line for line in self.lines if line.line_id == line_id), None)

    def addon_for(self, addon_name: str, line_id: str | None = None) -> ContractedAddon | None:
        """The entitlement covering this add-on, or None if there is no entitlement.

        An account-wide add-on (empty line_ids) covers every line.
        """
        for addon in self.addons:
            if addon.addon_name != addon_name:
                continue
            if not addon.line_ids or line_id is None or line_id in addon.line_ids:
                return addon
        return None
