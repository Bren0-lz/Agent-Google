"""Carrier extraction profiles, available at runtime.

Every carrier prints the same facts differently: separators, date order, which
section comes first, what the tax lines are called. ExtractionProfile is where
that variation is allowed to live, so the rule engine never learns about a
specific PDF layout.

This module ships inside the container because the extractor needs the
prompt hints in production. The synthetic dataset generator imports the same
definitions, so a profile is described exactly once: the invoice a judge sees
and the prompt the model reads are guaranteed to describe the same carrier.

Adding a carrier should mean adding an entry here and nothing else. If it ever
requires touching the extractor, the abstraction has leaked.
"""

from __future__ import annotations

from .schema import ExtractionProfile

# --- Profiles ----------------------------------------------------------------

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
    tax_labels=["State Sales Tax"],
    fee_labels=["Federal Universal Service Fund", "Regulatory Recovery Fee"],
    prompt_hints=[
        "Amounts use a dot as decimal separator and a comma for thousands.",
        "Dates are MM/DD/YYYY.",
        "The usage summary appears before the charge detail.",
        "Taxes, fees and surcharges are account-level and have no line_id.",
    ],
)


# --- Registry ----------------------------------------------------------------

PROFILES: dict[str, ExtractionProfile] = {
    profile.profile_key: profile for profile in (VANTEL, NORTHWIND)
}


def profile_for(profile_key: str) -> ExtractionProfile:
    """Look up a carrier profile, refusing to guess.

    Raises on an unknown key rather than falling back to a default, for the
    same reason run_rule_family does: silently extracting a Brazilian invoice
    with American separator hints produces plausible, wrong numbers, and
    nothing downstream would notice.
    """
    try:
        return PROFILES[profile_key]
    except KeyError:
        raise ValueError(
            f"unknown profile_key {profile_key!r}; known profiles: {sorted(PROFILES)}"
        ) from None
