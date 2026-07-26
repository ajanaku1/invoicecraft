from decimal import Decimal
import re

import pytest

from app.invoice import (
    parse_description,
    generate_invoice_number,
    compute_totals,
    create_invoice,
)
from app.models import InvoiceLineItem, IssuerInfo


def test_parse_description_simple():
    client, email, items = parse_description(
        "Website development for Acme Corp, contact billing@acme.com"
    )
    assert client == "Acme Corp"
    assert email == "billing@acme.com"
    assert len(items) == 1


def test_parse_description_minimal():
    client, email, items = parse_description("Design logo")
    assert client == "Client"
    assert email == "client@example.com"
    assert len(items) == 1


def test_generate_invoice_number_format():
    inv_num = generate_invoice_number()
    pattern = r"^INV-\d{8}-\d{3}$"
    assert re.match(pattern, inv_num), f"{inv_num!r} does not match {pattern}"


def test_compute_totals_single_item():
    items = [
        InvoiceLineItem(
            description="Website", quantity=1, unit_price="500.00", amount="500.00"
        )
    ]
    subtotal, tax, total = compute_totals(items, Decimal("0.08"))
    assert subtotal == Decimal("500.00")
    assert tax == Decimal("40.00")
    assert total == Decimal("540.00")


def test_compute_totals_multiple_items():
    items = [
        InvoiceLineItem(
            description="Design", quantity=1, unit_price="300.00", amount="300.00"
        ),
        InvoiceLineItem(
            description="Development",
            quantity=1,
            unit_price="700.00",
            amount="700.00",
        ),
    ]
    subtotal, tax, total = compute_totals(items, Decimal("0.08"))
    assert subtotal == Decimal("1000.00")
    assert tax == Decimal("80.00")
    assert total == Decimal("1080.00")


def test_compute_totals_zero_tax():
    items = [
        InvoiceLineItem(
            description="Consulting",
            quantity=1,
            unit_price="1000.00",
            amount="1000.00",
        )
    ]
    subtotal, tax, total = compute_totals(items, Decimal("0"))
    assert tax == Decimal("0")
    assert total == subtotal
    assert total == Decimal("1000.00")


def test_create_invoice_full():
    issuer = IssuerInfo(
        name="Test Co", address="0xtest", email="test@co.com"
    )
    invoice = create_invoice(
        "Build a website for Acme Corp, contact billing@acme.com",
        tax_rate=Decimal("0.08"),
        issuer=issuer,
    )
    assert invoice.client.name == "Acme Corp"
    assert invoice.client.email == "billing@acme.com"
    assert invoice.status == "pending"
    assert re.match(r"^INV-\d{8}-\d{3}$", invoice.invoice_number)
    assert Decimal(invoice.total) > 0
    assert invoice.currency == "USD"


def test_create_invoice_with_tx_hash():
    issuer = IssuerInfo(
        name="Test Co", address="0xtest", email="test@co.com"
    )
    tx_hash = "0xabc123def456"
    invoice = create_invoice(
        "Design a logo",
        tax_rate=Decimal("0.08"),
        issuer=issuer,
        payment_tx_hash=tx_hash,
    )
    assert tx_hash in invoice.notes
    assert invoice.status == "paid"
