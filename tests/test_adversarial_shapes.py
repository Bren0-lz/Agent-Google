"""Invoice shapes that are hard to read correctly, and were only tested by hand.

The dataset is built by a generator that never produces these, so every one of
them used to live in a browser tab: send the PDF, look at the answer, decide it
seemed right. That is not a test - it is a memory of a test.

Two shapes are covered here. A credit line, because a negative charge is the one
value where being off by its own size moves the total by twice that, and because
the parser was losing the sign until tests/test_money.py went looking. And a
per-line discount that does not divide evenly, because rounding is the boundary
where "the carrier billed the wrong rate" has to stop being a dispute: a finding
that claims one cent costs more credibility than it recovers.
"""

from __future__ import annotations

from decimal import Decimal

from invoice_sentinel.config import RATE_DRIFT_TOLERANCE
from invoice_sentinel.rules import AuditContext, rate_drift, run_all_rules
from invoice_sentinel.schema import ChargeCategory, ChargeItem
from tests.conftest import GB, LineInput, build_contract, build_cycle, periods

LINE = "11900000001"
PLAN = "Small 5GB"
RATE = Decimal("59.90")


def steady_line(rate: Decimal = RATE) -> LineInput:
    """A line that does nothing interesting: used, in plan, nothing to find."""
    return LineInput(
        LINE, PLAN, rate, 5 * GB, Decimal(3000),
        voice_consumed=Decimal(400),
    )


def cycles_billed_at(rate: Decimal, count: int = 4):
    return [build_cycle(end, [steady_line(rate)]) for end in periods(count)]


def with_charge(invoice, charge: ChargeItem):
    """The invoice plus one more charge, with the header total kept honest.

    Rebuilt rather than mutated: ExtractedInvoice validates on construction, so
    going through model_validate is what proves the shape is legal at all.
    """
    return type(invoice).model_validate(
        {
            **invoice.model_dump(mode="json"),
            "charges": [
                *invoice.model_dump(mode="json")["charges"],
                charge.model_dump(mode="json"),
            ],
            "header": {
                **invoice.model_dump(mode="json")["header"],
                "total_amount": str(invoice.charge_total() + charge.amount),
            },
        }
    )


# --- A credit line -----------------------------------------------------------


def credit(amount: Decimal, period: str) -> ChargeItem:
    return ChargeItem(
        line_id=LINE,
        category=ChargeCategory.DISCOUNT,
        description="Credito referente a cobranca indevida no ciclo anterior",
        unit_amount=amount,
        amount=amount,
        period=period,
    )


def test_a_credit_makes_the_invoice_balance_rather_than_break_it():
    """A negative charge has to reduce the total, in the schema's own arithmetic.

    charge_total() is what consistency_warnings reconciles against, and it is
    described as deterministic precisely so nobody is tempted to ask the model.
    If a credit were summed as positive the parts would miss the whole by twice
    the credit, and the warning would name the wrong culprit.
    """
    invoice = cycles_billed_at(RATE)[-1]
    period = f"{invoice.header.billing_period_end:%Y-%m}"
    before = invoice.charge_total()

    credited = with_charge(invoice, credit(Decimal("-12.34"), period))

    assert credited.charge_total() == before - Decimal("12.34")
    assert credited.consistency_warnings() == []


def test_a_credit_is_not_something_to_dispute(tiers):
    """Money coming back to the customer is not a finding.

    The rule engine reads DISCOUNT as part of what a line costs, so a naive
    change that treats every unexpected charge line as suspicious would flag the
    one line on the bill that is in the customer's favour.
    """
    contract = build_contract(plans=tiers, lines={LINE: PLAN})
    cycles = cycles_billed_at(RATE)
    period = f"{cycles[-1].header.billing_period_end:%Y-%m}"
    cycles[-1] = with_charge(cycles[-1], credit(Decimal("-12.34"), period))

    findings = run_all_rules(
        AuditContext(invoice=cycles[-1], contract=contract, history=cycles[:-1])
    )

    assert findings == [], [f.anomaly_type for f in findings]


# --- A per-line discount that does not divide evenly -------------------------


def test_a_cent_over_the_contracted_rate_is_rounding_and_not_drift(tiers):
    """A discount that does not divide evenly leaves a cent behind.

    59.90 less 10% is 53.91, and a carrier whose own arithmetic lands a cent
    away has not agreed a different rate with anybody. This pins the tolerance
    at its exact edge: a finding that claims one cent costs more credibility
    than it recovers, because the letter is signed by the customer and read by
    someone looking for a reason to dismiss it.
    """
    contract = build_contract(plans=tiers, lines={LINE: PLAN})
    at_tolerance = RATE + RATE_DRIFT_TOLERANCE
    cycles = cycles_billed_at(at_tolerance)

    findings = rate_drift(
        AuditContext(invoice=cycles[-1], contract=contract, history=cycles[:-1])
    )

    assert findings == []


def test_one_cent_past_the_tolerance_is_drift(tiers):
    """The other side of the same boundary, so the tolerance cannot quietly
    widen into a rule that never fires."""
    contract = build_contract(plans=tiers, lines={LINE: PLAN})
    over = RATE + RATE_DRIFT_TOLERANCE + Decimal("0.01")
    cycles = cycles_billed_at(over)

    findings = rate_drift(
        AuditContext(invoice=cycles[-1], contract=contract, history=cycles[:-1])
    )

    assert len(findings) == 1
    assert findings[0].line_id == LINE


def test_a_line_billed_below_its_contracted_rate_is_never_a_dispute(tiers):
    """A discount applied to the subscription itself reads as negative drift.

    `rate_drift` exists to catch the carrier charging more than was agreed. A
    carrier charging less is either a discount or their own mistake, and neither
    is money this customer can ask for.
    """
    contract = build_contract(plans=tiers, lines={LINE: PLAN})
    discounted = (RATE * Decimal("0.9")).quantize(Decimal("0.01"))
    cycles = cycles_billed_at(discounted)

    findings = rate_drift(
        AuditContext(invoice=cycles[-1], contract=contract, history=cycles[:-1])
    )

    assert findings == []
