"""PDF rendering for synthetic invoices.

Two visually distinct templates — a Brazilian and an American carrier — because
an extractor that only ever sees one layout proves nothing. They differ in
number format, date format, section order, units and vocabulary, which is
exactly the surface ExtractionProfile is supposed to absorb.

Nothing here is imported by the agent. ReportLab stays a development
dependency; it must never end up in the Cloud Run container.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from invoice_sentinel.schema import (
    ChargeCategory,
    ExtractedInvoice,
    ExtractionProfile,
    UsageMetric,
)

# --- Locale-aware formatting -------------------------------------------------


def format_amount(value: Decimal, profile: ExtractionProfile) -> str:
    """Render an amount the way this carrier prints it, symbol included."""
    negative = value < 0
    digits = f"{abs(value):.2f}"
    integer, _, fraction = digits.partition(".")

    grouped = ""
    for index, digit in enumerate(reversed(integer)):
        if index and index % 3 == 0:
            grouped = profile.thousands_separator + grouped
        grouped = digit + grouped

    symbol = {"BRL": "R$ ", "USD": "$"}.get(profile.currency, f"{profile.currency} ")
    rendered = f"{symbol}{grouped}{profile.decimal_separator}{fraction}"
    return f"-{rendered}" if negative else rendered


def format_date(day: datetime.date, profile: ExtractionProfile) -> str:
    return day.strftime(profile.date_format)


def format_data(megabytes: Decimal, profile: ExtractionProfile) -> str:
    """Print data in the unit the carrier uses, not the canonical one."""
    if profile.data_unit.upper() == "GB":
        value = megabytes / Decimal(1024)
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return f"{text} GB"
    return f"{megabytes:.0f} MB"


# --- Shared building blocks --------------------------------------------------

_STYLES = getSampleStyleSheet()
_TITLE = ParagraphStyle("title", parent=_STYLES["Title"], fontSize=15, spaceAfter=2)
_SUB = ParagraphStyle("sub", parent=_STYLES["Normal"], fontSize=8.5, textColor=colors.HexColor("#555555"))
_H = ParagraphStyle("h", parent=_STYLES["Heading3"], fontSize=10.5, spaceBefore=10, spaceAfter=4)
_CELL = ParagraphStyle("cell", parent=_STYLES["Normal"], fontSize=8)
_TOTAL = ParagraphStyle("total", parent=_STYLES["Normal"], fontSize=13, alignment=TA_RIGHT)


def _table(rows: list[list], widths: list[float], accent: str) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(accent)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6F8")]),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#D0D5DA")),
            ]
        )
    )
    return table


def _usage_rows(invoice: ExtractedInvoice, line_id: str, profile: ExtractionProfile) -> list[list[str]]:
    rows = []
    for record in invoice.usage_records:
        if record.line_id != line_id:
            continue
        if record.metric is UsageMetric.DATA_MB:
            metric_label = "Dados" if profile.country == "BR" else "Data"
            fmt = lambda v: format_data(v, profile)  # noqa: E731
        elif record.metric is UsageMetric.VOICE_MIN:
            metric_label = "Voz" if profile.country == "BR" else "Voice"
            fmt = lambda v: f"{v:.0f} min"  # noqa: E731
        else:
            metric_label = "SMS"
            fmt = lambda v: f"{v:.0f}"  # noqa: E731
        rows.append([metric_label, fmt(record.included), fmt(record.consumed), fmt(record.overage)])
    return rows


# --- Brazilian template ------------------------------------------------------


def _render_br(invoice: ExtractedInvoice, profile: ExtractionProfile, path: Path) -> None:
    accent = "#4A148C"
    header = invoice.header
    story: list = [
        Paragraph(profile.carrier_name.upper(), _TITLE),
        Paragraph("FATURA DE SERVIÇOS DE TELECOMUNICAÇÕES", _SUB),
        Spacer(1, 6 * mm),
    ]

    story.append(
        _table(
            [
                ["Conta", "Período de referência", "Emissão", "Vencimento"],
                [
                    header.account_id,
                    f"{format_date(header.billing_period_start, profile)} a "
                    f"{format_date(header.billing_period_end, profile)}",
                    format_date(header.issue_date, profile),
                    format_date(header.due_date, profile),
                ],
            ],
            [35 * mm, 60 * mm, 35 * mm, 35 * mm],
            accent,
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(f"<b>Total a pagar: {format_amount(header.total_amount, profile)}</b>", _TOTAL))

    story.append(Paragraph("Resumo por linha", _H))
    summary = [["Linha", "Descrição", "Plano contratado", "Situação", "Subtotal"]]
    for line in invoice.service_lines:
        subtotal = sum(
            (c.amount for c in invoice.charges if c.line_id == line.line_id), Decimal(0)
        )
        status = {"active": "Ativa", "suspended": "Suspensa", "cancelled": "Cancelada"}.get(
            line.status.value, "-"
        )
        summary.append(
            [line.line_id, line.label, line.plan_name, status, format_amount(subtotal, profile)]
        )
    story.append(_table(summary, [30 * mm, 40 * mm, 45 * mm, 20 * mm, 30 * mm], accent))

    for line in invoice.service_lines:
        block: list = [Paragraph(f"Detalhamento — linha {line.line_id} ({line.label})", _H)]
        rows = [["Descrição", "Qtd.", "Valor unitário", "Valor"]]
        for charge in invoice.charges:
            if charge.line_id != line.line_id:
                continue
            rows.append(
                [
                    Paragraph(charge.description, _CELL),
                    f"{charge.quantity:g}",
                    format_amount(charge.unit_amount, profile) if charge.unit_amount is not None else "-",
                    format_amount(charge.amount, profile),
                ]
            )
        block.append(_table(rows, [80 * mm, 20 * mm, 30 * mm, 30 * mm], accent))

        usage = _usage_rows(invoice, line.line_id, profile)
        if usage:
            block.append(Spacer(1, 2 * mm))
            block.append(
                _table(
                    [["Consumo", "Franquia", "Utilizado", "Excedente"], *usage],
                    [40 * mm, 40 * mm, 40 * mm, 40 * mm],
                    "#6A1B9A",
                )
            )
        story.append(KeepTogether(block))

    account_level = [c for c in invoice.charges if c.line_id is None]
    if account_level:
        story.append(Paragraph("Tributos e encargos", _H))
        rows = [["Descrição", "Valor"]]
        for charge in account_level:
            rows.append([Paragraph(charge.description, _CELL), format_amount(charge.amount, profile)])
        story.append(_table(rows, [120 * mm, 40 * mm], accent))

    story.append(Spacer(1, 5 * mm))
    story.append(
        Paragraph(
            "Documento gerado para fins de teste. Valores em reais (BRL). "
            "Dúvidas sobre a fatura: 0800 000 0000.",
            _SUB,
        )
    )

    SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Fatura {header.account_id} {header.billing_period_end:%Y-%m}",
        author=profile.carrier_name,
        # Suppresses the embedded creation timestamp, so the same seed produces
        # byte-identical PDFs and the hashes in ground_truth.json stay valid.
        invariant=1,
    ).build(story)


# --- American template -------------------------------------------------------


def _render_us(invoice: ExtractedInvoice, profile: ExtractionProfile, path: Path) -> None:
    accent = "#0B3D91"
    header = invoice.header
    story: list = [
        Paragraph(profile.carrier_name, _TITLE),
        Paragraph("Business Wireless — Monthly Statement", _SUB),
        Spacer(1, 6 * mm),
    ]

    story.append(
        _table(
            [
                ["Account number", "Billing period", "Statement date", "Payment due"],
                [
                    header.account_id,
                    f"{format_date(header.billing_period_start, profile)} - "
                    f"{format_date(header.billing_period_end, profile)}",
                    format_date(header.issue_date, profile),
                    format_date(header.due_date, profile),
                ],
            ],
            [40 * mm, 55 * mm, 35 * mm, 35 * mm],
            accent,
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(f"<b>Total due: {format_amount(header.total_amount, profile)}</b>", _TOTAL))

    # The American layout leads with usage and folds charges underneath it —
    # the opposite order from the Brazilian one, on purpose.
    story.append(Paragraph("Usage summary", _H))
    usage_rows = [["Line", "Metric", "Included", "Used", "Over"]]
    for line in invoice.service_lines:
        for row in _usage_rows(invoice, line.line_id, profile):
            usage_rows.append([line.line_id, *row])
    if len(usage_rows) > 1:
        story.append(_table(usage_rows, [35 * mm, 30 * mm, 35 * mm, 35 * mm, 30 * mm], accent))

    story.append(Paragraph("Charges by line", _H))
    for line in invoice.service_lines:
        rows = [[f"{line.line_id} · {line.label} · {line.plan_name}", "Qty", "Rate", "Amount"]]
        for charge in invoice.charges:
            if charge.line_id != line.line_id:
                continue
            rows.append(
                [
                    Paragraph(charge.description, _CELL),
                    f"{charge.quantity:g}",
                    format_amount(charge.unit_amount, profile) if charge.unit_amount is not None else "—",
                    format_amount(charge.amount, profile),
                ]
            )
        story.append(KeepTogether([_table(rows, [85 * mm, 18 * mm, 30 * mm, 30 * mm], accent), Spacer(1, 3 * mm)]))

    account_level = [c for c in invoice.charges if c.line_id is None]
    if account_level:
        story.append(Paragraph("Taxes, fees and surcharges", _H))
        rows = [["Description", "Amount"]]
        for charge in account_level:
            rows.append([Paragraph(charge.description, _CELL), format_amount(charge.amount, profile)])
        story.append(_table(rows, [125 * mm, 38 * mm], accent))

    story.append(Spacer(1, 5 * mm))
    story.append(
        Paragraph(
            "Sample document generated for testing. Amounts in US dollars (USD). "
            "Questions about this bill: 1-800-000-0000.",
            _SUB,
        )
    )

    SimpleDocTemplate(
        str(path), pagesize=LETTER,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Statement {header.account_id} {header.billing_period_end:%Y-%m}",
        author=profile.carrier_name,
        invariant=1,
    ).build(story)


# --- Entry point -------------------------------------------------------------


def render_invoice(invoice: ExtractedInvoice, profile: ExtractionProfile, path: Path) -> bytes:
    """Write the invoice as a PDF and return the bytes that were written.

    Returning the bytes matters: their SHA-256 is the record's identity, and it
    has to be the hash of the file that actually landed on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if profile.country == "BR":
        _render_br(invoice, profile, path)
    else:
        _render_us(invoice, profile, path)
    return path.read_bytes()
