import base64
import json
import re

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "InvoiceCraft"


def test_invoice_no_payment_returns_x402(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    monkeypatch.setenv("USDT_CONTRACT", "0x779ded0c9e1022225f8e0630b35a9b54be713736")
    response = client.post(
        "/api/v1/invoice",
        json={"description": "Build a website for Acme Corp"},
    )
    assert response.status_code == 402
    # Requirements must be advertised in the payment-required header (x402).
    assert "payment-required" in response.headers
    data = response.json()
    assert data["x402Version"] == 2
    assert data["error"] == "Payment required"
    accept = data["accepts"][0]
    assert accept["scheme"] == "exact"
    assert accept["network"] == "eip155:196"
    assert accept["amount"] == "500000"  # 0.50 USDT in base units (6 decimals)
    assert accept["payTo"] == "0xASPWalletAddress"
    assert accept["asset"] == "0x779ded0c9e1022225f8e0630b35a9b54be713736"
    # Header decodes to the same requirements object.
    decoded = json.loads(base64.b64decode(response.headers["payment-required"]))
    assert decoded == data


def test_invoice_with_payment_no_challenge(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    response = client.post(
        "/api/v1/invoice",
        json={
            "description": "Web development for Acme Corp",
            "payment_tx_hash": (
                "0x00000000000000000000000000000000"
                "00000000000000000000000000000001"
            ),
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "invoice" in data
    assert "pdf" in data
    assert data["invoice"]["status"] == "paid"
    # The settlement wallet is not the issuer's postal address; it stays blank
    # unless the caller supplies one.
    assert data["invoice"]["issuer"]["address"] == ""


GOOD_TX = "0x" + "01" * 32


def test_probe_returns_x402(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    # The x402 caller probes with an empty/minimal body — it must get the 402,
    # not a validation error, or it cannot obtain the payment requirements.
    assert client.post("/api/v1/invoice", json={}).status_code == 402
    assert client.post("/api/v1/invoice").status_code == 402


def test_invoice_empty_description(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    response = client.post(
        "/api/v1/invoice",
        json={"description": "", "payment_tx_hash": GOOD_TX},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_description"


def test_invoice_short_description(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    response = client.post(
        "/api/v1/invoice",
        json={"description": "Hi", "payment_tx_hash": GOOD_TX},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_description"


def test_invoice_too_long_description(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    desc = "Build a website. " * 1000
    response = client.post(
        "/api/v1/invoice",
        json={"description": desc, "payment_tx_hash": GOOD_TX},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_description"
