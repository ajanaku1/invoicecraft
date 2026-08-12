from __future__ import annotations

from dataclasses import replace

import pytest

from app.xrp.executor import (
    ExecutorError,
    FsaExecutionResult,
    SettlementPaymentPreparer,
    SettlementExecutor,
    SettlementUserOperationBuilder,
    XrplPaymentEvidence,
)
from app.xrp.instructions import build_custom_instruction, build_unsigned_payment
from app.xrp.repository import XrpInvoiceRepository


INVOICE_ID = "xrp_phase2_1"
BENEFICIARY = "0x" + "12" * 20
XRPL_HASH = "AB" * 32
MEMO = build_custom_instruction(0, 0, b"complete-packed-user-operation")
UNSIGNED = build_unsigned_payment("rSource", "rCoreVault", 10_200_000, MEMO)


def record(invoice_id: str = INVOICE_ID) -> dict[str, object]:
    return {
        "id": invoice_id,
        "state": "awaiting_signature",
        "network": "coston2",
        "beneficiary": BENEFICIARY,
        "canonical_hash": "0x" + "34" * 32,
        "invoice": {"total": "50.00", "currency": "USD"},
        "quote": {
            "expires_at": 2_000,
            "settlement_deadline_at": 2_800,
            "maximum_fxrp_uba": 30_000_000,
            "net_mint_uba": 25_000_000,
            "payment_amount_drops": 10_200_000,
            "memo_executor_fee_uba": 0,
            "core_vault": "rCoreVault",
        },
        "unsigned_payment": UNSIGNED,
        "fsa_evidence": {
            "packed_user_operation_hex": "0x" + MEMO.packed_user_operation.hex(),
            "user_op_hash": "0x" + MEMO.user_op_hash.hex(),
        },
        "receipt": None,
        "updated_at": 1_000,
    }


def payment(**changes: object) -> XrplPaymentEvidence:
    base = XrplPaymentEvidence(
        transaction_hash=XRPL_HASH,
        validated=True,
        result="tesSUCCESS",
        source_account="rSource",
        destination="rCoreVault",
        amount_drops=10_200_000,
        delivered_amount_drops=10_200_000,
        memo_data_hex=MEMO.memo_data_hex,
        destination_tag=None,
        flags=0,
        ledger_index=9_001,
        ledger_timestamp=1_099,
        fdc_round_id=812,
        fdc_proof_hash="0x" + "56" * 32,
    )
    return replace(base, **changes)


def result(status: str = "succeeded", **changes: object) -> FsaExecutionResult:
    base = FsaExecutionResult(
        status=status,
        flare_transaction_hash="0x" + "78" * 32,
        flare_block_number=12_345,
        settlement_contract="0x" + "90" * 20,
        adapter="0x" + "ab" * 20,
        usd0_token="0x" + "cd" * 20,
        beneficiary=BENEFICIARY,
        usd0_amount=50_000_000,
        settlement_id="0x" + "ef" * 32,
        adapter_event_index=4,
    )
    return replace(base, **changes)


class Reader:
    def __init__(self, evidence: XrplPaymentEvidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def read(self, transaction_hash: str) -> XrplPaymentEvidence:
        self.calls += 1
        assert transaction_hash == XRPL_HASH
        return self.evidence


class Gateway:
    def __init__(self, outcome: FsaExecutionResult) -> None:
        self.outcome = outcome
        self.calls = 0

    def execute(self, invoice: dict[str, object], evidence: XrplPaymentEvidence) -> FsaExecutionResult:
        self.calls += 1
        assert invoice["id"] == INVOICE_ID
        assert evidence.fdc_round_id == 812
        return self.outcome


class ProgressGateway(Gateway):
    def __init__(self) -> None:
        super().__init__(result("pending"))
        self.progress_calls = 0

    def progress(self, invoice: dict[str, object]) -> FsaExecutionResult:
        self.progress_calls += 1
        assert invoice["state"] == "flare_executing"
        return result("succeeded")


class UserOpBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def build(self, invoice: dict[str, object], source_account: str) -> bytes:
        self.calls += 1
        assert invoice["canonical_hash"] == "0x" + "34" * 32
        assert source_account == "rSource"
        return b"server-built-complete-user-operation"


@pytest.fixture
def repository(tmp_path, monkeypatch) -> XrpInvoiceRepository:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "phase2.sqlite"))
    value = XrpInvoiceRepository()
    assert value.create(INVOICE_ID, record())
    return value


def test_success_is_exact_dual_ledger_and_idempotent(repository: XrpInvoiceRepository) -> None:
    reader, gateway = Reader(payment()), Gateway(result())
    executor = SettlementExecutor(repository, reader, gateway, clock=lambda: 1_100)

    first = executor.submit(INVOICE_ID, XRPL_HASH)
    repeated = executor.submit(INVOICE_ID, XRPL_HASH)

    assert first == repeated
    assert first["state"] == "paid"
    assert first["receipt"]["xrpl"]["transaction_hash"] == XRPL_HASH
    assert first["receipt"]["flare"]["transaction_hash"] == "0x" + "78" * 32
    assert first["receipt"]["payout"]["amount_uba"] == 50_000_000
    assert first["receipt"]["fsa"]["packed_user_operation_hex"] == "0x" + MEMO.packed_user_operation.hex()
    assert reader.calls == gateway.calls == 1


def test_rejects_unfinalized_or_changed_xrpl_fields(repository: XrpInvoiceRepository) -> None:
    unsafe = (
        payment(validated=False),
        payment(result="tecFAILED"),
        payment(destination="rWrong"),
        payment(delivered_amount_drops=10_199_999),
        payment(memo_data_hex="FE" + "00" * 41),
        payment(destination_tag=7),
        payment(flags=0x00020000),
        payment(fdc_proof_hash=None),
    )
    for evidence in unsafe:
        fresh_id = f"{INVOICE_ID}_{unsafe.index(evidence)}"
        assert repository.create(fresh_id, record(fresh_id))
        with pytest.raises(ExecutorError):
            SettlementExecutor(repository, Reader(evidence), Gateway(result())).submit(fresh_id, XRPL_HASH)
        assert repository.get(fresh_id)["state"] == "payment_rejected"

    rejected = repository.get(f"{INVOICE_ID}_1")
    assert rejected["recovery"]["classification"] == "xrpl_rejected_no_funds_moved"


def test_transaction_replay_is_rejected_across_invoices(repository: XrpInvoiceRepository) -> None:
    first = SettlementExecutor(repository, Reader(payment()), Gateway(result()), clock=lambda: 1_100)
    first.submit(INVOICE_ID, XRPL_HASH)
    second_id = "xrp_phase2_2"
    assert repository.create(second_id, record(second_id))

    with pytest.raises(ExecutorError, match="replay"):
        SettlementExecutor(repository, Reader(payment()), Gateway(result()), clock=lambda: 1_100).submit(second_id, XRPL_HASH)


@pytest.mark.parametrize(
    ("status", "state", "classification"),
    [
        ("pending", "flare_executing", "flare_execution_pending_or_delayed"),
        ("reverted", "recovery_required", "flare_reverted_skip_memo_guidance"),
    ],
)
def test_nonfinal_flare_outcomes_are_guided_not_automatic(
    repository: XrpInvoiceRepository, status: str, state: str, classification: str
) -> None:
    updated = SettlementExecutor(
        repository, Reader(payment()), Gateway(result(status)), clock=lambda: 1_100
    ).submit(INVOICE_ID, XRPL_HASH)

    assert updated["state"] == state
    assert updated["recovery"]["classification"] == classification
    assert "automatic" not in updated["recovery"]["guidance"].lower()
    assert updated["receipt"] is None


def test_invoice_lock_and_transaction_claim_are_persistent_atomic(repository: XrpInvoiceRepository) -> None:
    assert repository.acquire_execution_lock(INVOICE_ID, "worker-a", now=100, ttl=30)
    assert not repository.acquire_execution_lock(INVOICE_ID, "worker-b", now=101, ttl=30)
    assert repository.claim_transaction(XRPL_HASH, INVOICE_ID) == INVOICE_ID
    assert repository.claim_transaction(XRPL_HASH, INVOICE_ID) == INVOICE_ID
    assert repository.release_execution_lock(INVOICE_ID, "worker-a")
    assert repository.acquire_execution_lock(INVOICE_ID, "worker-b", now=102, ttl=30)


def test_preparer_retains_complete_userop_and_builds_unsigned_payment(
    repository: XrpInvoiceRepository,
) -> None:
    pending = record()
    pending.update(state="quoted", unsigned_payment=None, fsa_evidence=None)
    assert repository.replace(INVOICE_ID, pending)
    builder = UserOpBuilder()
    preparer = SettlementPaymentPreparer(repository, builder, clock=lambda: 1_100)

    first = preparer.prepare(INVOICE_ID, "rSource")
    repeated = preparer.prepare(INVOICE_ID, "rSource")

    assert first == repeated
    assert first["unsigned_payment"]["Destination"] == "rCoreVault"
    assert first["unsigned_payment"]["Amount"] == "10200000"
    assert first["fsa_evidence"]["packed_user_operation_hex"] == "0x" + b"server-built-complete-user-operation".hex()
    assert first["unsigned_payment"]["Memos"][0]["Memo"]["MemoData"].startswith("FE00")
    assert builder.calls == 1


def test_settlement_userop_binds_quote_beneficiary_and_exact_payout() -> None:
    builder = SettlementUserOperationBuilder(
        personal_account="0x" + "11" * 20,
        nonce=7,
        fxrp_token="0x" + "33" * 20,
        settlement_contract="0x" + "22" * 20,
    )

    payload = builder.build(record())
    changed = record()
    changed["beneficiary"] = "0x" + "44" * 20

    assert bytes.fromhex("34" * 32) in payload
    assert bytes.fromhex("12" * 20) in payload
    assert (50_000_000).to_bytes(32, "big") in payload
    assert (30_000_000).to_bytes(32, "big") in payload
    assert (25_000_000).to_bytes(32, "big") in payload
    assert (2_800).to_bytes(32, "big") in payload
    assert bytes.fromhex("095ea7b3") in payload
    assert payload.index(bytes.fromhex("00" * 12 + "33" * 20)) < payload.index(
        bytes.fromhex("00" * 12 + "22" * 20)
    )
    assert builder.build(changed) != payload


def test_preparer_respects_the_persistent_invoice_lock(repository: XrpInvoiceRepository) -> None:
    pending = record()
    pending.update(state="quoted", unsigned_payment=None, fsa_evidence=None)
    assert repository.replace(INVOICE_ID, pending)
    assert repository.acquire_execution_lock(INVOICE_ID, "other-worker", now=1_100, ttl=30)

    with pytest.raises(ExecutorError, match="locked"):
        SettlementPaymentPreparer(repository, UserOpBuilder(), clock=lambda: 1_100).prepare(
            INVOICE_ID, "rSource"
        )


def test_pending_execution_progresses_once_to_paid(repository: XrpInvoiceRepository) -> None:
    gateway = ProgressGateway()
    executor = SettlementExecutor(repository, Reader(payment()), gateway, clock=lambda: 1_100)

    pending = executor.submit(INVOICE_ID, XRPL_HASH)
    paid = executor.progress(INVOICE_ID)
    repeated = executor.progress(INVOICE_ID)

    assert pending["state"] == "flare_executing"
    assert paid["state"] == "paid"
    assert paid == repeated
    assert gateway.progress_calls == 1


def test_submit_and_get_routes_use_injected_executor(
    repository: XrpInvoiceRepository, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    gateway = ProgressGateway()
    executor = SettlementExecutor(repository, Reader(payment()), gateway, clock=lambda: 1_100)
    monkeypatch.setattr(app.state, "xrp_executor", executor, raising=False)
    client = TestClient(app)

    submitted = client.post(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/submit",
        json={"xrpl_transaction_hash": XRPL_HASH},
    )
    progressed = client.get(f"/api/v1/xrp/invoices/{INVOICE_ID}")

    assert submitted.status_code == 200
    assert submitted.json()["state"] == "flare_executing"
    assert progressed.status_code == 200
    assert progressed.json()["state"] == "paid"


def test_signing_route_uses_server_owned_userop_preparer(
    repository: XrpInvoiceRepository, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.xrp.xaman import SigningResult

    pending = record()
    pending.update(state="quoted", unsigned_payment=None, fsa_evidence=None)
    assert repository.replace(INVOICE_ID, pending)
    builder = UserOpBuilder()
    preparer = SettlementPaymentPreparer(repository, builder, clock=lambda: 1_100)

    class UnsignedGateway:
        def create(self, unsigned: dict[str, object]) -> SigningResult:
            return SigningResult(mode="unsigned", unsigned_transaction=unsigned)

    monkeypatch.setattr(app.state, "xrp_payment_preparer", preparer, raising=False)
    monkeypatch.setattr(app.state, "xrp_xaman_gateway", UnsignedGateway(), raising=False)
    monkeypatch.setattr(app.state, "xrp_clock", lambda: 1_100, raising=False)

    response = TestClient(app).post(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/signing-request",
        json={"source_account": "rSource"},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "awaiting_signature"
    assert response.json()["signing_request"]["mode"] == "unsigned"
    assert response.json()["fsa_evidence"]["packed_user_operation_hex"].startswith("0x")
    assert builder.calls == 1


def test_submit_route_rejects_a_nonhex_transaction_hash(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).post(
        f"/api/v1/xrp/invoices/{INVOICE_ID}/submit",
        json={"xrpl_transaction_hash": "Z" * 64},
    )

    assert response.status_code == 422
