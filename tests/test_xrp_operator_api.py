from fastapi.testclient import TestClient

from app.main import app, configure_xrp_browser_operator
from app.xrp.operator import BrowserOperatorCoordinator


INVOICE_ID = "xrp_operator_demo"
XRPL_HASH = "AB" * 32
EVM_HASH = "0x" + "12" * 32
TOKEN = "operator-test-token"
INVOICE = {
    "description": "Settle the operator API test invoice",
    "beneficiary": "0x" + "90" * 20,
    "currency": "USD",
    "tax_rate": "0",
}


class RecordingOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.job: dict[str, object] = {
            "invoice_id": INVOICE_ID,
            "stage": "prepare_fdc",
            "xrpl_transaction_hash": XRPL_HASH,
        }

    def submit(self, invoice_id: str, transaction_hash: str) -> dict[str, object]:
        self.calls.append(("submit", invoice_id, transaction_hash))
        return {"id": invoice_id, "state": "flare_executing", "operator_job": self.job}

    def get_job(self, invoice_id: str) -> dict[str, object]:
        self.calls.append(("get", invoice_id))
        return self.job

    def prepare(self, invoice_id: str) -> dict[str, object]:
        self.calls.append(("prepare", invoice_id))
        self.job = {
            **self.job,
            "stage": "awaiting_fdc_transaction",
            "sign_request": {
                "chain_id": "0x72",
                "purpose": "fdc-request",
                "signer": "0x" + "34" * 20,
                "to": "0x" + "56" * 20,
                "value": "0x1",
                "data": "0x1234",
                "calldata_hash": "0x" + "78" * 32,
            },
        }
        return self.job

    def record(self, invoice_id: str, transaction_hash: str) -> dict[str, object]:
        self.calls.append(("record", invoice_id, transaction_hash))
        self.job = {
            **self.job,
            "stage": "prepare_execute",
            "fdc_transaction_hash": transaction_hash,
        }
        self.job.pop("sign_request", None)
        return self.job


def _client(monkeypatch, operator: RecordingOperator | None = None) -> TestClient:
    monkeypatch.setattr(app.state, "xrp_operator_token", TOKEN, raising=False)
    if operator is not None:
        monkeypatch.setattr(app.state, "xrp_operator_coordinator", operator, raising=False)
    return TestClient(app)


def test_submit_uses_keyless_operator_queue_when_configured(monkeypatch) -> None:
    operator = RecordingOperator()
    client = _client(monkeypatch, operator)
    invoice_id = client.post("/api/v1/xrp/invoices", json=INVOICE).json()["id"]

    response = client.post(
        f"/api/v1/xrp/invoices/{invoice_id}/submit",
        json={"xrpl_transaction_hash": XRPL_HASH},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "flare_executing"
    assert operator.calls == [("submit", invoice_id, XRPL_HASH)]


def test_operator_job_requires_configured_token(monkeypatch) -> None:
    operator = RecordingOperator()
    monkeypatch.delenv("XRP_OPERATOR_TOKEN", raising=False)
    monkeypatch.delattr(app.state, "xrp_operator_token", raising=False)
    monkeypatch.setattr(app.state, "xrp_operator_coordinator", operator, raising=False)

    response = TestClient(app).get(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/operator-job",
        headers={"X-Operator-Token": TOKEN},
    )

    assert response.status_code == 503
    assert response.json()["error"] == "operator_unavailable"
    assert operator.calls == []


def test_operator_job_rejects_missing_or_wrong_token_without_leaking_it(monkeypatch) -> None:
    operator = RecordingOperator()
    client = _client(monkeypatch, operator)

    missing = client.get(f"/api/v1/xrp/invoices/{INVOICE_ID}/operator-job")
    wrong = client.get(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/operator-job",
        headers={"X-Operator-Token": "wrong-token"},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert TOKEN not in missing.text
    assert TOKEN not in wrong.text
    assert operator.calls == []


def test_operator_job_get_prepare_and_record_are_resource_oriented(monkeypatch) -> None:
    operator = RecordingOperator()
    client = _client(monkeypatch, operator)
    headers = {"X-Operator-Token": TOKEN}

    current = client.get(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/operator-job", headers=headers
    )
    prepared = client.post(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/operator-job", headers=headers
    )
    recorded = client.patch(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/operator-job",
        headers=headers,
        json={"transaction_hash": EVM_HASH},
    )

    assert current.status_code == 200
    assert current.json()["stage"] == "prepare_fdc"
    assert prepared.json()["stage"] == "awaiting_fdc_transaction"
    assert prepared.json()["sign_request"]["purpose"] == "fdc-request"
    assert recorded.json()["stage"] == "prepare_execute"
    assert recorded.json()["fdc_transaction_hash"] == EVM_HASH
    assert operator.calls == [
        ("get", INVOICE_ID),
        ("prepare", INVOICE_ID),
        ("record", INVOICE_ID, EVM_HASH),
    ]


def test_operator_transaction_hash_is_strictly_validated(monkeypatch) -> None:
    operator = RecordingOperator()
    client = _client(monkeypatch, operator)

    response = client.patch(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/operator-job",
        headers={"X-Operator-Token": TOKEN},
        json={"transaction_hash": "0x1234"},
    )

    assert response.status_code == 422
    assert operator.calls == []


def test_runtime_queue_requires_complete_public_bindings_and_no_wallet_key(
    monkeypatch,
) -> None:
    monkeypatch.setattr(app.state, "xrp_operator_coordinator", None, raising=False)
    monkeypatch.setattr(app.state, "xrp_payment_preparer", None, raising=False)
    values = {
        "XRP_OPERATOR_TOKEN": TOKEN,
        "COSTON2_SIGNER_ADDRESS": "0x" + "11" * 20,
        "VERIFIER_URL_TESTNET": "https://fdc-verifiers-testnet.flare.network",
        "VERIFIER_API_KEY_TESTNET": "server-verifier-key",
        "COSTON2_DA_LAYER_URL": "https://ctn2-data-availability.flare.network",
        "XRP_SETTLEMENT_CONTRACT": "0x" + "22" * 20,
        "XRP_LIQUIDITY_ADAPTER": "0x" + "33" * 20,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    assert configure_xrp_browser_operator() is True
    assert isinstance(
        app.state.xrp_operator_coordinator, BrowserOperatorCoordinator
    )
    assert "PRIVATE_KEY" not in values
