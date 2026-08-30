"""The parser the whole money claim rests on, tested against ugly input.

`_parse_money` is described in its own docstring as the deterministic safety net
for when the model does not return a normalised amount - which means its job is
precisely the input nobody designed for. It had no tests at all, and it turned
out to be losing the minus sign four different ways.

Every case here is something a PDF actually prints. A credit read as a charge is
not a near miss: it inflates the invoice total by twice its own value, and the
sign is gone before the value is ever a Decimal, so no rule, no guard and no
reconciliation downstream can notice. `amount_guard` compares the letter against
what the engine computed; it cannot compare the engine against the page.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from invoice_sentinel.schema import _parse_money


@pytest.mark.parametrize(
    "printed",
    [
        "−50,00",  # U+2212, what a properly typeset PDF prints
        "–50,00",  # en dash, from a layout pasted out of a word processor
        "—50,00",  # em dash
        "‐50,00",  # hyphen
        "-50,00",
        "R$ -50,00",
        "(50,00)",  # accounting notation for a credit
        "R$ (50,00)",
    ],
)
def test_a_credit_stays_a_credit(printed: str):
    """Four of these eight used to come back as +50.00.

    The regex that strips currency noise keeps only ASCII "-", so every
    typographic minus was silently deleted along with the R$ - and parentheses
    went the same way. A fifty-real credit became a fifty-real charge, and the
    invoice total it was reconciled against moved by a hundred.
    """
    assert _parse_money(printed) == Decimal("-50.00")


def test_parentheses_survive_thousands_separators():
    assert _parse_money("(1.234,56)") == Decimal("-1234.56")
    assert _parse_money("(1,234.56)") == Decimal("-1234.56")


@pytest.mark.parametrize("printed", ["(-50,00)", "(+50,00)", "(−50,00)"])
def test_a_value_that_states_its_sign_twice_is_refused(printed: str):
    """Parentheses and a minus say two different things about the same number.

    Reading it either way is a guess, and this is money. Refusing is the same
    call profile_for() makes on an unknown carrier: the wrong answer here is
    plausible and unnoticeable, which is exactly when guessing is worst.
    """
    with pytest.raises(ValueError, match="both parentheses and a sign"):
        _parse_money(printed)


@pytest.mark.parametrize("printed", ["", "   ", "()", "( )", "−", "-", "R$"])
def test_nothing_that_is_not_a_number_becomes_zero(printed: str):
    """The dangerous failure would be quiet: a missing amount read as 0.00
    balances nothing and disputes nothing, and looks like a clean line."""
    with pytest.raises(ValueError):
        _parse_money(printed)


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("1.234,56", "1234.56"),  # Brazilian
        ("1,234.56", "1234.56"),  # American
        ("1.234.567,89", "1234567.89"),
        ("0,00", "0.00"),
        ("-0,01", "-0.01"),
        ("50", "50"),
    ],
)
def test_both_decimal_conventions_still_read_the_same_way(printed: str, expected: str):
    """The regression guard on the fix above: this is the behaviour that was
    already right, and a sign fix must not move it."""
    assert _parse_money(printed) == Decimal(expected)


def test_a_float_never_carries_its_binary_error_in():
    """Decimal(0.1) is 0.1000000000000000055511151231257827. Money is exact or
    it is not money."""
    assert _parse_money(0.1) == Decimal("0.1")


@pytest.mark.parametrize("printed", [True, False])
def test_a_boolean_is_not_an_amount(printed):
    """bool is an int subclass, so an unguarded parser reads True as 1.00."""
    with pytest.raises(ValueError):
        _parse_money(printed)
