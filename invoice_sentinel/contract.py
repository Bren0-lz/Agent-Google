"""The contracted truth an invoice is audited against.

An invoice on its own cannot be wrong — it can only disagree with something.
This module holds that something: what the customer actually agreed to pay.
Rules like RateDrift and PlanTierMismatch are meaningless without it.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schema import Money, UsageMetric

#: Punctuation a bill and a contract disagree about while meaning the same
#: product: dashes of every width, brackets, dots, commas, slashes.
_PUNCTUATION = re.compile(r"[\s\-‐-―_(),./:;]+")


def normalised_name(name: str) -> str:
    """A product name reduced to what two documents have to agree on.

    A contract writes "Vantel Multi SIM (eSIM adicional na mesma linha)" and the
    bill for it prints "Vantel Multi SIM – eSIM adicional". Comparing those with
    == said the customer was billed for something they never bought, which is an
    accusation, not a finding. Case, accents and punctuation are how the same
    product is written twice; they are not how two products differ.

    Deliberately not fuzzy: this collapses formatting, it does not measure
    similarity. Two genuinely different add-ons stay different, and the caller
    still decides what to do when nothing matches.
    """
    folded = unicodedata.normalize("NFKD", name.casefold())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _PUNCTUATION.sub(" ", stripped).strip()


def names_agree(one: str, other: str) -> bool:
    """Whether two product names can be the same product spelled out differently.

    True when the shorter normalised name is contained in the longer one. The
    qualifier that goes missing sits at either end: a bill prints "Vantel Multi
    SIM – eSIM adicional" for a contract's "Vantel Multi SIM (eSIM adicional na
    mesma linha)", dropping the tail, and "Conecta Empresas 40 GB" for "Vantel
    Conecta Empresas 40 GB", dropping the carrier off the head.

    On its own this is too generous — "Roaming Pack" sits inside "Roaming Pack
    Premium", a different product at a different price. Both callers pair it
    with an exact price match, and the two together are what make it safe.
    """
    left, right = normalised_name(one), normalised_name(other)
    if not left or not right:
        return False
    shorter, longer = sorted((left, right), key=len)
    return shorter in longer


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

    @model_validator(mode="after")
    def _check_no_duplicate_plans(self) -> "Contract":
        """One product, one plan entry.

        Transcribing a contract, the model filed each plan twice: once under the
        name the agreement uses and once under the shorter name an attached
        invoice prints. It read "never invent a plan" as being about prices, and
        the duplicates as a kindness to whoever had to match the two documents.

        Nothing downstream needed the favour — plans are resolved through
        contract.lines, never by a name read off a bill — and the cost is that
        the record an audit computes money against stops being the agreement
        somebody signed. Raising here puts the repair loop on it, which is how
        every other transcription error in this file gets corrected.
        """
        for index, plan in enumerate(self.plans):
            for earlier in self.plans[:index]:
                same_name = normalised_name(earlier.plan_name) == normalised_name(
                    plan.plan_name
                )
                # An abbreviation charging a different price is a different
                # plan, and the contract is entitled to sell both.
                abbreviated = earlier.monthly_rate == plan.monthly_rate and names_agree(
                    earlier.plan_name, plan.plan_name
                )
                if same_name or abbreviated:
                    raise ValueError(
                        f"plans {earlier.plan_name!r} and {plan.plan_name!r} are the "
                        f"same plan filed twice at {plan.monthly_rate}; transcribe "
                        f"each plan once, under the name the contract itself uses"
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

        An account-wide add-on (empty line_ids) covers every line. Names are
        matched through normalised_name, because the bill and the agreement are
        two documents written by different people about the same product.
        """
        wanted = normalised_name(addon_name)
        for addon in self.addons:
            if normalised_name(addon.addon_name) != wanted:
                continue
            if not addon.line_ids or line_id is None or line_id in addon.line_ids:
                return addon
        return None

    def addon_priced_at(
        self, amount: Decimal, line_id: str | None = None
    ) -> ContractedAddon | None:
        """An entitlement this account holds at exactly this price, if any.

        Only consulted once addon_for has already failed. A charge whose wording
        matches nothing but whose amount is a price the contract sets is a
        question about how two documents word the same thing — the caller sends
        it to a person instead of disputing it. A charge that matches neither
        name nor price is the case orphan_addon exists for.
        """
        for addon in self.addons:
            if addon.monthly_rate != amount:
                continue
            if not addon.line_ids or line_id is None or line_id in addon.line_ids:
                return addon
        return None
