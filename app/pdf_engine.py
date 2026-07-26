from __future__ import annotations

import html
from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models import Invoice


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _build_line_items_table(invoice: Invoice) -> Table:
    header = ["Description", "Qty", "Unit Price", "Amount"]
    data = [header]
    for item in invoice.line_items:
        data.append(
            [
                _escape(item.description),
                str(item.quantity),
                f"${_escape(item.unit_price)}",
                f"${_escape(item.amount)}",
            ]
        )
    data.append(["", "", "Subtotal:", f"${invoice.subtotal}"])
    data.append(["", "", f"Tax ({invoice.tax_rate}):", f"${invoice.tax_amount}"])
    data.append(["", "", "Total:", f"${invoice.total}"])

    col_widths = [3.5 * inch, 0.6 * inch, 1.0 * inch, 1.0 * inch]
    line_table = Table(data, colWidths=col_widths)
    line_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-2, -4), "Helvetica"),
                ("FONTNAME", (-2, -3), (-1, -3), "Helvetica"),
                ("FONTNAME", (-2, -2), (-1, -2), "Helvetica"),
                ("FONTNAME", (-2, -1), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("GRID", (0, 0), (-1, -4), 0.5, colors.black),
                ("LINEABOVE", (-2, -3), (-1, -3), 0.5, colors.black),
                ("LINEABOVE", (-2, -2), (-1, -2), 0.5, colors.black),
                ("LINEABOVE", (-2, -1), (-1, -1), 1, colors.black),
                ("LINEBELOW", (-2, -1), (-1, -1), 1, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.95, 0.95, 0.95)),
            ]
        )
    )
    return line_table


def generate_invoice_pdf(invoice: Invoice) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=50,
        rightMargin=50,
        topMargin=50,
        bottomMargin=50,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "InvoiceTitle", parent=styles["Title"], fontSize=28, spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        "InvoiceSubtitle",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.grey,
        spaceAfter=20,
    )
    section_style = ParagraphStyle(
        "Section", parent=styles["Normal"], fontSize=11, spaceAfter=4
    )
    small_style = ParagraphStyle(
        "Small", parent=styles["Normal"], fontSize=8, textColor=colors.grey
    )

    elements: list = []

    elements.append(Paragraph("InvoiceCraft AI", title_style))
    elements.append(
        Paragraph(
            f"Invoice #{_escape(invoice.invoice_number)}",
            subtitle_style,
        )
    )
    elements.append(Spacer(1, 10))

    today_str = date.today().isoformat()
    meta_data = [
        ["Invoice Number:", _escape(invoice.invoice_number)],
        ["Date:", today_str],
        ["Due Date:", invoice.due_date],
        ["Status:", invoice.status.upper()],
    ]
    meta_table = Table(meta_data, colWidths=[1.2 * inch, 2.5 * inch])
    meta_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    elements.append(meta_table)
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("Issuer", styles["Heading3"]))
    elements.append(Paragraph(f"<b>{_escape(invoice.issuer.name)}</b>", section_style))
    elements.append(Paragraph(_escape(invoice.issuer.address), section_style))
    elements.append(Paragraph(_escape(invoice.issuer.email), section_style))
    elements.append(Spacer(1, 10))

    elements.append(Paragraph("Bill To", styles["Heading3"]))
    elements.append(Paragraph(f"<b>{_escape(invoice.client.name)}</b>", section_style))
    elements.append(Paragraph(_escape(invoice.client.email), section_style))
    elements.append(Spacer(1, 20))

    elements.append(_build_line_items_table(invoice))
    elements.append(Spacer(1, 30))

    elements.append(
        Paragraph(
            f"Payment Reference: {_escape(invoice.notes)}" if invoice.notes else "",
            small_style,
        )
    )
    elements.append(
        Paragraph(
            f"Currency: {invoice.currency} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"Generated by InvoiceCraft",
            small_style,
        )
    )

    doc.build(elements)
    return buf.getvalue()
