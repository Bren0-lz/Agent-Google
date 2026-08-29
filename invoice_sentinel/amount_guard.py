"""Check that every amount in generated prose was computed, not written.

This is the last mile of the project's central rule. Everywhere else, money is
kept away from the model structurally: the rule engine computes it, the tools
refuse to accept it as an argument. But a dispute letter is prose, and prose is
generated. Nothing structural stops a model from writing "we request a refund of
R$ 4.200,00" in a document addressed to a carrier.

So the letter is checked before it goes anywhere. Every figure that reads as
money is matched against the set the engine actually produced. A figure that
matches nothing was invented, and an invented figure in a document sent to a
carrier is the worst failure this system could have - worse than missing the
error entirely, because it is the customer's credibility being spent.

Deciding what "reads as money" has to be conservative in one direction and
strict in the other. Flagging a line id or a date as an invented amount would
make the guard useless noise; letting a currency-marked figure through unchecked
would make it decorative. So: anything attached to a currency marker, plus any
bare number written to exactly two decimal places, which is how money is written
and how line ids, dates and data volumes are not.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

#: A number carrying a currency marker: "R$ 1.234,56", "$89.90", "1234.56 BRL".
_CURRENCY_MARKED = re.compile(
    r"""
    (?:
        (?:R\$|US\$|\$|BRL|USD)\s*(?P<before>[\d.,]+\d)
      | (?P<after>[\d.,]+\d)\s*(?:BRL|USD)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: A bare number written to exactly two decimals - "89.90", "1.234,56".
#: Line ids, dates, cycle counts and "10GB" never look like this.
_TWO_DECIMALS = re.compile(r"(?<![\d.,])\d{1,3}(?:[.,]\d{3})*[.,]\d{2}(?![\d.,])")

#: Percentages are rates, not amounts. "ICMS (20,00%)" is quoted from the page.
_PERCENT_SUFFIX = re.compile(r"\s*%")


def _to_decimal(raw: str) -> Decimal | None:
    """Parse a written figure, accepting either decimal convention."""
    text = raw.strip().rstrip(".,")
    if not text:
        return None

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        text = text.replace(thousands, "").replace(decimal_sep, ".")
    elif has_comma:
        head, _, tail = text.rpartition(",")
        text = f"{head}.{tail}" if len(tail) == 2 else text.replace(",", "")
    elif has_dot:
        head, _, tail = text.rpartition(".")
        if len(tail) == 3 and head:
            text = text.replace(".", "")

    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def find_amounts(text: str) -> list[tuple[str, Decimal]]:
    """Every figure in the text that reads as a monetary amount."""
    found: list[tuple[str, Decimal]] = []
    seen_spans: list[tuple[int, int]] = []

    for match in _CURRENCY_MARKED.finditer(text):
        raw = match.group("before") or match.group("after")
        if _PERCENT_SUFFIX.match(text[match.end():]):
            continue
        value = _to_decimal(raw)
        if value is not None:
            found.append((match.group(0).strip(), value))
            seen_spans.append(match.span())

    for match in _TWO_DECIMALS.finditer(text):
        if any(start <= match.start() < end for start, end in seen_spans):
            continue
        if _PERCENT_SUFFIX.match(text[match.end():]):
            continue
        value = _to_decimal(match.group(0))
        if value is not None:
            found.append((match.group(0), value))

    return found


def unverified_amounts(text: str, allowed: set[Decimal]) -> list[tuple[str, Decimal]]:
    """Figures in the text that no computed value accounts for.

    An empty list is the only acceptable result for a document that leaves the
    building.
    """
    return [(raw, value) for raw, value in find_amounts(text) if value not in allowed]


def allowed_amounts(anomalies) -> set[Decimal]:
    """Every figure a document about these findings is entitled to quote.

    Recovered totals, the per-cycle figure behind them, the sum, and whatever
    the evidence already states - a rate from the contract, a rate from the
    invoice. All of it computed or transcribed upstream; none of it authored
    by the writer.
    """
    allowed: set[Decimal] = set()
    total = Decimal(0)

    for anomaly in anomalies:
        allowed.add(anomaly.recovered_amount)
        allowed.add(anomaly.monthly_amount())
        # Rounded per-cycle figure: a letter naturally writes 59.90, not 59.9000.
        allowed.add(anomaly.monthly_amount().quantize(Decimal("0.01")))
        total += anomaly.recovered_amount
        for evidence in anomaly.evidence:
            for _, value in find_amounts(evidence.value):
                allowed.add(value)

    allowed.add(total)
    return allowed
