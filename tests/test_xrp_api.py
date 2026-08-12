import hashlib
from decimal import Decimal

from fastapi.testclient import TestClient

import app.xrp.api as xrp_api
from app.main import app
from app.xrp.quotes import QuoteParameters
from app.xrp.repository import XrpInvoiceRepository
from app.xrp.xaman import SigningResult


BENEFICIARY = "0x" + "12" * 20
OTHER_BENEFICIARY = "0x" + "34" * 20
INVOICE = {
    "description": "Design and build the Acme launch page",
    "beneficiary": BENEFICIARY,
    "issuer": {"name": "Bambam Studio", "email": "billing@example.com"},
    "client": {"name": "Acme"},
    "currency": "USD",
    "tax_rate": "0",
}


class RecordingQuoteProvider:
    def __init__(self) -> None:
        self.calls = 0

    def read(self) -> QuoteParameters:
        self.calls += 1
        return QuoteParameters(
            xrp_usd_price=Decimal("2"),
            price_decimals=6,
            price_timestamp=1_000,
            lot_size_uba=10_000_000,
            minimum_redeem_uba=5_000_000,
            minimum_fee_uba=100_000,
            fee_bips=25,
            memo_executor_fee_uba=0,
            core_vault="rCoreVault",
            source_block="0x123",
        )


def test_create_is_free_persistent_and_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(app.state, "xrp_clock", lambda: 1_030, raising=False)
    client = TestClient(app)

    created = client.post("/api/v1/xrp/invoices", json=INVOICE, headers={"Idempotency-Key": "demo-1"})
    repeated = client.post("/api/v1/xrp/invoices", json=INVOICE, headers={"Idempotency-Key": "demo-1"})

    assert created.status_code == 201
    assert repeated.status_code == 200
    assert created.json() == repeated.json()
    document = created.json()
    assert document["state"] == "open"
    assert document["beneficiary"] == BENEFICIARY
    assert document["share_url"] == f"/pay/{document['id']}"
    predictable = "xrp_" + hashlib.sha256(b"demo-1").hexdigest()[:24]
    assert document["id"] != predictable
    assert client.get(f"/api/v1/xrp/invoices/{document['id']}").json() == document
    shared = client.get(document["share_url"])
    assert shared.status_code == 200
    assert shared.headers["content-type"].startswith("text/html")
    assert 'id="settlementDocket"' in shared.text


def test_idempotency_key_rejects_a_different_request(monkeypatch) -> None:
    monkeypatch.setattr(app.state, "xrp_clock", lambda: 1_030, raising=False)
    client = TestClient(app)
    assert client.post("/api/v1/xrp/invoices", json=INVOICE, headers={"Idempotency-Key": "demo-1"}).status_code == 201

    changed = {**INVOICE, "beneficiary": OTHER_BENEFICIARY}
    response = client.post("/api/v1/xrp/invoices", json=changed, headers={"Idempotency-Key": "demo-1"})

    assert response.status_code == 409
    assert response.json()["error"] == "idempotency_conflict"


def test_quote_is_live_bounded_and_cached_until_expiry(monkeypatch) -> None:
    provider = RecordingQuoteProvider()
    monkeypatch.setattr(app.state, "xrp_quote_provider", provider, raising=False)
    monkeypatch.setattr(app.state, "xrp_clock", lambda: 1_030, raising=False)
    client = TestClient(app)
    invoice = client.post("/api/v1/xrp/invoices", json=INVOICE).json()

    first = client.post(f"/api/v1/xrp/invoices/{invoice['id']}/quote")
    second = client.post(f"/api/v1/xrp/invoices/{invoice['id']}/quote")

    assert first.status_code == 200
    assert second.json() == first.json()
    assert provider.calls == 1
    quoted = first.json()
    assert quoted["state"] == "quoted"
    assert quoted["quote"]["expires_at"] == 1_150
    assert quoted["quote"]["payment_amount_drops"] == 253_131_250
    assert quoted["canonical_hash"] == quoted["quote"]["quote_hash"]


def test_refreshing_an_expired_quote_clears_stale_signing_artifacts(monkeypatch) -> None:
    provider = RecordingQuoteProvider()
    monkeypatch.setattr(app.state, "xrp_quote_provider", provider, raising=False)
    monkeypatch.setattr(app.state, "xrp_clock", lambda: 1_100, raising=False)
    client = TestClient(app)
    invoice = client.post("/api/v1/xrp/invoices", json=INVOICE).json()
    repository = XrpInvoiceRepository()
    stale = repository.get(invoice["id"])
    assert stale is not None
    stale.update(
        state="awaiting_signature",
        quote={"expires_at": 1_050},
        unsigned_payment={"Account": "rOld"},
        fsa_evidence={"user_op_hash": "0xold"},
        signing_request={"mode": "xaman", "uuid": "old"},
    )
    assert repository.replace(invoice["id"], stale)

    refreshed = client.post(f"/api/v1/xrp/invoices/{invoice['id']}/quote")

    assert refreshed.status_code == 200
    assert refreshed.json()["state"] == "quoted"
    assert refreshed.json().get("unsigned_payment") is None
    assert refreshed.json().get("fsa_evidence") is None
    assert refreshed.json().get("signing_request") is None


def test_signing_request_fails_closed_until_phase2_prepares_userop(monkeypatch) -> None:
    monkeypatch.setattr(app.state, "xrp_clock", lambda: 1_030, raising=False)
    client = TestClient(app)
    invoice = client.post("/api/v1/xrp/invoices", json=INVOICE).json()

    response = client.post(
        f"/api/v1/xrp/invoices/{invoice['id']}/signing-request",
        json={"source_account": "rSource"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "signing_not_ready"


def test_prepared_signing_request_is_idempotent_and_server_owned(monkeypatch) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, unsigned_payment: dict[str, object]) -> SigningResult:
            self.calls += 1
            return SigningResult(mode="unsigned", unsigned_transaction=unsigned_payment)

    gateway = Gateway()
    monkeypatch.setattr(app.state, "xrp_xaman_gateway", gateway, raising=False)
    monkeypatch.setattr(app.state, "xrp_clock", lambda: 1_030, raising=False)
    client = TestClient(app)
    invoice = client.post("/api/v1/xrp/invoices", json=INVOICE).json()
    repository = XrpInvoiceRepository()
    stored = repository.get(invoice["id"])
    assert stored is not None
    stored["unsigned_payment"] = {
        "TransactionType": "Payment",
        "Account": "rSource",
        "Destination": "rCoreVault",
        "Amount": "10100000",
        "Memos": [{"Memo": {"MemoData": "FE" + "00" * 41}}],
    }
    assert repository.replace(invoice["id"], stored)

    first = client.post(
        f"/api/v1/xrp/invoices/{invoice['id']}/signing-request",
        json={"source_account": "rSource"},
    )
    second = client.post(
        f"/api/v1/xrp/invoices/{invoice['id']}/signing-request",
        json={"source_account": "rSource"},
    )

    assert first.status_code == 200
    assert second.json() == first.json()
    assert first.json()["state"] == "awaiting_signature"
    assert first.json()["signing_request"]["mode"] == "unsigned"
    assert gateway.calls == 1

    mismatch = client.post(
        f"/api/v1/xrp/invoices/{invoice['id']}/signing-request",
        json={"source_account": "rDifferent"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"] == "source_account_mismatch"


def test_submit_and_receipt_fail_closed_before_phase2(monkeypatch) -> None:
    monkeypatch.setattr(app.state, "xrp_clock", lambda: 1_030, raising=False)
    client = TestClient(app)
    invoice = client.post("/api/v1/xrp/invoices", json=INVOICE).json()

    submitted = client.post(
        f"/api/v1/xrp/invoices/{invoice['id']}/submit",
        json={"xrpl_transaction_hash": "AB" * 32},
    )
    receipt = client.get(f"/api/v1/xrp/invoices/{invoice['id']}/receipt")

    assert submitted.status_code == 409
    assert submitted.json()["error"] == "executor_not_ready"
    assert receipt.status_code == 409
    assert receipt.json()["error"] == "receipt_not_ready"


def test_invalid_beneficiary_and_missing_invoice_are_explicit() -> None:
    client = TestClient(app)
    invalid = client.post("/api/v1/xrp/invoices", json={**INVOICE, "beneficiary": "0x1234"})
    missing = client.get("/api/v1/xrp/invoices/does-not-exist")

    assert invalid.status_code == 422
    assert missing.status_code == 404
    assert missing.json()["error"] == "invoice_not_found"


def test_invoice_parser_failure_is_structured(monkeypatch) -> None:
    monkeypatch.setattr(xrp_api, "create_invoice", lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad parse")))
    response = TestClient(app).post("/api/v1/xrp/invoices", json=INVOICE)

    assert response.status_code == 422
    assert response.json()["error"] == "invoice_parse_error"
