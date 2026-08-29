"""Intake tests, all offline.

The intake stage is what makes the agent usable by somebody who is not holding
a curl command, so what is tested here is the reading of a message: which
attachments count, which one is the contract, which carrier was named, and —
most of all — what happens when the answer is not obvious. A stage that guesses
between a contract and an invoice would audit a bill against another bill, so
the refusals get as much attention as the happy path.

No model is called anywhere in this file.
"""

from __future__ import annotations

import asyncio
import types as pytypes
from pathlib import Path

import pytest
from google.genai import types

from invoice_sentinel import config
from invoice_sentinel.intake import (
    IntakeAgent,
    PdfAttachment,
    help_text,
    looks_like_contract,
    message_text,
    pdf_attachments,
    profile_key_in,
    split_attachments,
)
from invoice_sentinel.profiles import NORTHWIND, VANTEL
from invoice_sentinel.schema import content_hash

DATASET = Path(__file__).resolve().parent.parent / "data" / "synthetic"
INVOICE_PDF = DATASET / "invoices" / "ACC-BR-1041-2026-07.pdf"


# --- Message builders --------------------------------------------------------


def pdf_part(data: bytes = b"%PDF-1.4 fake", name: str | None = None) -> types.Part:
    return types.Part(
        inline_data=types.Blob(
            data=data, mime_type="application/pdf", display_name=name
        )
    )


def message(*parts: types.Part) -> types.Content:
    return types.Content(role="user", parts=list(parts))


def fake_ctx(content: types.Content | None, state: dict | None = None):
    """The three attributes an intake run actually reads.

    A real InvocationContext needs a Runner, a session service and a live
    session; the agent touches `user_content`, `session.state` and
    `invocation_id` and nothing else, so standing those up would test the ADK
    rather than this stage.
    """
    return pytypes.SimpleNamespace(
        user_content=content,
        session=pytypes.SimpleNamespace(state=state if state is not None else {}),
        invocation_id="test-invocation",
    )


def run(agent: IntakeAgent, ctx) -> list:
    async def collect():
        return [event async for event in agent._run_async_impl(ctx)]

    return asyncio.run(collect())


def texts(events: list) -> str:
    return "\n".join(
        part.text for event in events for part in event.content.parts if part.text
    )


def deltas(events: list) -> dict:
    merged: dict = {}
    for event in events:
        merged.update(event.actions.state_delta or {})
    return merged


# --- Reading attachments -----------------------------------------------------


def test_pdf_attachments_ignores_text_parts():
    content = message(types.Part(text="audit this"), pdf_part(name="bill.pdf"))
    found = pdf_attachments(content)
    assert [a.name for a in found] == ["bill.pdf"]


def test_pdf_attachments_ignores_other_mime_types():
    png = types.Part(inline_data=types.Blob(data=b"\x89PNG", mime_type="image/png"))
    assert pdf_attachments(message(png)) == []


def test_pdf_attachments_skips_an_empty_upload():
    """The UI sends a part per selected file; a failed upload arrives with no bytes."""
    empty = types.Part(inline_data=types.Blob(data=b"", mime_type="application/pdf"))
    assert pdf_attachments(message(empty)) == []


def test_pdf_attachments_of_an_empty_message():
    assert pdf_attachments(None) == []
    assert pdf_attachments(types.Content(role="user", parts=[])) == []


def test_message_text_joins_and_lowercases():
    content = message(types.Part(text="Audit This"), types.Part(text="NOW"))
    assert message_text(content) == "audit this now"


# --- Choosing a carrier ------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("use br-vantel-empresas please", VANTEL.profile_key),
        ("this is a Vantel Empresas bill", VANTEL.profile_key),
        ("northwind wireless invoice", NORTHWIND.profile_key),
        ("us-northwind-wireless", NORTHWIND.profile_key),
        ("just audit it", None),
        ("", None),
    ],
)
def test_profile_key_in(text, expected):
    assert profile_key_in(text) == expected


def test_an_unknown_carrier_is_refused_rather_than_guessed():
    """The refusal belongs to profile_for; intake must surface it, not route around it."""
    agent = IntakeAgent(name="intake", persist=False)
    ctx = fake_ctx(message(pdf_part()), state={"profile_key": "br-vivo-empresas"})

    events = run(agent, ctx)

    assert "unknown profile_key" in texts(events)
    assert deltas(events) == {}


# --- Telling a contract from an invoice --------------------------------------


@pytest.mark.parametrize(
    "text, name, expected",
    [
        ("here is the contract", None, True),
        ("segue o contrato assinado", None, True),
        ("", "contrato-2025.pdf", True),
        ("", "Contract_ACC-BR-1041.pdf", True),
        ("audit this invoice", "fatura-07.pdf", False),
        ("", None, False),
    ],
)
def test_looks_like_contract(text, name, expected):
    assert looks_like_contract(text, PdfAttachment(data=b"x", name=name)) is expected


def test_split_attachments_separates_the_two():
    content = message(
        types.Part(text="the contract is attached too"),
        pdf_part(name="contrato.pdf"),
        pdf_part(name="fatura.pdf"),
    )
    contracts, invoices = split_attachments(content)
    assert [a.name for a in contracts] == ["contrato.pdf"]
    assert [a.name for a in invoices] == ["fatura.pdf"]


# --- The agent ---------------------------------------------------------------


def test_no_attachment_explains_what_to_do():
    """The old reply named a state key the person can neither see nor set."""
    agent = IntakeAgent(name="intake", persist=False)

    events = run(agent, fake_ctx(message(types.Part(text="oi"))))

    said = texts(events)
    assert "Attach a telecom invoice" in said
    assert "source_uri" not in said
    for profile in (VANTEL, NORTHWIND):
        assert profile.carrier_name in said


def test_a_session_built_over_http_is_left_alone():
    """A caller that set source_uri gets no help text: it did nothing wrong."""
    agent = IntakeAgent(name="intake", persist=False)
    ctx = fake_ctx(message(types.Part(text="audit")), state={"source_uri": "gs://b/x.pdf"})

    assert run(agent, ctx) == []


def test_a_single_pdf_is_taken_as_the_invoice():
    agent = IntakeAgent(name="intake", persist=False)

    events = run(agent, fake_ctx(message(pdf_part(name="julho.pdf"))))

    delta = deltas(events)
    assert delta["invoice_attached"] is True
    assert delta["uploaded_name"] == "julho.pdf"
    assert delta["profile_key"] == config.DEFAULT_PROFILE_KEY


def test_assuming_a_carrier_is_announced_not_hidden():
    agent = IntakeAgent(name="intake", persist=False)

    events = run(agent, fake_ctx(message(pdf_part())))

    assert "did not name a carrier" in texts(events)


def test_a_named_carrier_is_honoured_and_not_announced():
    agent = IntakeAgent(name="intake", persist=False)
    content = message(types.Part(text="northwind wireless"), pdf_part())

    events = run(agent, fake_ctx(content))

    assert deltas(events)["profile_key"] == NORTHWIND.profile_key
    assert "did not name a carrier" not in texts(events)


def test_two_unlabelled_pdfs_are_a_question_not_a_guess():
    """Auditing a bill against another bill is worse than asking."""
    agent = IntakeAgent(name="intake", persist=False)
    content = message(pdf_part(name="a.pdf"), pdf_part(name="b.pdf"))

    events = run(agent, fake_ctx(content))

    said = texts(events)
    assert "cannot tell" in said
    assert "a.pdf" in said and "b.pdf" in said
    assert deltas(events) == {}


# --- Idempotency survives the new way in -------------------------------------


def test_an_uploaded_pdf_hashes_the_same_as_the_stored_one():
    """The content hash is the Firestore key, so the route in must not change it.

    Same document, sent as an attachment rather than read from the bucket, has
    to land on the same record — otherwise uploading an invoice that was already
    audited would silently create a second one.
    """
    from invoice_sentinel.extractor import InvoiceSource

    data = INVOICE_PDF.read_bytes()
    uploaded = InvoiceSource.from_bytes(data, name="qualquer-nome.pdf")

    assert content_hash(uploaded.read()) == content_hash(INVOICE_PDF.read_bytes())
    assert content_hash(data) in uploaded.uri


def test_an_uploaded_pdf_is_inlined_not_fetched():
    """An upload:// URI has no bucket behind it; the bytes have to travel."""
    from invoice_sentinel.extractor import InvoiceSource

    source = InvoiceSource.from_bytes(b"%PDF-1.4 x", name="x.pdf")

    assert not source.is_gcs
    part = source.as_part(source.read())
    assert part.inline_data.data == b"%PDF-1.4 x"


def test_the_uri_is_derived_from_the_bytes_not_the_filename():
    from invoice_sentinel.extractor import InvoiceSource

    one = InvoiceSource.from_bytes(b"same bytes", name="fatura.pdf")
    two = InvoiceSource.from_bytes(b"same bytes", name="invoice-copy.pdf")

    assert one.uri.split("/")[2] == two.uri.split("/")[2]


def test_help_text_names_every_shipped_profile():
    said = help_text()
    assert VANTEL.profile_key in said
    assert NORTHWIND.profile_key in said


def test_the_message_does_not_label_every_attachment_at_once():
    """"Here is the contract" cannot mean both files.

    With several attachments the sentence is evidence about none of them in
    particular, so only the filenames decide. Reading it as evidence about all
    of them filed the invoice as a contract too.
    """
    content = message(
        types.Part(text="segue o contrato assinado"),
        pdf_part(name="contrato.pdf"),
        pdf_part(name="fatura-julho.pdf"),
    )

    contracts, invoices = split_attachments(content)

    assert [a.name for a in contracts] == ["contrato.pdf"]
    assert [a.name for a in invoices] == ["fatura-julho.pdf"]
