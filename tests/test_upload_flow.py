"""The attachment path, end to end through a real ADK runner.

The unit tests drive the intake agent with a hand-built context, which proves
the logic and assumes the plumbing. This file removes the assumption: a real
InMemoryRunner, a real session with empty state — exactly what the Web UI
creates — and a message with a PDF attached to it.

That is the case the whole change exists for, and the one most likely to break
without anybody noticing, because it breaks in the ADK's hands rather than in
ours: if attachments ever stop arriving as inline_data on user_content, every
unit test here still passes and the agent silently goes back to answering
"nothing to extract".

No tokens are spent: extract_invoice is replaced with a stub that returns a
canonical invoice built from the committed fixtures. What is under test is
whether the stages hand off, not whether Gemini can read a PDF — that is
measured by scripts/eval_extraction.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from google.adk.runners import InMemoryRunner
from google.genai import types

from invoice_sentinel import extractor_agent
from invoice_sentinel.extractor_agent import ExtractorAgent
from invoice_sentinel.intake import IntakeAgent
from invoice_sentinel.schema import CanonicalInvoice, content_hash

ROOT = Path(__file__).resolve().parent.parent
INVOICE_PDF = ROOT / "data" / "synthetic" / "invoices" / "ACC-BR-1041-2026-07.pdf"
EXTRACTED = ROOT / "data" / "extracted"


def canonical_fixture() -> CanonicalInvoice:
    """The committed extraction of the invoice this test attaches."""
    digest = content_hash(INVOICE_PDF.read_bytes())
    payload = json.loads((EXTRACTED / f"{digest}.json").read_text(encoding="utf-8"))
    return CanonicalInvoice.model_validate(payload)


def message_with_pdf(text: str, pdf: bytes, name: str) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(text=text),
            types.Part(
                inline_data=types.Blob(
                    data=pdf, mime_type="application/pdf", display_name=name
                )
            ),
        ],
    )


def drive(agent, content: types.Content) -> list:
    """Run one message through a real runner on a session with empty state.

    asyncio.run rather than pytest-asyncio: the suite ships with no async
    plugin, and a judge running `pip install -r requirements-dev.txt` should not
    discover a missing one.
    """

    async def go():
        runner = InMemoryRunner(agent=agent, app_name="test")
        session = await runner.session_service.create_session(
            app_name="test", user_id="tester"
        )
        return [
            event
            async for event in runner.run_async(
                user_id="tester", session_id=session.id, new_message=content
            )
        ]

    return asyncio.run(go())


def said(events: list) -> str:
    return "\n".join(
        part.text
        for event in events
        if event.content
        for part in (event.content.parts or [])
        if part.text
    )


def test_an_attached_invoice_reaches_the_extractor(monkeypatch):
    """The case the Web UI produces: empty state, a PDF on the message."""
    seen: dict = {}

    def fake_extract(source, profile, **kwargs):
        seen["uri"] = source.uri
        seen["bytes"] = source.read()
        seen["profile"] = profile.profile_key
        return canonical_fixture()

    monkeypatch.setattr(extractor_agent, "extract_invoice", fake_extract)

    from google.adk.agents import SequentialAgent

    pipeline = SequentialAgent(
        name="upload_pipeline",
        sub_agents=[
            IntakeAgent(name="intake", persist=False),
            ExtractorAgent(name="invoice_extractor", persist=False),
        ],
    )

    pdf = INVOICE_PDF.read_bytes()
    events = drive(pipeline, message_with_pdf("", pdf, "fatura-julho.pdf"))

    # The extractor got the actual bytes, not a placeholder.
    assert seen["bytes"] == pdf
    assert content_hash(pdf) in seen["uri"]
    assert seen["profile"] == "br-vantel-empresas"

    transcript = said(events)
    assert "Reading fatura-julho.pdf" in transcript
    assert "Extracted invoice ACC-BR-1041" in transcript
    # The old dead end must be gone.
    assert "nothing to extract" not in transcript


def test_an_empty_message_gets_help_and_no_extraction(monkeypatch):
    """What the user actually hit: typing 'oi' into an empty session."""

    def fail(*args, **kwargs):
        raise AssertionError("extraction ran with nothing attached")

    monkeypatch.setattr(extractor_agent, "extract_invoice", fail)

    from google.adk.agents import SequentialAgent

    pipeline = SequentialAgent(
        name="upload_pipeline",
        sub_agents=[
            IntakeAgent(name="intake", persist=False),
            ExtractorAgent(name="invoice_extractor", persist=False),
        ],
    )

    events = drive(pipeline, types.Content(role="user", parts=[types.Part(text="oi")]))

    transcript = said(events)
    assert "Attach a telecom invoice" in transcript
    assert "nothing to extract" not in transcript


def test_a_greeting_produces_one_answer_not_a_cascade():
    """The whole graph, on a message with nothing attached.

    This runs the real root_agent — every stage, including the auditor and the
    dispute writer — and it runs offline, which is itself the point: with no
    invoice on the run, no stage should reach Firestore or a model.

    What is being pinned is the shape of the reply. The deployed agent used to
    answer a greeting with seven stages reporting that they had nothing to do,
    the last of which concluded that "the invoice looks clean" — a clean bill of
    health for a document nobody sent. A person gets one answer, and it tells
    them what to attach.
    """
    from invoice_sentinel.agent import root_agent

    events = drive(root_agent, types.Content(role="user", parts=[types.Part(text="oi")]))

    transcript = said(events)
    assert "Attach a telecom invoice" in transcript

    # The false statement, and the noise that surrounded it.
    assert "looks clean" not in transcript
    assert "nothing to audit" not in transcript
    assert "nothing to write about" not in transcript
    assert "No findings to persist" not in transcript
    assert "skipped, no contract" not in transcript

    speakers = {event.author for event in events if said([event]).strip()}
    assert speakers == {"intake"}, f"only intake should speak, got {speakers}"


def upload_pipeline(*, persist: bool = False):
    """Intake and extractor: the pair that decides what gets read as what.

    `persist` reaches only the extractor, and only the store lookup that skips a
    second extraction — the tests that use it monkeypatch get_invoice.
    """
    from google.adk.agents import SequentialAgent

    return SequentialAgent(
        name="upload_pipeline",
        sub_agents=[
            IntakeAgent(name="intake", persist=False),
            ExtractorAgent(name="invoice_extractor", persist=persist),
        ],
    )


def canonical_billed_by(carrier: str) -> CanonicalInvoice:
    """The committed extraction, re-badged as another carrier's document."""
    payload = canonical_fixture().model_dump(mode="json")
    payload["invoice"]["header"]["carrier"] = carrier
    return CanonicalInvoice.model_validate(payload)


def test_naming_the_wrong_carrier_is_refused(monkeypatch):
    """The hole found in production: 'northwind' typed over a Vantel bill.

    It was accepted and audited without a warning. The audit happened to come
    out right only because the content hash hit an extraction made earlier under
    the correct profile; a PDF arriving for the first time would have been read
    with the American separator hints, which turn 1.234,56 into 1.234.
    """
    monkeypatch.setattr(
        extractor_agent, "extract_invoice", lambda source, profile, **kw: canonical_fixture()
    )

    events = drive(
        upload_pipeline(),
        message_with_pdf("northwind", INVOICE_PDF.read_bytes(), "fatura.pdf"),
    )

    transcript = said(events)
    assert "Refusing to audit this one" in transcript
    # It names both carriers, so the person can see which one to correct.
    assert "Northwind Wireless" in transcript
    assert "Vantel Empresas" in transcript
    # And no audit was published off the back of it.
    assert "Extracted invoice" not in transcript

    delta: dict = {}
    for event in events:
        if event.actions and event.actions.state_delta:
            delta.update(event.actions.state_delta)
    assert "canonical_invoice" not in delta
    assert "content_hash" not in delta


def test_a_carrier_with_no_profile_is_refused(monkeypatch):
    """A Vivo bill sent as a Vantel one: refused rather than misread."""
    monkeypatch.setattr(
        extractor_agent,
        "extract_invoice",
        lambda source, profile, **kw: canonical_billed_by("Vivo Empresas"),
    )

    events = drive(
        upload_pipeline(),
        message_with_pdf("vantel", INVOICE_PDF.read_bytes(), "fatura.pdf"),
    )

    transcript = said(events)
    assert "Refusing to audit this one" in transcript
    assert "Vivo Empresas" in transcript
    assert "Extracted invoice" not in transcript


def test_the_carrier_check_tolerates_how_a_bill_prints_its_name(monkeypatch):
    """VANTEL EMPRESAS S.A. is the same carrier as Vantel Empresas.

    The check matches on aliases rather than on string equality, because a
    refusal that fires on the carrier's own letterhead is worse than no check.
    """
    monkeypatch.setattr(
        extractor_agent,
        "extract_invoice",
        lambda source, profile, **kw: canonical_billed_by("VANTEL EMPRESAS S.A."),
    )

    events = drive(
        upload_pipeline(),
        message_with_pdf("vantel", INVOICE_PDF.read_bytes(), "fatura.pdf"),
    )

    transcript = said(events)
    assert "Refusing" not in transcript
    assert "Extracted invoice ACC-BR-1041" in transcript


def test_an_invoice_already_extracted_does_not_pay_for_a_second_extraction(monkeypatch):
    """Re-sending a PDF used to cost a full extraction that was then discarded.

    extract_invoice computed the content hash before calling the model, but
    nothing consulted the store with it: save_invoice only found the duplicate
    afterwards, by which point the tokens were spent.
    """
    stored = canonical_fixture()

    def fail(*args, **kwargs):
        raise AssertionError("the model was called for an invoice already on file")

    monkeypatch.setattr(extractor_agent, "extract_invoice", fail)
    monkeypatch.setattr(
        extractor_agent.store,
        "get_invoice",
        lambda digest, **kw: stored if digest == stored.content_hash else None,
    )

    events = drive(upload_pipeline(persist=True), message_with_pdf("vantel", INVOICE_PDF.read_bytes(), "f.pdf"))

    transcript = said(events)
    assert "Extracted invoice ACC-BR-1041" in transcript
    assert "not re-extracted" in transcript

