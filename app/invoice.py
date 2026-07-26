from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from app.models import ClientInfo, Invoice, InvoiceLineItem, IssuerInfo

_invoice_counters: dict[str, int] = {}


def parse_description(
    description: str,
) -> tuple[str, str, list[InvoiceLineItem]]:
    client_name = "Client"
    client_email = "client@example.com"

    text = description
    for prefix in ["invoice for ", "for ", "client: ", "client "]:
        idx = text.lower().find(prefix)
        if idx == -1:
            continue
        candidate = text[idx + len(prefix):].strip()
        candidate = re.split(r"[,.;]", candidate)[0].strip()
        if candidate:
            client_name = candidate
            break

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    if email_match:
        client_email = email_match.group(0)

    default_price = Decimal("500.00")
    line_items = [
        InvoiceLineItem(
            description=description,
            quantity=1,
            unit_price=str(default_price),
            amount=str(default_price),
        )
    ]

    return client_name, client_email, line_items


def generate_invoice_number() -> str:
    today = date.today()
    key = today.strftime("%Y%m%d")
    _invoice_counters[key] = _invoice_counters.get(key, 0) + 1
    return f"INV-{key}-{_invoice_counters[key]:03d}"


def compute_totals(
    line_items: list[InvoiceLineItem], tax_rate: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    subtotal = sum(Decimal(item.amount) for item in line_items)
    subtotal = subtotal.quantize(Decimal("0.01"))
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"))
    total = (subtotal + tax).quantize(Decimal("0.01"))
    return subtotal, tax, total


def create_invoice(
    description: str,
    tax_rate: Decimal,
    issuer: IssuerInfo,
    payment_tx_hash: str = "",
) -> Invoice:
    client_name, client_email, line_items = parse_description(description)
    invoice_number = generate_invoice_number()
    subtotal, tax_amount, total = compute_totals(line_items, tax_rate)

    due_date = (date.today() + timedelta(days=30)).isoformat()
    status = "paid" if payment_tx_hash else "pending"
    notes = f"Payment: {payment_tx_hash} on X Layer" if payment_tx_hash else ""

    return Invoice(
        issuer=issuer,
        client=ClientInfo(name=client_name, email=client_email),
        line_items=line_items,
        invoice_number=invoice_number,
        subtotal=str(subtotal),
        tax_rate=str(tax_rate),
        tax_amount=str(tax_amount),
        total=str(total),
        due_date=due_date,
        status=status,
        notes=notes,
    )
