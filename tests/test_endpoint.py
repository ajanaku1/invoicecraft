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


def test_invoice_no_payment(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    response = client.post(
        "/api/v1/invoice",
        json={"description": "Build a website for Acme Corp"},
    )
    assert response.status_code == 402
    data = response.json()
    assert data["error"] == "payment_required"
    assert "payment" in data
    assert data["payment"]["amount"] == "0.50"
    assert data["payment"]["token"] == "USDT"
    assert data["payment"]["chain"] == "eip155:196"
    assert "challenge_id" in data["payment"]


def test_invoice_with_payment(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")

    challenge_resp = client.post(
        "/api/v1/invoice",
        json={"description": "Web development for Acme Corp"},
    )
    assert challenge_resp.status_code == 402
    challenge_id = challenge_resp.json()["payment"]["challenge_id"]

    response = client.post(
        "/api/v1/invoice",
        json={
            "description": "Web development for Acme Corp",
            "payment_tx_hash": (
                "0x00000000000000000000000000000000"
                "00000000000000000000000000000001"
            ),
            "challenge_id": challenge_id,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "invoice" in data
    assert "pdf" in data
    assert data["invoice"]["status"] == "paid"
    assert data["invoice"]["issuer"]["address"] == "0xASPWalletAddress"


def test_invoice_empty_description():
    response = client.post(
        "/api/v1/invoice",
        json={"description": ""},
    )
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "invalid_description"


def test_invoice_short_description():
    response = client.post(
        "/api/v1/invoice",
        json={"description": "Hi"},
    )
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "invalid_description"


def test_invoice_too_long_description(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", "0xASPWalletAddress")
    desc = "Build a website. " * 1000
    response = client.post(
        "/api/v1/invoice",
        json={"description": desc},
    )
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "invalid_description"
