"""Contract extraction tests, all offline.

Same shape as test_extractor.py and for the same reason: the fake client makes
these deterministic and free. What is under test is the contract around the
model, not the model — that a rejected contract is repaired rather than filed,
that the repair budget is finite, and that a contract nobody could read raises
instead of quietly becoming an empty agreement.

The last one carries the most weight. `AuditContext` requires a Contract, and
the rules compare against it: a contract filed with half its plans missing does
not fail loudly, it produces an audit that finds less than it should and looks
just as confident doing it.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from invoice_sentinel import config
from invoice_sentinel.contract import Contract
from invoice_sentinel.contract_extractor import (
    ContractExtractionFailed,
    build_prompt,
    extract_contract,
)
from invoice_sentinel.extractor import InvoiceSource
from invoice_sentinel.profiles import NORTHWIND, VANTEL
from tests.test_extractor import FakeClient

PDF_BYTES = b"%PDF-1.4 pretend this is a signed contract"


@pytest.fixture
def source() -> InvoiceSource:
    return InvoiceSource.from_bytes(PDF_BYTES, name="contrato.pdf")


def valid_contract() -> dict:
    """The smallest contract that satisfies every validator."""
    return {
        "account_id": "ACC-BR-1041",
        "carrier": "Vantel Empresas",
        "currency": "BRL",
        "effective_from": "2025-01-01",
        "effective_to": None,
        "plans": [
            {
                "plan_name": "Vantel Corp 10GB",
                "monthly_rate": "89.90",
                "allowances": [
                    {
                        "metric": "data_mb",
                        "included": "10240",
                        "overage_unit_rate": "0.02",
                    }
                ],
            }
        ],
        "lines": [
            {
                "line_id": "11987650101",
                "plan_name": "Vantel Corp 10GB",
                "activated_on": "2025-03-01",
                "cancelled_on": None,
            }
        ],
        "addons": [],
    }


# --- The happy path ----------------------------------------------------------


def test_a_valid_contract_is_returned_as_a_model(source):
    client = FakeClient(json.dumps(valid_contract()))

    contract = extract_contract(source, VANTEL, client=client, model_id="test-model")

    assert isinstance(contract, Contract)
    assert contract.account_id == "ACC-BR-1041"
    # Exact, not approx: this project's whole claim is that money is never
    # approximated, and Decimal is why.
    assert contract.plan_for_line("11987650101").monthly_rate == Decimal("89.90")
    assert len(client.calls) == 1


def test_transcription_runs_at_temperature_zero(source):
    """A contract is copied, not composed — the same rule the invoice path follows."""
    client = FakeClient(json.dumps(valid_contract()))

    extract_contract(source, VANTEL, client=client, model_id="test-model")

    assert client.calls[0]["config"].temperature == config.EXTRACTION_TEMPERATURE


def test_the_pdf_is_sent_with_the_prompt(source):
    client = FakeClient(json.dumps(valid_contract()))

    extract_contract(source, VANTEL, client=client, model_id="test-model")

    parts = client.calls[0]["contents"][0].parts
    assert parts[0].inline_data.data == PDF_BYTES
    assert "Transcribe this contract." in parts[1].text


# --- Repair ------------------------------------------------------------------


def test_a_line_on_an_unpriced_plan_is_repaired_not_filed(source):
    """Contract rejects it, so the model gets told exactly what was wrong.

    This is the failure worth catching: a contract missing one plan still looks
    like a contract, and every rule that consults that plan would go quiet.
    """
    broken = valid_contract()
    broken["lines"][0]["plan_name"] = "Vantel Corp 50GB"
    client = FakeClient(json.dumps(broken), json.dumps(valid_contract()))

    contract = extract_contract(source, VANTEL, client=client, model_id="test-model")

    assert contract.lines[0].plan_name == "Vantel Corp 10GB"
    assert len(client.calls) == 2
    repair = client.prompt_texts(1)[-1]
    assert "Fix exactly these problems" in repair
    assert "Vantel Corp 50GB" in repair


def test_the_repair_budget_is_finite(source):
    """Two repairs is the budget; the third rejection has to raise."""
    broken = json.dumps({"account_id": "ACC-BR-1041"})
    client = FakeClient(broken, broken, broken)

    with pytest.raises(ContractExtractionFailed) as failure:
        extract_contract(
            source, VANTEL, client=client, model_id="test-model", max_repairs=2
        )

    assert failure.value.attempts == 3
    assert len(failure.value.repair_notes) == 3
    assert "contrato.pdf" in failure.value.source_uri


def test_the_failure_names_the_document_it_could_not_read(source):
    client = FakeClient("{}", "{}")

    with pytest.raises(ContractExtractionFailed) as failure:
        extract_contract(
            source, VANTEL, client=client, model_id="test-model", max_repairs=1
        )

    assert "could not extract a contract" in str(failure.value)


# --- The prompt --------------------------------------------------------------


def test_the_prompt_carries_the_carrier_layout_hints():
    text = build_prompt(NORTHWIND)

    assert NORTHWIND.carrier_name in text
    assert "Dates are MM/DD/YYYY." in text


def test_a_known_account_id_is_pinned_in_the_prompt():
    """The contract is filed under this id; a document spelling it differently
    would file it where get_contract never looks."""
    text = build_prompt(VANTEL, account_id="ACC-BR-2087")

    assert "ACC-BR-2087" in text


def test_the_same_plan_filed_twice_is_repaired_not_filed(source):
    """What the model actually did with a real contract and a real bill.

    Asked to transcribe an agreement listing three plans, it filed six: each one
    again under the shorter name the invoice prints, so that whoever had to
    match the two documents would find either spelling. It read "never invent a
    plan" as being about prices.

    Nothing downstream wanted the help — a line resolves its plan through
    contract.lines, never through a name read off a bill — and the price is that
    the record every amount is computed against stops being the signed
    agreement. The repair loop is how the other transcription errors here get
    fixed, and this one belongs with them.
    """
    duplicated = valid_contract()
    duplicated["plans"].append(
        {
            "plan_name": "Corp 10GB",
            "monthly_rate": "89.90",
            "allowances": [
                {"metric": "data_mb", "included": "10240", "overage_unit_rate": "0.02"}
            ],
        }
    )
    client = FakeClient(json.dumps(duplicated), json.dumps(valid_contract()))

    contract = extract_contract(source, VANTEL, client=client, model_id="test-model")

    assert len(contract.plans) == 1
    assert len(client.calls) == 2
    repair = client.prompt_texts(1)[-1]
    assert "Fix exactly these problems" in repair
    # The complaint names both spellings, so the model can see what it did.
    assert "Corp 10GB" in repair
