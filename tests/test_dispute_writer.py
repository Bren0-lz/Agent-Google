"""The amount guard, and what happens when it fires.

Every other defence against a fabricated amount in this system is structural:
the engine computes, the tools refuse to accept a number. A dispute letter is
the one artefact that is generated prose, addressed to a third party, over the
customer's name - so it is the one place the guarantee has to be checked rather
than arranged.

Offline throughout. The point is not whether the model writes a good letter; it
is what happens to a letter with a number nobody computed in it.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import types as pytypes
from decimal import Decimal

import pytest

from invoice_sentinel import amount_guard
from invoice_sentinel.anomaly import Anomaly, AnomalyType, Evidence
from invoice_sentinel import dispute_writer as writer_module
from invoice_sentinel.dispute import Dispute, DisputeStatus
from invoice_sentinel.dispute_writer import DisputeWriterAgent, write_dispute
from invoice_sentinel.schema import CanonicalInvoice
from tests.test_extractor import FakeClient, valid_payload


# --- What counts as an amount ------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("a refund of R$ 1.234,56 please", [Decimal("1234.56")]),
        ("billed $89.90 per cycle", [Decimal("89.90")]),
        ("the contracted rate is 89.90 but 94.90 was billed", [Decimal("89.90"), Decimal("94.90")]),
        ("total 1036.10 BRL", [Decimal("1036.10")]),
    ],
)
def test_money_is_recognised(text, expected):
    assert [value for _, value in amount_guard.find_amounts(text)] == expected


@pytest.mark.parametrize(
    "text",
    [
        "ICMS (20,00%) was applied",          # a rate, quoted from the page
        "line 11987650101 on account ACC-BR-1041",  # identifiers
        "cycle 2026-07, contract from 2026-01-01",  # dates
        "the Vantel Corp 10GB plan over 4 cycles",  # plan names and counts
        "no figures here at all",
    ],
)
def test_things_that_are_not_money_are_left_alone(text):
    """A guard that flags line ids and dates is noise, and noise gets ignored."""
    assert amount_guard.find_amounts(text) == []


# --- Fixtures ----------------------------------------------------------------


def anomaly(kind=AnomalyType.RATE_DRIFT, amount="20.00", months=4) -> Anomaly:
    return Anomaly(
        type=kind,
        line_id="11987650101",
        account_id="ACC-BR-1041",
        summary="Charged 94.90 against a contracted 89.90",
        evidence=[
            Evidence(claim="contracted rate", value="89.90", source="contract"),
            Evidence(claim="charged rate", value="94.90", source="invoice 2026-07"),
        ],
        confidence=0.97,
        recovered_amount=Decimal(amount),
        months_affected=months,
    )


@pytest.fixture
def invoice() -> CanonicalInvoice:
    return CanonicalInvoice(
        provenance={
            "content_hash": "a" * 64,
            "source_uri": "file:///tmp/invoice.pdf",
            "profile_key": "br-vantel-empresas",
            "model_id": "test-model",
            "extracted_at": "2026-08-27T10:00:00Z",
        },
        invoice=valid_payload(),
    )


def documents(letter: str, summary: str = "Summary.") -> str:
    return json.dumps({"carrier_letter": letter, "executive_summary": summary})


# --- The allowed set ---------------------------------------------------------


def test_allowed_amounts_cover_the_engines_own_figures():
    allowed = amount_guard.allowed_amounts([anomaly()])

    assert Decimal("20.00") in allowed        # recovered total
    assert Decimal("5.00") in allowed         # per cycle, 20.00 over 4
    assert Decimal("89.90") in allowed        # from the evidence
    assert Decimal("94.90") in allowed        # from the evidence
    assert Decimal("4200.00") not in allowed


# --- The guard in the writer -------------------------------------------------


def test_a_letter_quoting_only_computed_figures_is_a_draft(invoice):
    client = FakeClient(
        documents("We dispute 94.90 against the contracted 89.90; credit 20.00 requested.")
    )

    dispute = write_dispute(invoice, [anomaly()], [], client=client, model_id="test-model")

    assert dispute.status is DisputeStatus.DRAFT
    assert dispute.amounts_verified is True
    assert dispute.unverified_amounts == []
    assert dispute.attempts == 1


def test_an_invented_figure_triggers_a_repair(invoice):
    """The model is told which figure, in the same shape as the extractor's
    repair loop."""
    client = FakeClient(
        documents("We request a credit of R$ 4.200,00."),
        documents("We request a credit of 20.00."),
    )

    dispute = write_dispute(invoice, [anomaly()], [], client=client, model_id="test-model")

    assert dispute.status is DisputeStatus.DRAFT
    assert dispute.amounts_verified is True
    assert dispute.attempts == 2


def test_a_letter_that_keeps_inventing_is_blocked_not_drafted(invoice):
    """The whole reason this file exists: a confident wrong number in a document
    addressed to a carrier is worse than no letter at all."""
    client = FakeClient(*[documents("Credit of R$ 4.200,00 requested.")] * 5)

    dispute = write_dispute(
        invoice, [anomaly()], [], client=client, model_id="test-model", max_repairs=2
    )

    assert dispute.status is DisputeStatus.BLOCKED
    assert dispute.amounts_verified is False
    assert "R$ 4.200,00" in dispute.unverified_amounts


def test_a_derived_total_is_caught(invoice):
    """20.00 a cycle over 4 cycles is 80.00 - arithmetic the model was told not
    to do, and a figure the engine never produced."""
    client = FakeClient(*[documents("Annualised, this is 240.00 per year.")] * 5)

    dispute = write_dispute(
        invoice, [anomaly()], [], client=client, model_id="test-model", max_repairs=1
    )

    assert dispute.amounts_verified is False
    assert dispute.status is DisputeStatus.BLOCKED


def test_the_summary_is_checked_too_not_only_the_letter(invoice):
    """Both documents leave the building, so both are guarded."""
    client = FakeClient(
        *[documents("Credit of 20.00 requested.", "You will save R$ 9.999,00 a year.")] * 5
    )

    dispute = write_dispute(
        invoice, [anomaly()], [], client=client, model_id="test-model", max_repairs=1
    )

    assert dispute.amounts_verified is False
    assert "R$ 9.999,00" in dispute.unverified_amounts


# --- Totals are computed, never read back from the prose ---------------------


def test_totals_come_from_the_engine_not_from_the_letter(invoice):
    client = FakeClient(documents("Credit of 20.00 requested."))

    dispute = write_dispute(
        invoice,
        [anomaly()],
        [anomaly(AnomalyType.ZOMBIE_LINE, "239.60")],
        client=client,
        model_id="test-model",
    )

    assert dispute.disputed_total == Decimal("20.00")
    assert dispute.optimisation_total == Decimal("239.60")
    assert dispute.disputed_finding_ids == ["rate_drift:11987650101"]
    assert dispute.optimisation_finding_ids == ["zombie_line:11987650101"]


# --- Handing the documents to the person reading the conversation ------------
#
# Firestore is where the letter and the summary live. It is not where anybody
# can open them: the Artifacts tab of the UI was empty, so the one artefact this
# whole pipeline exists to produce could not be read off the screen that ran it.


class FakeArtifactService:
    """Records what was attached, the way the ADK's service would be asked."""

    def __init__(self):
        self.saved: dict[str, bytes] = {}

    async def save_artifact(self, *, app_name, user_id, session_id, filename, artifact):
        self.saved[filename] = artifact.inline_data.data
        return len(self.saved)


def writer_ctx(dispute, artifact_service):
    """Only the attributes this stage reads.

    A real InvocationContext would test the ADK rather than the agent; the same
    reasoning as fake_ctx in test_intake.py.
    """
    findings = {"f1": anomaly().model_dump(mode="json")}
    state = {
        "canonical_invoice": dispute["invoice"].model_dump(mode="json"),
        "findings": findings,
        "decisions": {"f1": {"action": "dispute"}},
    }
    return pytypes.SimpleNamespace(
        session=pytypes.SimpleNamespace(state=state, id="s1"),
        invocation_id="i1",
        app_name="test",
        user_id="tester",
        artifact_service=artifact_service,
    )


def _drafted_dispute(*, verified: bool) -> Dispute:
    """A dispute whose guard verdict is fixed, so the agent is what is tested."""
    return Dispute(
        content_hash="abc123",
        account_id="ACC-BR-1041",
        carrier="Vantel Empresas",
        currency="BRL",
        period="2026-07",
        status=DisputeStatus.DRAFT if verified else DisputeStatus.BLOCKED,
        carrier_letter="Dear Vantel, 20.00 was overcharged.",
        executive_summary="You can recover 20.00.",
        disputed_total=Decimal("20.00"),
        optimisation_total=Decimal("0.00"),
        amounts_verified=verified,
        unverified_amounts=[] if verified else ["4000.00"],
        generated_at=datetime.datetime.now(datetime.timezone.utc),
        model_id="fake",
    )


def run_writer(invoice, monkeypatch, *, verified: bool):
    """Drive DisputeWriterAgent over a dispute whose guard verdict is fixed."""
    drafted = _drafted_dispute(verified=verified)
    monkeypatch.setattr(writer_module, "write_dispute", lambda *a, **kw: drafted)

    service = FakeArtifactService()
    agent = DisputeWriterAgent(name="dispute_writer", persist=False)
    ctx = writer_ctx({"invoice": invoice}, service)

    async def go():
        return [event async for event in agent._run_async_impl(ctx)]

    return service, asyncio.run(go())


def test_a_verified_dispute_is_attached_to_the_session(invoice, monkeypatch):
    """Both documents, openable from the conversation that produced them."""
    service, events = run_writer(invoice, monkeypatch, verified=True)

    assert sorted(service.saved) == [
        "ACC-BR-1041-2026-07-carrier-letter.md",
        "ACC-BR-1041-2026-07-customer-summary.md",
    ]
    assert b"Dear Vantel" in service.saved["ACC-BR-1041-2026-07-carrier-letter.md"]

    # The event carries the delta, which is what makes the UI list them.
    delta = {}
    for event in events:
        if event.actions and event.actions.artifact_delta:
            delta.update(event.actions.artifact_delta)
    assert set(delta) == set(service.saved)


def test_a_blocked_dispute_is_not_attached(invoice, monkeypatch):
    """The one file nobody may download.

    A blocked letter quotes a figure the rule engine never computed. Storing it
    as something a person can open is exactly the draft later mistaken for a
    reviewed one - the outcome amount_guard exists to prevent.
    """
    service, events = run_writer(invoice, monkeypatch, verified=False)

    assert service.saved == {}
    said = "\n".join(
        part.text
        for event in events
        if event.content
        for part in (event.content.parts or [])
        if part.text
    )
    assert "WITHHELD" in said
    assert "attached to this session" not in said


def test_an_artifact_service_that_fails_does_not_lose_the_audit(invoice, monkeypatch):
    """The download is a convenience; the run that produced it is not."""

    class BrokenArtifactService(FakeArtifactService):
        async def save_artifact(self, **kwargs):
            raise RuntimeError("bucket unreachable")

    drafted_service = BrokenArtifactService()
    monkeypatch.setattr(
        writer_module, "write_dispute", lambda *a, **kw: _drafted_dispute(verified=True)
    )
    agent = DisputeWriterAgent(name="dispute_writer", persist=False)
    ctx = writer_ctx({"invoice": invoice}, drafted_service)

    async def go():
        return [event async for event in agent._run_async_impl(ctx)]

    events = asyncio.run(go())

    said = "\n".join(
        part.text
        for event in events
        if event.content
        for part in (event.content.parts or [])
        if part.text
    )
    assert "Drafted a dispute for ACC-BR-1041" in said
    assert "attached to this session" not in said
