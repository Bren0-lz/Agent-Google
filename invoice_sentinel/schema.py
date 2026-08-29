"""Canonical invoice schema.

Every carrier bills differently. This module defines the one shape the rest of
the system understands, so the rule engine never learns about a specific
carrier's PDF layout. Carrier peculiarities are confined to ExtractionProfile.

Two layers, deliberately separated:

  ExtractedInvoice   what Gemini is allowed to produce — transcription only.
  CanonicalInvoice   ExtractedInvoice + provenance computed in Python.

The split exists because a content hash, a timestamp and a model ID are facts
about the run, not facts on the page. Asking a language model for them invites
it to invent them. Anything a machine can compute exactly, a machine computes.

Money is Decimal end to end and crosses the wire as a string. Floats are not
allowed near currency.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    model_validator,
)

# --- Money -------------------------------------------------------------------

_CURRENCY_NOISE = re.compile(r"[^\d,.\-+]")


def _parse_money(value: Any) -> Any:
    """Coerce a transcribed amount into Decimal.

    Handles both decimal conventions, because the same engine has to read a
    Brazilian invoice ("1.234,56") and an American one ("1,234.56"). The
    extraction prompt asks for a normalised value; this is the deterministic
    safety net for when it does not get one.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass — reject it early
        raise ValueError("boolean is not a monetary amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Never Decimal(float) — that carries the binary rounding error in.
        return Decimal(str(value))
    if not isinstance(value, str):
        raise ValueError(f"cannot read {type(value).__name__} as a monetary amount")

    raw = _CURRENCY_NOISE.sub("", value.strip())
    if not raw:
        raise ValueError("empty monetary amount")

    has_comma, has_dot = "," in raw, "." in raw
    if has_comma and has_dot:
        # Whichever separator comes last is the decimal one.
        decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        raw = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_comma:
        # "1,50" is a decimal comma; "1,500" is ambiguous but reads as thousands
        # in every carrier format we target.
        head, _, tail = raw.rpartition(",")
        raw = f"{head}.{tail}" if len(tail) == 2 else raw.replace(",", "")

    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} is not a monetary amount") from exc


#: A currency amount. Decimal internally, JSON string on the wire.
#: The plain string JSON schema matters: Gemini's structured-output subset does
#: not handle Pydantic's default anyOf/pattern schema for Decimal.
Money = Annotated[
    Decimal,
    BeforeValidator(_parse_money),
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
    WithJsonSchema(
        {"type": "string", "description": "Decimal amount, dot as decimal separator, e.g. \"1234.56\""},
        mode="serialization",
    ),
    WithJsonSchema(
        {"type": "string", "description": "Decimal amount, dot as decimal separator, e.g. \"1234.56\""},
        mode="validation",
    ),
]


#: A non-monetary decimal: units, megabytes, minutes. Same string-on-the-wire
#: treatment as Money, for the same reason — Pydantic's default JSON schema for
#: Decimal is anyOf[number, string+pattern], and Gemini's structured-output
#: subset does not digest it. These feed the rule engine's arithmetic, so float
#: is no more acceptable here than it is for currency.
Quantity = Annotated[
    Decimal,
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
    WithJsonSchema(
        {"type": "string", "description": "Decimal quantity, dot as decimal separator, e.g. \"1024\""},
        mode="serialization",
    ),
    WithJsonSchema(
        {"type": "string", "description": "Decimal quantity, dot as decimal separator, e.g. \"1024\""},
        mode="validation",
    ),
]


# --- Enums -------------------------------------------------------------------


class ChargeCategory(str, Enum):
    """What kind of money a charge line represents."""

    SUBSCRIPTION = "subscription"
    USAGE = "usage"
    ADDON = "addon"
    TAX = "tax"
    FEE = "fee"
    DISCOUNT = "discount"
    ONE_TIME = "one_time"


class UsageMetric(str, Enum):
    """Metered dimensions, in canonical units."""

    DATA_MB = "data_mb"
    VOICE_MIN = "voice_min"
    SMS = "sms"


class LineStatus(str, Enum):
    """Lifecycle state of a service line as the invoice presents it."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# --- Carrier peculiarities ---------------------------------------------------


class ExtractionProfile(BaseModel):
    """Per-carrier quirks, kept out of the engine.

    This is what lets a Brazilian and an American invoice run through identical
    downstream code. If handling a new carrier requires touching anything other
    than a profile, the abstraction has leaked.
    """

    model_config = ConfigDict(extra="forbid")

    profile_key: str = Field(description="Stable identifier, e.g. 'br-vivo-empresas'")
    carrier_name: str
    country: str = Field(description="ISO 3166-1 alpha-2, e.g. 'BR' or 'US'")
    currency: str = Field(description="ISO 4217, e.g. 'BRL' or 'USD'")

    decimal_separator: str = Field(default=".", description="'.' or ','")
    thousands_separator: str = Field(default=",", description="',' , '.' or ''")
    date_format: str = Field(default="%Y-%m-%d", description="strftime pattern used on the page")

    data_unit: str = Field(default="MB", description="Unit the carrier prints for data: MB, GB…")
    tax_labels: list[str] = Field(
        default_factory=list,
        description="Line labels this carrier uses for taxes, e.g. ['ICMS', 'FUST', 'FUNTTEL']",
    )
    fee_labels: list[str] = Field(
        default_factory=list,
        description=(
            "Account-level lines this carrier charges as fees rather than taxes, "
            "e.g. ['Regulatory Recovery Fee']. Kept apart from tax_labels because "
            "the wording does not decide it: a US universal service fund is a fee "
            "while the Brazilian FUST is a tax, and only the carrier knows which."
        ),
    )
    tax_inclusive_pricing: bool = Field(
        default=False,
        description=(
            "True when the printed prices already contain the taxes the bill "
            "itemises, so the tax lines restate part of the total instead of "
            "adding to it. Brazilian telecom bills work this way — the tax is "
            "computed 'por dentro' and broken out only because Lei 12.741/2012 "
            "requires it — while a US bill adds its taxes on top."
        ),
    )
    prompt_hints: list[str] = Field(
        default_factory=list,
        description="Layout notes handed to the extractor for this carrier",
    )


# --- Invoice parts -----------------------------------------------------------


class InvoiceHeader(BaseModel):
    """Invoice-level facts, as printed."""

    model_config = ConfigDict(extra="forbid")

    carrier: str
    account_id: str = Field(description="Customer account number on the invoice")
    billing_period_start: datetime.date
    billing_period_end: datetime.date
    issue_date: datetime.date
    due_date: datetime.date
    currency: str = Field(description="ISO 4217 code, e.g. 'BRL'")
    total_amount: Money = Field(description="Invoice total exactly as printed")

    @model_validator(mode="after")
    def _check_period(self) -> "InvoiceHeader":
        if self.billing_period_end < self.billing_period_start:
            raise ValueError(
                "billing_period_end is before billing_period_start "
                f"({self.billing_period_end} < {self.billing_period_start})"
            )
        if self.due_date < self.issue_date:
            raise ValueError(
                f"due_date is before issue_date ({self.due_date} < {self.issue_date})"
            )
        return self


class ServiceLine(BaseModel):
    """One billed service — typically a phone line or a data SIM."""

    model_config = ConfigDict(extra="forbid")

    line_id: str = Field(description="Carrier's identifier for the line, e.g. MSISDN")
    label: str = Field(description="Human label printed next to the line")
    plan_name: str
    assigned_to: str | None = Field(
        default=None, description="Employee or cost centre, when the invoice states one"
    )
    status: LineStatus = LineStatus.UNKNOWN


class ChargeItem(BaseModel):
    """One money line on the invoice."""

    model_config = ConfigDict(extra="forbid")

    line_id: str | None = Field(
        default=None,
        description="Service line this charge belongs to; null for account-level charges",
    )
    category: ChargeCategory
    description: str
    quantity: Quantity = Field(default=Decimal(1), ge=0)
    unit_amount: Money | None = Field(
        default=None, description="Per-unit price, when the invoice prints one"
    )
    amount: Money = Field(
        description="Charged amount. Negative for discounts and credits."
    )
    period: str | None = Field(
        default=None, description="Period the charge covers, e.g. '2026-07'"
    )

    @model_validator(mode="after")
    def _check_sign(self) -> "ChargeItem":
        if self.category is ChargeCategory.DISCOUNT and self.amount > 0:
            raise ValueError(
                f"discount '{self.description}' has a positive amount ({self.amount}); "
                "discounts must be negative"
            )
        return self


class UsageRecord(BaseModel):
    """Metered consumption for one line and one metric, in canonical units."""

    model_config = ConfigDict(extra="forbid")

    line_id: str
    metric: UsageMetric
    included: Quantity = Field(ge=0, description="Allowance included in the plan")
    consumed: Quantity = Field(ge=0, description="Actually consumed this period")
    overage: Quantity = Field(default=Decimal(0), ge=0, description="Billed above the allowance")


# --- What the extractor may produce ------------------------------------------


class ExtractedInvoice(BaseModel):
    """The extractor's output schema — transcription of one PDF, nothing more.

    Referential integrity is enforced here rather than downstream on purpose:
    a precise complaint ("charge 3 references line 11987654321, which is not in
    service_lines") is exactly what the repair prompt needs to send back.
    """

    model_config = ConfigDict(extra="forbid")

    header: InvoiceHeader
    service_lines: list[ServiceLine] = Field(default_factory=list)
    charges: list[ChargeItem] = Field(default_factory=list)
    usage_records: list[UsageRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "ExtractedInvoice":
        known = {line.line_id for line in self.service_lines}

        duplicates = [lid for lid in known if [s.line_id for s in self.service_lines].count(lid) > 1]
        if duplicates:
            raise ValueError(f"duplicate line_id in service_lines: {sorted(set(duplicates))}")

        for index, charge in enumerate(self.charges):
            if charge.line_id is not None and charge.line_id not in known:
                raise ValueError(
                    f"charges[{index}] references line_id {charge.line_id!r}, "
                    f"which is not present in service_lines {sorted(known)}"
                )
        for index, usage in enumerate(self.usage_records):
            if usage.line_id not in known:
                raise ValueError(
                    f"usage_records[{index}] references line_id {usage.line_id!r}, "
                    f"which is not present in service_lines {sorted(known)}"
                )
        return self

    def charge_total(self) -> Decimal:
        """Sum of every charge. Deterministic — never ask the model for this."""
        return sum((charge.amount for charge in self.charges), Decimal(0))

    def consistency_warnings(
        self,
        tolerance: Decimal = Decimal("0.05"),
        *,
        tax_inclusive: bool = False,
    ) -> list[str]:
        """Soft checks that flag a suspicious extraction without rejecting it.

        Kept out of Pydantic validation because a carrier that rounds its own
        total should not send the extractor into a repair loop — it should be
        recorded and carried forward for a human to see.

        `tax_inclusive` comes from the carrier's profile. Where prices already
        contain the tax, the itemised tax lines restate part of the total rather
        than adding to it, and summing them double-counts: a real Brazilian bill
        was read correctly, line by line, and still reported as 149.42 out of
        balance. Excluding them here keeps the warning meaning what it says — the
        parts do not add up to the whole — instead of firing on every bill from
        a country whose invoices are all built this way.
        """
        warnings: list[str] = []

        counted = sum(
            (
                charge.amount
                for charge in self.charges
                if not (tax_inclusive and charge.category is ChargeCategory.TAX)
            ),
            Decimal(0),
        )
        delta = abs(counted - self.header.total_amount)
        if delta > tolerance:
            warnings.append(
                f"charges sum to {counted} but the invoice total is "
                f"{self.header.total_amount} (off by {delta})"
            )

        if self.header.currency.upper() != self.header.currency:
            warnings.append(f"currency {self.header.currency!r} is not upper-case ISO 4217")

        billed = {c.line_id for c in self.charges if c.line_id is not None}
        for line in self.service_lines:
            if line.line_id not in billed:
                warnings.append(f"service line {line.line_id} has no charges at all")

        return warnings


# --- Provenance and the canonical record -------------------------------------


class ExtractionProvenance(BaseModel):
    """How this record came to exist. Computed in Python, never generated."""

    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(description="SHA-256 of the source PDF bytes")
    source_uri: str = Field(description="gs:// URI of the raw PDF")
    profile_key: str
    model_id: str
    extracted_at: datetime.datetime
    attempts: int = Field(default=1, ge=1, description="Model calls, including repairs")
    repair_notes: list[str] = Field(
        default_factory=list, description="Validation errors that triggered each repair"
    )
    warnings: list[str] = Field(
        default_factory=list, description="Soft consistency findings, carried forward"
    )


class CanonicalInvoice(BaseModel):
    """One audited-ready invoice. This is what Firestore stores.

    Keyed by provenance.content_hash, which makes reprocessing the same PDF a
    no-op rather than a duplicate.
    """

    model_config = ConfigDict(extra="forbid")

    provenance: ExtractionProvenance
    invoice: ExtractedInvoice

    @property
    def content_hash(self) -> str:
        return self.provenance.content_hash

    @property
    def document_id(self) -> str:
        """Firestore document ID."""
        return self.provenance.content_hash


def content_hash(pdf_bytes: bytes) -> str:
    """Stable identity for a source document.

    The whole idempotency story rests on this: same bytes, same key, one record.
    """
    return hashlib.sha256(pdf_bytes).hexdigest()
