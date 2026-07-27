from __future__ import annotations

import base64
import binascii
import html
import logging
from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models import Invoice

logger = logging.getLogger(__name__)

# Refuse oversized uploads before handing bytes to the image decoder.
MAX_LOGO_BYTES = 2 * 1024 * 1024
LOGO_MAX_WIDTH = 1.6 * inch
LOGO_MAX_HEIGHT = 0.7 * inch

_CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥",
    "NGN": "₦", "INR": "₹", "CAD": "C$", "AUD": "A$", "CHF": "CHF ",
    "BRL": "R$", "ZAR": "R", "AED": "AED ", "SGD": "S$",
}


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _symbol(currency: str) -> str:
    """Currency symbol, falling back to the code itself (e.g. "SEK 120.00")."""
    code = (currency or "USD").upper()
    return _CURRENCY_SYMBOLS.get(code, f"{code} ")


def _decode_logo(logo: str) -> bytes | None:
    """Decode a data: URL or bare base64 logo, or None if it is unusable."""
    payload = logo.split(",", 1)[1] if logo.startswith("data:") else logo
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("Logo is not valid base64; skipping it")
        return None
    if not raw or len(raw) > MAX_LOGO_BYTES:
        logger.warning("Logo is empty or larger than %d bytes; skipping it", MAX_LOGO_BYTES)
        return None
    return raw


def _build_logo(logo: str) -> Image | None:
    """Scale the logo into the header box, preserving its aspect ratio."""
    raw = _decode_logo(logo)
    if raw is None:
        return None
    try:
        image = Image(BytesIO(raw))
        scale = min(LOGO_MAX_WIDTH / image.imageWidth, LOGO_MAX_HEIGHT / image.imageHeight)
        image.drawWidth = image.imageWidth * scale
        image.drawHeight = image.imageHeight * scale
        image.hAlign = "LEFT"
        return image
    except Exception:
        logger.warning("Could not render the supplied logo; skipping it", exc_info=True)
        return None


def _build_line_items_table(invoice: Invoice) -> Table:
    money = _symbol(invoice.currency)
    header = ["Description", "Qty", "Unit Price", "Amount"]
    data = [header]
    for item in invoice.line_items:
        data.append(
            [
                _escape(item.description),
                str(item.quantity),
                f"{money}{_escape(item.unit_price)}",
                f"{money}{_escape(item.amount)}",
            ]
        )
    data.append(["", "", "Subtotal:", f"{money}{invoice.subtotal}"])
    data.append(["", "", f"Tax ({invoice.tax_rate}):", f"{money}{invoice.tax_amount}"])
    data.append(["", "", "Total:", f"{money}{invoice.total}"])

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


def generate_invoice_pdf(invoice: Invoice, logo: str = "") -> bytes:
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

    # The header carries the issuer's own branding — their logo and their name.
    logo_flowable = _build_logo(logo) if logo else None
    if logo_flowable is not None:
        elements.append(logo_flowable)
        elements.append(Spacer(1, 12))

    elements.append(Paragraph(_escape(invoice.issuer.name) or "Invoice", title_style))
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

    def add_party(heading: str, name: str, lines: list[str]) -> None:
        elements.append(Paragraph(heading, styles["Heading3"]))
        elements.append(Paragraph(f"<b>{_escape(name)}</b>", section_style))
        for line in lines:
            if line:
                elements.append(Paragraph(_escape(line), section_style))

    add_party("From", invoice.issuer.name, [invoice.issuer.address, invoice.issuer.email])
    elements.append(Spacer(1, 10))
    add_party("Bill To", invoice.client.name, [invoice.client.address, invoice.client.email])
    elements.append(Spacer(1, 20))

    elements.append(_build_line_items_table(invoice))
    elements.append(Spacer(1, 30))

    if invoice.notes:
        elements.append(Paragraph(_escape(invoice.notes), small_style))
    elements.append(Paragraph(f"Currency: {_escape(invoice.currency)}", small_style))

    doc.build(elements)
    return buf.getvalue()
