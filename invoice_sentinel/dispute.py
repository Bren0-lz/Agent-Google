"""The output of an audit: what gets sent, and to whom.

Two documents, because there are two audiences and two kinds of finding. The
carrier gets a letter about charges it had no right to make. The customer gets a
summary that also covers the money they are losing to their own plan
configuration, which the carrier has no part in and no obligation about.

Status is explicit and the transitions are one-way. `submitted` is set by the
submission step, never by the writer - so a draft nobody approved can never be
mistaken for something that was sent.
"""

from __future__ import annotations

import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .schema import Money


class DisputeStatus(str, Enum):
    """Where a dispute is in its life.

    `blocked` exists because a document that failed the amount check must not
    be quietly downgraded to a draft and picked up later as if it were fine.
    """

    #: Written, not yet reviewed.
    DRAFT = "draft"
    #: A person signed off. Eligible for submission.
    APPROVED = "approved"
    #: Sent to the carrier.
    SUBMITTED = "submitted"
    #: The amount check failed. Needs a human before anything else happens.
    BLOCKED = "blocked"


class DisputeDocuments(BaseModel):
    """What the model is asked to produce. Prose only.

    No amounts field, no totals field: the writer composes text, and every
    figure in that text is checked against what the engine computed before the
    document is allowed anywhere.
    """

    model_config = ConfigDict(extra="forbid")

    carrier_letter: str = Field(
        description="Formal dispute letter addressed to the carrier's billing department"
    )
    executive_summary: str = Field(
        description="Plain-language summary for the customer, covering disputes and plan changes"
    )


class Dispute(BaseModel):
    """One audit's output, as stored.

    Keyed by content_hash in Firestore, the same key as the invoice it came
    from: re-auditing an invoice replaces its dispute rather than accumulating
    a second one beside it.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str
    account_id: str
    carrier: str
    currency: str
    period: str = Field(description="Billing cycle the audit covered, as YYYY-MM")

    status: DisputeStatus = DisputeStatus.DRAFT
    carrier_letter: str
    executive_summary: str

    disputed_finding_ids: list[str] = Field(default_factory=list)
    optimisation_finding_ids: list[str] = Field(default_factory=list)

    #: Both computed by the rule engine. Present so a reader does not have to
    #: trust the prose, and so the dashboard never parses a letter for a number.
    disputed_total: Money
    optimisation_total: Money

    #: False when a figure in the prose matched nothing the engine produced.
    amounts_verified: bool = True
    unverified_amounts: list[str] = Field(default_factory=list)

    generated_at: datetime.datetime
    model_id: str
    attempts: int = Field(default=1, ge=1)

    @property
    def document_id(self) -> str:
        return self.content_hash
