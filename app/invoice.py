from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app import ai_parser, store
from app.models import ClientInfo, Invoice, InvoiceLineItem, IssuerInfo

_DEFAULT_PRICE = Decimal("500.00")


def _label(text: str) -> str:
    """Sentence-case a parsed line-item label for display on the invoice."""
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text


def _money(value) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


# Where a client name ends: punctuation, a dash, or the work itself starting.
_CLIENT_STOP = re.compile(
    r"[,.;:(\[]|\s[—–-]\s|\s(?=\d)|\s+(?:invoice|inv\.?|re:)\b",
    re.IGNORECASE,
)


def _extract_client(text: str) -> tuple[str, str]:
    client_name = "Client"
    # No placeholder email — a fabricated address on a real invoice is worse
    # than an omitted one, and the PDF simply drops the line when it's blank.
    client_email = ""

    for prefix in ["invoice for ", "for ", "client: ", "client "]:
        idx = text.lower().find(prefix)
        if idx == -1:
            continue
        candidate = text[idx + len(prefix):].strip()
        candidate = _CLIENT_STOP.split(candidate)[0].strip(" -–—")
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


# Hard item boundaries; "and" is handled separately because it joins phrases
# ("hosting and DNS setup") as often as it joins items.
_SEGMENT_SPLIT = re.compile(r",|;|\bplus\b", re.IGNORECASE)
_AND_SPLIT = re.compile(r"\band\b", re.IGNORECASE)

# A flat fee at the end of a segment: "CMS integration 1200".
_TRAILING_AMOUNT = re.compile(r"\$?(\d+(?:\.\d+)?)\s*$")

# "40 hours at 75/hr", and the same with words in between:
# "40 hours of design and front-end build at 75/hr".
_HOURS_AND_RATE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b"       # 1: quantity
    r"(?:\s+(?:of\s+)?([^,;]*?))??"              # 2: the work, when named here
    r"\s*(?:@|at)?\s*\$?(\d+(?:\.\d+)?)"         # 3: rate
    r"\s*(?:/\s*(?:hr|hour)|\s*per\s+hour)?",    # optional /hr suffix
    re.IGNORECASE,
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# A trailing "for <Client>" clause belongs in Bill To, not in the item label.
# Only a capitalised name is stripped, so "Training for staff" survives intact.
_TRAILING_CLIENT = re.compile(r"\s+for\s+[A-Z][^,;]*$")


def _has_price(text: str) -> bool:
    return bool(_HOURS_AND_RATE.search(text) or _TRAILING_AMOUNT.search(text.strip()))


def _split_segments(description: str) -> list[str]:
    """Split into candidate line items.

    "and" only separates items when both sides carry their own price; otherwise
    it is part of one phrase ("hosting and DNS setup 200").
    """
    segments = []
    for segment in _SEGMENT_SPLIT.split(description):
        parts = _AND_SPLIT.split(segment)
        if len(parts) > 1 and all(_has_price(part) for part in parts):
            segments.extend(parts)
        else:
            segments.append(segment)
    return segments


def _item_label(*candidates: str) -> str:
    """First usable label among the candidates, in order of preference.

    Skips blanks and bare contact details — an email is never a description of
    work — and drops a trailing client clause before returning.
    """
    for candidate in candidates:
        label = _TRAILING_CLIENT.sub("", (candidate or "").strip(" -–—")).strip(" -–—")
        if label and not _EMAIL.fullmatch(label):
            return label
    return "Professional services"


def _extract_line_items(description: str) -> list[InvoiceLineItem]:
    """Heuristic fallback: pull priced work items out of the description.

    Recognizes "<n> hours at <rate>" and trailing "<amount>" per segment. Only
    emits multiple/priced items when the text carries an explicit price signal;
    otherwise returns a single default-priced item. The LLM parser
    (`ai_parser.parse_with_ai`) is the primary path when an API key is set.
    """
    items: list[InvoiceLineItem] = []
    context = ""

    for raw in _split_segments(description):
        seg = raw.strip()
        if not seg:
            continue

        hours = _HOURS_AND_RATE.search(seg)
        if hours:
            try:
                qty = Decimal(hours.group(1))
                rate = Decimal(hours.group(3))
            except InvalidOperation:
                context = seg
                continue
            label = _item_label(seg[: hours.start()], hours.group(2), context)
            amount = qty * rate
            items.append(
                InvoiceLineItem(
                    description=_label(label),
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
            label = _item_label(seg[: flat.start()].strip(" $"), context)
            items.append(
                InvoiceLineItem(
                    description=_label(label),
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
    client: ClientInfo | None = None,
    currency: str = "USD",
) -> Invoice:
    """Build an invoice from a description.

    Client details supplied by the caller win over anything the parser can
    extract from the description; fields left blank fall back to the parser.
    """
    parsed_name, parsed_email, line_items = parse_description(description)
    invoice_number = generate_invoice_number()
    subtotal, tax_amount, total = compute_totals(line_items, tax_rate)

    due_date = (date.today() + timedelta(days=30)).isoformat()
    status = "paid" if payment_tx_hash else "pending"

    return Invoice(
        issuer=issuer,
        client=ClientInfo(
            name=(client.name if client and client.name else parsed_name),
            email=(client.email if client and client.email else parsed_email),
            address=(client.address if client else ""),
        ),
        line_items=line_items,
        invoice_number=invoice_number,
        subtotal=str(subtotal),
        tax_rate=str(tax_rate),
        tax_amount=str(tax_amount),
        total=str(total),
        currency=currency,
        due_date=due_date,
        status=status,
    )
