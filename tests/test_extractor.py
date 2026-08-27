"""Extractor tests, all offline.

A fake client stands in for Gemini, so these run in CI, cost nothing and are
deterministic. What is being tested is not the model - that is measured
separately by scripts/eval_extraction.py against the 15 fixtures - but the
contract around it: that invalid output is repaired rather than accepted, that
the repair budget is finite, that a soft inconsistency is recorded rather than
retried, and that provenance is computed here and never taken from the model.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal
from pathlib import Path

import pytest
from google.genai import errors

from invoice_sentinel import config
from invoice_sentinel.extractor import (
    ExtractionFailed,
    InvoiceSource,
    extract_invoice,
    repair_prompt,
)
from invoice_sentinel.profiles import PROFILES, VANTEL, profile_for
from invoice_sentinel.schema import ExtractedInvoice, content_hash

DATASET = Path(__file__).resolve().parent.parent / "data" / "synthetic"


# --- Fake Gemini -------------------------------------------------------------


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _Models:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):  # noqa: A002
        self.calls.append({"model": model, "contents": contents, "config": config})
        if not self._replies:
            raise AssertionError("the extractor asked for more replies than were scripted")
        return _Response(self._replies.pop(0))


class FakeClient:
    """Replays scripted responses and records what it was asked."""

    def __init__(self, *replies: str) -> None:
        self.models = _Models(list(replies))

    @property
    def calls(self) -> list[dict]:
        return self.models.calls

    def prompt_texts(self, call_index: int) -> list[str]:
        """Every text part in the conversation sent on a given call."""
        texts = []
        for content in self.calls[call_index]["contents"]:
            for part in content.parts:
                if getattr(part, "text", None):
                    texts.append(part.text)
        return texts


# --- Payload builders --------------------------------------------------------


def valid_payload() -> dict:
    """A minimal invoice that satisfies every hard validator."""
    return {
        "header": {
            "carrier": "Vantel Empresas",
            "account_id": "ACC-BR-1041",
            "billing_period_start": "2026-07-01",
            "billing_period_end": "2026-07-31",
            "issue_date": "2026-08-01",
            "due_date": "2026-08-10",
            "currency": "BRL",
            "total_amount": "89.90",
        },
        "service_lines": [
            {
                "line_id": "11987650101",
                "label": "Comercial",
                "plan_name": "Vantel Corp 10GB",
                "status": "active",
            }
        ],
        "charges": [
            {
                "line_id": "11987650101",
                "category": "subscription",
                "description": "Assinatura mensal",
                "quantity": "1",
                "unit_amount": "89.90",
                "amount": "89.90",
                "period": "2026-07",
            }
        ],
        "usage_records": [
            {
                "line_id": "11987650101",
                "metric": "data_mb",
                "included": "10240",
                "consumed": "7685",
                "overage": "0",
            }
        ],
    }


@pytest.fixture
def source(tmp_path: Path) -> InvoiceSource:
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4 not a real pdf, never parsed locally")
    return InvoiceSource.from_path(pdf)


# --- Happy path --------------------------------------------------------------


def test_valid_first_answer_is_accepted_unrepaired(source):
    client = FakeClient(json.dumps(valid_payload()))

    canonical = extract_invoice(source, VANTEL, client=client, model_id="test-model")

    assert canonical.provenance.attempts == 1
    assert canonical.provenance.repair_notes == []
    assert canonical.provenance.warnings == []
    assert len(client.calls) == 1
    assert canonical.invoice.header.total_amount == Decimal("89.90")


def test_provenance_describes_the_run_not_the_page(source):
    client = FakeClient(json.dumps(valid_payload()))
    before = datetime.datetime.now(datetime.timezone.utc)

    canonical = extract_invoice(source, VANTEL, client=client, model_id="test-model")

    prov = canonical.provenance
    assert prov.content_hash == content_hash(source.read())
    assert prov.source_uri == source.uri
    assert prov.profile_key == "br-vantel-empresas"
    assert prov.model_id == "test-model"
    assert prov.extracted_at >= before
    # The document id is the hash, which is what makes reprocessing idempotent.
    assert canonical.document_id == prov.content_hash


def test_model_supplied_provenance_is_refused(source):
    """extra='forbid' is the guard: the model must not be able to assert a hash."""
    payload = valid_payload()
    payload["provenance"] = {"content_hash": "deadbeef", "model_id": "whatever"}
    client = FakeClient(json.dumps(payload), json.dumps(valid_payload()))

    canonical = extract_invoice(source, VANTEL, client=client, model_id="test-model")

    assert canonical.provenance.content_hash != "deadbeef"
    assert canonical.provenance.content_hash == content_hash(source.read())


def test_the_carrier_profile_reaches_the_prompt(source):
    client = FakeClient(json.dumps(valid_payload()))

    extract_invoice(source, VANTEL, client=client, model_id="test-model")

    prompt = "\n".join(client.prompt_texts(0))
    assert "Vantel Empresas" in prompt
    for hint in VANTEL.prompt_hints:
        assert hint in prompt
    assert "ICMS" in prompt


# --- Repair ------------------------------------------------------------------


def test_orphan_line_id_is_repaired(source):
    broken = valid_payload()
    broken["charges"][0]["line_id"] = "99999"
    client = FakeClient(json.dumps(broken), json.dumps(valid_payload()))

    canonical = extract_invoice(source, VANTEL, client=client, model_id="test-model")

    assert canonical.provenance.attempts == 2
    assert len(canonical.provenance.repair_notes) == 1
    assert "99999" in canonical.provenance.repair_notes[0]
    assert canonical.invoice.charges[0].line_id == "11987650101"


def test_the_repair_prompt_names_the_offending_field(source):
    broken = valid_payload()
    broken["charges"][0]["line_id"] = "99999"
    client = FakeClient(json.dumps(broken), json.dumps(valid_payload()))

    extract_invoice(source, VANTEL, client=client, model_id="test-model")

    second_call = "\n".join(client.prompt_texts(1))
    # The rejected answer stays in the conversation, and the complaint that
    # follows it points at the exact value to fix.
    assert "99999" in second_call
    assert "did not match the schema" in second_call


def test_positive_discount_is_repaired(source):
    broken = valid_payload()
    broken["charges"][0]["category"] = "discount"
    client = FakeClient(json.dumps(broken), json.dumps(valid_payload()))

    canonical = extract_invoice(source, VANTEL, client=client, model_id="test-model")

    assert canonical.provenance.attempts == 2
    assert "discount" in canonical.provenance.repair_notes[0]


def test_repair_budget_is_finite(source):
    broken = valid_payload()
    broken["charges"][0]["line_id"] = "99999"
    # One more reply than the extractor is allowed to consume, so exceeding the
    # budget is what stops it rather than running out of scripted answers.
    client = FakeClient(*[json.dumps(broken)] * 5)

    with pytest.raises(ExtractionFailed) as caught:
        extract_invoice(source, VANTEL, client=client, model_id="test-model", max_repairs=2)

    assert caught.value.attempts == 3  # first attempt plus two repairs
    assert len(client.calls) == 3
    assert len(caught.value.repair_notes) == 3


def test_malformed_json_counts_as_a_repairable_answer(source):
    client = FakeClient("this is not json at all", json.dumps(valid_payload()))

    canonical = extract_invoice(source, VANTEL, client=client, model_id="test-model")

    assert canonical.provenance.attempts == 2


# --- Transient API failures --------------------------------------------------


class FlakyClient(FakeClient):
    """Fails the first `failures` calls the way the API does, then behaves."""

    def __init__(self, *replies: str, failures: int, error: Exception | None = None) -> None:
        super().__init__(*replies)
        inner = self.models.generate_content
        remaining = {"n": failures}
        boom = error or errors.ServerError(500, {"error": {"message": "Internal"}})

        def flaky(**kwargs):
            if remaining["n"] > 0:
                remaining["n"] -= 1
                self.models.calls.append({"failed": True, **kwargs})
                raise boom
            return inner(**kwargs)

        self.models.generate_content = flaky


def test_a_transient_server_error_is_retried(source, monkeypatch):
    monkeypatch.setattr("invoice_sentinel.extractor.time.sleep", lambda _: None)
    client = FlakyClient(json.dumps(valid_payload()), failures=2)

    canonical = extract_invoice(
        source, VANTEL, client=client, model_id="test-model", max_transient_retries=3
    )

    assert canonical.provenance.attempts == 1  # one logical attempt, three calls
    assert canonical.provenance.repair_notes == []


def test_a_transient_failure_does_not_spend_the_repair_budget(source, monkeypatch):
    """A 500 says nothing about the invoice, so it must not cost the model a
    chance at the schema."""
    monkeypatch.setattr("invoice_sentinel.extractor.time.sleep", lambda _: None)
    broken = valid_payload()
    broken["charges"][0]["line_id"] = "99999"
    client = FlakyClient(
        json.dumps(broken), json.dumps(valid_payload()), failures=1
    )

    canonical = extract_invoice(
        source,
        VANTEL,
        client=client,
        model_id="test-model",
        max_repairs=1,
        max_transient_retries=3,
    )

    assert canonical.provenance.attempts == 2
    assert len(canonical.provenance.repair_notes) == 1


def test_retries_are_bounded(source, monkeypatch):
    monkeypatch.setattr("invoice_sentinel.extractor.time.sleep", lambda _: None)
    client = FlakyClient(json.dumps(valid_payload()), failures=99)

    with pytest.raises(errors.ServerError):
        extract_invoice(
            source, VANTEL, client=client, model_id="test-model", max_transient_retries=2
        )

    assert len(client.calls) == 3  # the call plus two retries


def test_a_bad_request_is_not_retried(source, monkeypatch):
    """A 400 means the request is wrong; repeating it only burns the budget."""
    monkeypatch.setattr("invoice_sentinel.extractor.time.sleep", lambda _: None)
    bad_request = errors.ClientError(400, {"error": {"message": "Invalid argument"}})
    client = FlakyClient(json.dumps(valid_payload()), failures=99, error=bad_request)

    with pytest.raises(errors.ClientError):
        extract_invoice(
            source, VANTEL, client=client, model_id="test-model", max_transient_retries=3
        )

    assert len(client.calls) == 1


def test_rate_limiting_is_retried(source, monkeypatch):
    monkeypatch.setattr("invoice_sentinel.extractor.time.sleep", lambda _: None)
    throttled = errors.ClientError(429, {"error": {"message": "Resource exhausted"}})
    client = FlakyClient(json.dumps(valid_payload()), failures=1, error=throttled)

    canonical = extract_invoice(
        source, VANTEL, client=client, model_id="test-model", max_transient_retries=3
    )

    assert canonical.provenance.attempts == 1


# --- Soft consistency --------------------------------------------------------


def test_total_mismatch_is_recorded_not_repaired(source):
    """A carrier that rounds its own total must not be able to spin the loop."""
    payload = valid_payload()
    payload["header"]["total_amount"] = "95.00"  # charges only sum to 89.90
    client = FakeClient(json.dumps(payload))

    canonical = extract_invoice(source, VANTEL, client=client, model_id="test-model")

    assert canonical.provenance.attempts == 1
    assert canonical.provenance.repair_notes == []
    assert any("89.90" in w and "95.00" in w for w in canonical.provenance.warnings)


# --- Source ------------------------------------------------------------------


def test_local_source_inlines_bytes_and_gcs_passes_a_reference(source):
    assert source.is_gcs is False
    assert source.as_part(source.read()).inline_data is not None

    remote = InvoiceSource.resolve("gs://a-bucket/an-invoice.pdf")
    assert remote.is_gcs is True
    part = remote.as_part(b"")
    assert part.file_data.file_uri == "gs://a-bucket/an-invoice.pdf"


def test_gcs_uri_must_name_an_object():
    with pytest.raises(ValueError, match="names no object"):
        InvoiceSource.from_gcs("gs://just-a-bucket").read()


def test_fixture_hashes_match_the_published_ground_truth():
    """The hash the extractor computes is the hash the dataset promised.

    Guards the idempotency key end to end: if the PDFs or the hashing were to
    drift apart, every content_hash already published would be wrong.
    """
    truth = json.loads((DATASET / "ground_truth.json").read_text(encoding="utf-8"))
    checked = 0
    for account in truth["accounts"]:
        for entry in account["invoices"]:
            source = InvoiceSource.from_path(DATASET / entry["file"])
            assert content_hash(source.read()) == entry["content_hash"], entry["file"]
            checked += 1
    assert checked == truth["totals"]["invoices"]


# --- Profiles ----------------------------------------------------------------


def test_unknown_profile_refuses_to_guess():
    with pytest.raises(ValueError, match="unknown profile_key"):
        profile_for("br-does-not-exist")


def test_every_dataset_profile_is_available_at_runtime():
    """The container has no access to scripts/, so the profiles must live here."""
    truth = json.loads((DATASET / "ground_truth.json").read_text(encoding="utf-8"))
    for account in truth["accounts"]:
        assert account["profile_key"] in PROFILES
        assert profile_for(account["profile_key"]).currency == account["currency"]


def test_fees_are_not_described_as_taxes(source):
    """A fee and a tax are different charge categories, and only the carrier
    knows which is which: "Federal Universal Service Fund" is a US fee, while
    the Brazilian FUST is a tax despite also being a fund."""
    from invoice_sentinel.extractor import build_prompt
    from invoice_sentinel.profiles import NORTHWIND

    prompt = build_prompt(NORTHWIND)

    assert "State Sales Tax are taxes" in prompt
    assert "Regulatory Recovery Fee are fees, not taxes" in prompt
    assert "Federal Universal Service Fund" in prompt.split("are fees")[0]


def test_no_label_is_both_a_tax_and_a_fee():
    for profile in PROFILES.values():
        assert not set(profile.tax_labels) & set(profile.fee_labels), profile.profile_key


def test_default_profile_key_resolves():
    assert profile_for(config.DEFAULT_PROFILE_KEY) is not None


# --- Repair prompt formatting ------------------------------------------------


def test_repair_prompt_lists_every_complaint():
    payload = valid_payload()
    payload["charges"][0]["line_id"] = "99999"
    payload["usage_records"][0]["line_id"] = "88888"
    try:
        ExtractedInvoice.model_validate(payload)
    except Exception as error:  # noqa: BLE001 - the ValidationError is the subject
        text = repair_prompt(error)
    assert "Fix exactly these problems" in text
    assert "99999" in text
