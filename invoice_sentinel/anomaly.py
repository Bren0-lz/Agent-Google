"""Billing anomalies — the output of the rule engine.

Every field here is produced by deterministic Python. No monetary value in this
module has ever passed through a language model: the agent decides which lines
are worth investigating and whether the evidence supports a dispute, but the
arithmetic behind `recovered_amount` is verifiable and reproducible.

The five types are structurally universal. None of them requires knowing
Brazilian or American telecom law — local specifics (ICMS, FUST, Anatel) belong
in the extraction profile, not here.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .schema import Money


class AnomalyType(str, Enum):
    """The five families of billing error this system claims to detect."""

    #: Billed every cycle, consumption ~0 for N cycles. Nobody is using it.
    ZOMBIE_LINE = "zombie_line"

    #: Consumption far below the included allowance for N cycles. Overpaying
    #: for headroom that is never touched.
    PLAN_TIER_MISMATCH = "plan_tier_mismatch"

    #: Blows past the allowance N cycles running. The cheaper plan was the
    #: bigger one all along.
    CHRONIC_OVERAGE = "chronic_overage"

    #: An add-on billed with no active parent service, or with no entitlement
    #: in the contract at all.
    ORPHAN_ADDON = "orphan_addon"

    #: Rate charged does not match the rate contracted.
    RATE_DRIFT = "rate_drift"


class Evidence(BaseModel):
    """One verifiable fact supporting a finding.

    Structured rather than prose so the dispute letter can cite it and a human
    reviewer can check it without re-reading the invoice.
    """

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(description="What this fact establishes, in one clause")
    value: str = Field(description="The measured value, formatted for a human")
    source: str = Field(
        description="Where it came from: 'invoice 2026-07', 'contract', 'history 2026-04..06'"
    )


class Anomaly(BaseModel):
    """A billing error the engine is prepared to defend.

    `confidence` is the engine's own calibration, not a model's opinion. Below
    the escalation threshold the finding goes to a human instead of to the
    carrier — see config.ESCALATION_CONFIDENCE_THRESHOLD.
    """

    model_config = ConfigDict(extra="forbid")

    type: AnomalyType
    line_id: str | None = Field(
        default=None, description="Affected line; null for account-level findings"
    )
    account_id: str
    summary: str = Field(description="One sentence a non-technical reader understands")

    evidence: list[Evidence] = Field(
        default_factory=list, description="Facts that make the finding checkable"
    )
    confidence: float = Field(ge=0.0, le=1.0)

    recovered_amount: Money = Field(
        description="Money the customer should get back or stop paying. Computed, never generated."
    )
    months_affected: int = Field(
        ge=1, description="Billing cycles over which the error persisted"
    )

    #: Set by the auditor, from config.ESCALATION_CONFIDENCE_THRESHOLD.
    needs_human_review: bool = False

    def monthly_amount(self) -> Decimal:
        """Recurring exposure per cycle — what the dispute stops going forward."""
        return self.recovered_amount / Decimal(self.months_affected)
