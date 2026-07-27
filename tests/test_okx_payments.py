"""Tests for the official OKX Payment SDK integration.

The SDK middleware is driven with a stub facilitator so the handshake, the 402
it emits, and the route pattern are exercised without OKX credentials.
"""

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.okx_payments import PROTECTED_ROUTE, install_payment_middleware, payments_enabled

pytest.importorskip("x402", reason="okxweb3-app-x402 not installed")

WALLET = "0xc11bf6e5809835213fcd64e2e45409117bdd36cc"


class StubFacilitator:
    """Facilitator that advertises exact/eip155:196 support and nothing else."""

    def get_supported(self):
        from x402.schemas.responses import SupportedKind, SupportedResponse

        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme="exact", network="eip155:196")]
        )


@pytest.fixture
def okx_env(monkeypatch):
    monkeypatch.setenv("ASP_WALLET", WALLET)
    monkeypatch.setenv("PAYMENT_CHAIN", "eip155:196")
    monkeypatch.setenv("INVOICE_PRICE_USDT", "0.50")


def _app_with_middleware():
    app = FastAPI()

    @app.post("/api/v1/invoice")
    async def invoice():  # pragma: no cover - only reached when payment verifies
        return {"ok": True}

    installed = install_payment_middleware(app, facilitator=StubFacilitator())
    return app, installed


def test_disabled_without_credentials(monkeypatch):
    for key in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        monkeypatch.delenv(key, raising=False)
    assert payments_enabled() is False
    assert install_payment_middleware(FastAPI()) is False


def test_enabled_requires_wallet_and_all_credentials(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.setenv("OKX_SECRET_KEY", "s")
    monkeypatch.setenv("OKX_PASSPHRASE", "p")
    monkeypatch.delenv("ASP_WALLET", raising=False)
    assert payments_enabled() is False
    monkeypatch.setenv("ASP_WALLET", WALLET)
    assert payments_enabled() is True


def test_sdk_returns_402_for_unpaid_probe(okx_env):
    app, installed = _app_with_middleware()
    assert installed is True

    response = TestClient(app).post("/api/v1/invoice", json={})

    assert response.status_code == 402
    header = response.headers.get("payment-required")
    assert header, "SDK must advertise requirements in the PAYMENT-REQUIRED header"
    advertised = json.loads(base64.b64decode(header))
    accepts = advertised["accepts"][0]
    assert accepts["scheme"] == "exact"
    assert accepts["network"] == "eip155:196"
    assert accepts["payTo"].lower() == WALLET
    # 0.50 USD at 6 decimals, in base units.
    assert accepts.get("maxAmountRequired", accepts.get("amount")) == "500000"


def test_unprotected_routes_are_untouched(okx_env):
    app, _ = _app_with_middleware()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    assert TestClient(app).get("/health").status_code == 200


def test_protected_route_matches_the_live_endpoint():
    assert PROTECTED_ROUTE == "POST /api/v1/invoice"
