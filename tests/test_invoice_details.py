"""Caller-supplied invoice details: parties, currency, tax rate, and logo."""

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ClientInfo, IssuerInfo
from app.pdf_engine import MAX_LOGO_BYTES, generate_invoice_pdf

client = TestClient(app)

PAID_TX = "0x" + "01" * 32

# Smallest valid PNG: a single transparent pixel.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def wallet(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    monkeypatch.delenv("INVOICE_ISSUER_NAME", raising=False)
    monkeypatch.delenv("INVOICE_ISSUER_EMAIL", raising=False)
    monkeypatch.delenv("INVOICE_CURRENCY", raising=False)


def post_invoice(**fields):
    body = {"description": "Website redesign, 10 hours at 50/hr", "payment_tx_hash": PAID_TX}
    body.update(fields)
    return client.post("/api/v1/invoice", json=body)


def test_supplied_parties_win_over_the_parser():
    response = post_invoice(
        issuer={"name": "Bambam Studio", "email": "hi@bambam.dev", "address": "12 Marina Rd"},
        client={"name": "FinFlow Ltd", "email": "ap@finflow.com", "address": "9 King St"},
    )

    assert response.status_code == 200
    invoice = response.json()["invoice"]
    assert invoice["issuer"] == {
        "name": "Bambam Studio", "email": "hi@bambam.dev", "address": "12 Marina Rd"
    }
    assert invoice["client"] == {
        "name": "FinFlow Ltd", "email": "ap@finflow.com", "address": "9 King St"
    }


def test_issuer_falls_back_without_naming_the_product():
    invoice = post_invoice().json()["invoice"]
    assert invoice["issuer"]["name"] == "Your Business"
    assert "InvoiceCraft" not in invoice["issuer"]["name"]


def test_currency_and_tax_rate_are_honoured():
    invoice = post_invoice(currency="eur", tax_rate="0.20").json()["invoice"]
    assert invoice["currency"] == "EUR"
    assert invoice["tax_rate"] == "0.20"
    assert invoice["subtotal"] == "500.00"
    assert invoice["tax_amount"] == "100.00"
    assert invoice["total"] == "600.00"


@pytest.mark.parametrize("bad", ["abc", "1.5", "-0.1"])
def test_malformed_tax_rate_is_rejected(bad):
    response = post_invoice(tax_rate=bad)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_tax_rate"


def test_no_payment_reference_on_the_invoice():
    invoice = post_invoice().json()["invoice"]
    assert invoice["notes"] == ""
    assert PAID_TX not in str(invoice)


@pytest.fixture
def readable_pdf():
    """Turn off stream compression so page text can be asserted on directly."""
    from reportlab import rl_config

    previous = rl_config.pageCompression
    rl_config.pageCompression = 0
    yield _pdf_text
    rl_config.pageCompression = previous


def _pdf_text(pdf: bytes) -> str:
    # Non-ASCII glyphs are written as WinAnsi octal escapes (€ -> \200).
    text = pdf.decode("latin-1")
    for octal, char in (("\\200", "€"), ("\\243", "£"), ("\\245", "¥")):
        text = text.replace(octal, char)
    return text


def _invoice(**overrides):
    from decimal import Decimal

    from app.invoice import create_invoice

    params = {
        "description": "Website redesign, 10 hours at 50/hr",
        "tax_rate": Decimal("0.08"),
        "issuer": IssuerInfo(name="Bambam Studio", address="12 Marina Rd", email="hi@bambam.dev"),
        "client": ClientInfo(name="FinFlow Ltd", email="ap@finflow.com"),
    }
    params.update(overrides)
    return create_invoice(**params)


def test_pdf_is_titled_with_the_issuer_not_the_product(readable_pdf):
    text = readable_pdf(generate_invoice_pdf(_invoice()))
    assert "Bambam Studio" in text
    assert "InvoiceCraft" not in text


def test_pdf_uses_the_invoice_currency_symbol(readable_pdf):
    text = readable_pdf(generate_invoice_pdf(_invoice(currency="EUR")))
    assert "€" in text


def test_pdf_embeds_a_data_url_logo():
    data_url = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    with_logo = generate_invoice_pdf(_invoice(), logo=data_url)
    without_logo = generate_invoice_pdf(_invoice())
    assert len(with_logo) > len(without_logo)


@pytest.mark.parametrize(
    "logo",
    [
        "not base64 at all!!",
        base64.b64encode(b"nonsense that is not an image").decode(),
        base64.b64encode(b"x" * (MAX_LOGO_BYTES + 1)).decode(),
    ],
)
def test_unusable_logos_are_skipped_not_fatal(logo):
    """A bad logo must never cost the caller their paid invoice."""
    pdf = generate_invoice_pdf(_invoice(), logo=logo)
    assert pdf.startswith(b"%PDF")
