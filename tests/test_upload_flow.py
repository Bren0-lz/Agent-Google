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
