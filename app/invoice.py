from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app import ai_parser, store
from app.models import ClientInfo, Invoice, InvoiceLineItem, IssuerInfo

_DEFAULT_PRICE = Decimal("500.00")


def _money(value) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _extract_client(text: str) -> tuple[str, str]:
    client_name = "Client"
    client_email = "client@example.com"

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
        # Don't let a bare email leak into the client name.
        if client_name == client_email:
            client_name = "Client"
    return client_name, client_email


def _extract_line_items(description: str) -> list[InvoiceLineItem]:
    """Heuristic fallback: pull priced work items out of the description.

    Recognizes "<n> hours at <rate>" and trailing "<amount>" per segment. Only
    emits multiple/priced items when the text carries an explicit price signal;
    otherwise returns a single default-priced item. The LLM parser
    (`ai_parser.parse_with_ai`) is the primary path when an API key is set.
    """
    segments = re.split(r",|\bplus\b|\band\b|;", description, flags=re.IGNORECASE)
    items: list[InvoiceLineItem] = []
    context = ""

    for raw in segments:
        seg = raw.strip()
        if not seg:
            continue

        hours = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*(?:@|at)?\s*\$?(\d+(?:\.\d+)?)",
            seg,
            re.IGNORECASE,
        )
        if hours:
            try:
                qty = Decimal(hours.group(1))
                rate = Decimal(hours.group(2))
            except InvalidOperation:
                context = seg
                continue
            label = seg[: hours.start()].strip(" -–—") or context or "Professional services"
            amount = qty * rate
            items.append(
                InvoiceLineItem(
                    description=label,
                    quantity=int(qty) if qty == qty.to_integral_value() else 1,
                    unit_price=_money(rate),
                    amount=_money(amount),
                )
            )
            context = ""
            continue

        flat = re.search(r"\$?(\d+(?:\.\d+)?)\s*$", seg)
        if flat:
            try:
                amount = Decimal(flat.group(1))
            except InvalidOperation:
                context = seg
                continue
            label = seg[: flat.start()].strip(" $-–—") or context or "Professional services"
            items.append(
                InvoiceLineItem(
                    description=label,
                    quantity=1,
                    unit_price=_money(amount),
                    amount=_money(amount),
                )
            )
            context = ""
            continue

        context = seg

    if not items:
        items = [
            InvoiceLineItem(
                description=description,
                quantity=1,
                unit_price=_money(_DEFAULT_PRICE),
                amount=_money(_DEFAULT_PRICE),
            )
        ]
    return items


def parse_description(
    description: str,
) -> tuple[str, str, list[InvoiceLineItem]]:
    ai_result = ai_parser.parse_with_ai(description)
    if ai_result:
        return (
            ai_result["client_name"],
            ai_result["client_email"],
            ai_result["line_items"],
        )
    client_name, client_email = _extract_client(description)
    line_items = _extract_line_items(description)
    return client_name, client_email, line_items


def generate_invoice_number() -> str:
    today = date.today()
    key = today.strftime("%Y%m%d")
    seq = store.next_invoice_seq(key)
    return f"INV-{key}-{seq:03d}"


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
