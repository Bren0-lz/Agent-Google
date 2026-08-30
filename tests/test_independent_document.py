"""The one document nobody here designed.

Every other fixture in this repository comes out of scripts/synthetic/generate.py.
That makes the suite prove the rule engine agrees with the generator, which is a
weaker claim than it looks: a template and the code that reads it can share the
same wrong assumption and both stay green forever.

This is a Brazilian invoice and its signed contract, built independently from
public material about how carriers here actually bill - a cycle that does not
start on the first (26/07-25/08), taxes itemised because Lei 12.741/2012
requires it, identifiers in the shape a real bill prints them. Auditing it the
first time surfaced three defects at once, and all three were FALSE POSITIVES:
taxes computed "por dentro" counted as money billed twice, and a legitimate
add-on accused of being unauthorised because the invoice and the contract spell
the same product differently.

So what this file locks is the quiet direction. This invoice is correct against
its contract, and a change that starts finding money in it is finding money that
is not there - which is the failure that puts a claim the customer cannot defend
in front of their carrier. A missed finding costs a recovery; an invented one
costs the customer's credibility with their own supplier.

Offline, like the rest of the suite: it runs the rule engine over the committed
extraction, never the model. The PDFs are committed beside it so the extraction
can be regenerated and so the carrier can be read off the page for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from invoice_sentinel.contract import Contract
from invoice_sentinel.intake import PdfAttachment, carriers_printed_in
from invoice_sentinel.profiles import VANTEL, profile_for
from invoice_sentinel.rules import AuditContext, run_all_rules
from invoice_sentinel.schema import content_hash
from scripts import extraction_cache

INDEPENDENT = Path(__file__).resolve().parent.parent / "data" / "independent"
INVOICE_PDF = INDEPENDENT / "fatura.pdf"
CONTRACT_PDF = INDEPENDENT / "contrato.pdf"


@pytest.fixture(scope="module")
def canonical():
    cached = extraction_cache.load_all(INDEPENDENT / "extracted")
    assert len(cached) == 1, "expected exactly one extraction in the fixture"
    return next(iter(cached.values()))


@pytest.fixture(scope="module")
def contract() -> Contract:
    return Contract.model_validate(
        json.loads((INDEPENDENT / "contract.json").read_text(encoding="utf-8"))
    )


def test_the_committed_pdf_is_the_one_that_was_extracted(canonical):
    """Otherwise the fixture drifts from its own evidence.

    The extraction is committed rather than produced here - the suite runs with
    no credentials - so the only thing tying it to the PDF beside it is this
    hash. Without this assertion someone could replace either file and every
    other test in this module would keep passing about the wrong document.
    """
    assert content_hash(INVOICE_PDF.read_bytes()) == canonical.provenance.content_hash


def test_the_carrier_is_read_off_a_letterhead_nobody_here_typeset(canonical):
    """The bill prints VANTEL with the letters spaced out; the profile does not.

    Worth its own test because the synthetic invoices all render the carrier's
    name the one way generate.py writes it, so they cannot fail this.
    """
    printed = carriers_printed_in(PdfAttachment(data=INVOICE_PDF.read_bytes()))

    assert printed == {VANTEL.profile_key}
    assert canonical.provenance.profile_key == VANTEL.profile_key


def test_the_contract_is_three_plans_and_stays_three(contract):
    """Transcribing this contract, the model once filed six plans.

    It re-added each one under the abbreviated name the invoice prints, which
    reads like helpfulness and is not: the contract is the baseline every figure
    is computed against, so it came to contain terms nobody signed. Validation
    rejects it now, and model_validate above is where that runs.
    """
    assert len(contract.plans) == 3
    assert len({plan.plan_name for plan in contract.plans}) == 3


def test_the_printed_taxes_do_not_look_like_money_billed_twice(canonical):
    """ICMS, PIS, COFINS, FUST and FUNTTEL are computed inside the price here.

    They are itemised because the law requires it, not because they are added
    on top, and reading them as additions reported this invoice 149.42 out of
    balance while every figure on it had been transcribed correctly. The regime
    is declared per carrier, and this is the document that proves it.
    """
    profile = profile_for(canonical.provenance.profile_key)
    assert profile.tax_inclusive_pricing is True

    warnings = canonical.invoice.consistency_warnings(
        tax_inclusive=profile.tax_inclusive_pricing
    )

    assert warnings == []


def test_an_independently_built_invoice_yields_no_findings(canonical, contract):
    """The false-positive control, on a document the generator never touched.

    Both defects this fixture exists for produced findings that were not there.
    A future change that makes this list non-empty has not found money - it has
    invented a dispute, and it will do that to a real customer's bill.
    """
    findings = run_all_rules(
        AuditContext(invoice=canonical.invoice, contract=contract, history=[])
    )

    assert findings == [], [f.anomaly_type for f in findings]


def test_the_signed_contract_pdf_is_committed_too():
    """An audit nobody can trace back to its agreement is not reproducible."""
    assert CONTRACT_PDF.exists()
    assert carriers_printed_in(PdfAttachment(data=CONTRACT_PDF.read_bytes())) == {
        VANTEL.profile_key
    }
