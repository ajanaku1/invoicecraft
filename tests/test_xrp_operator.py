from __future__ import annotations

from dataclasses import replace

import pytest

from app.xrp.executor import FsaExecutionResult, XrplPaymentEvidence
from app.xrp.instructions import build_custom_instruction, build_unsigned_payment
from app.xrp.operator import (
    BrowserOperatorCoordinator,
    OperatorError,
    OperatorPreparation,
    OperatorProgress,
)
from app.xrp.repository import XrpInvoiceRepository


INVOICE_ID = "xrp_browser_queue"
XRPL_HASH = "AB" * 32
FDC_HASH = "0x" + "12" * 32
EXECUTE_HASH = "0x" + "34" * 32
BENEFICIARY = "0x" + "56" * 20
MEMO = build_custom_instruction(0, 0, b"product-packed-user-operation")
UNSIGNED = build_unsigned_payment("rSource", "rCoreVault", 10_200_000, MEMO)


def record() -> dict[str, object]:
    return {
        "id": INVOICE_ID,
        "state": "awaiting_signature",
        "network": "coston2",
        "beneficiary": BENEFICIARY,
        "canonical_hash": "0x" + "78" * 32,
        "invoice": {"total": "50.00", "currency": "USD"},
        "quote": {
            "expires_at": 2_000,
            "payment_amount_drops": 10_200_000,
            "net_mint_uba": 25_000_000,
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
    value = XrplPaymentEvidence(
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
        fdc_round_id=None,
        fdc_proof_hash=None,
    )
    return replace(value, **changes)


class Reader:
    def __init__(self, evidence: XrplPaymentEvidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def read(self, transaction_hash: str) -> XrplPaymentEvidence:
        self.calls += 1
        assert transaction_hash == XRPL_HASH
        return self.evidence


class Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(
        self, invoice: dict[str, object], evidence: XrplPaymentEvidence
    ) -> dict[str, object]:
        self.calls.append("start")
        assert invoice["id"] == INVOICE_ID
        assert evidence.fdc_round_id is None
        return {"public_checkpoint": "ready"}

    def prepare_fdc(self, context: dict[str, object]) -> OperatorPreparation:
        self.calls.append("prepare_fdc")
        return OperatorPreparation(
            sign_request=_sign_request("fdc-request"),
            context={**context, "fdc_request_bytes": "0x1234"},
        )

    def record_fdc(
        self, context: dict[str, object], transaction_hash: str
    ) -> OperatorProgress:
        self.calls.append("record_fdc")
        assert transaction_hash == FDC_HASH
        return OperatorProgress(
            context={**context, "fdc_round_id": 812},
            fdc_round_id=812,
        )

    def prepare_execute(self, context: dict[str, object]) -> OperatorPreparation:
        self.calls.append("prepare_execute")
        return OperatorPreparation(
            sign_request=_sign_request("execute-direct-mint"),
            context={**context, "fdc_proof_hash": "0x" + "90" * 32},
            fdc_proof_hash="0x" + "90" * 32,
        )

    def finalize(
        self, context: dict[str, object], transaction_hash: str
    ) -> FsaExecutionResult:
        self.calls.append("finalize")
        assert transaction_hash == EXECUTE_HASH
        assert context["fdc_round_id"] == 812
        return FsaExecutionResult(
            status="succeeded",
            flare_transaction_hash=transaction_hash,
            flare_block_number=12_345,
            settlement_contract="0x" + "ab" * 20,
            adapter="0x" + "cd" * 20,
            usd0_token="0x" + "ef" * 20,
            beneficiary=BENEFICIARY,
            usd0_amount=50_000_000,
            settlement_id="0x" + "11" * 32,
            adapter_event_index=4,
        )


def _sign_request(purpose: str) -> dict[str, str]:
    return {
        "version": "1",
        "purpose": purpose,
        "chain_id": "0x72",
        "signer": "0x" + "22" * 20,
        "to": "0x" + "33" * 20,
        "value": "0x0",
        "data": "0x1234",
        "calldata_hash": "0x" + "44" * 32,
    }


@pytest.fixture
def repository() -> XrpInvoiceRepository:
    value = XrpInvoiceRepository()
    assert value.create(INVOICE_ID, record())
    return value


def test_submit_creates_an_idempotent_keyless_job(repository: XrpInvoiceRepository) -> None:
    reader, backend = Reader(payment()), Backend()
    coordinator = BrowserOperatorCoordinator(
        repository, reader, backend, clock=lambda: 1_100
    )

    first = coordinator.submit(INVOICE_ID, XRPL_HASH)
    repeated = coordinator.submit(INVOICE_ID, XRPL_HASH)

    assert first == repeated
    assert first["state"] == "flare_executing"
    assert first["operator_job"]["stage"] == "prepare_fdc"
    assert first["operator_job"]["xrpl_transaction_hash"] == XRPL_HASH
    assert reader.calls == 1
    assert backend.calls == ["start"]


def test_submit_uses_xrpl_ledger_time_for_quote_expiry(
    repository: XrpInvoiceRepository,
) -> None:
    evidence = replace(payment(), ledger_timestamp=1_999)
    coordinator = BrowserOperatorCoordinator(
        repository, Reader(evidence), Backend(), clock=lambda: 2_100
    )

    submitted = coordinator.submit(INVOICE_ID, XRPL_HASH)

    assert submitted["state"] == "flare_executing"


def test_submit_rejects_payment_that_landed_after_quote_expiry(
    repository: XrpInvoiceRepository,
) -> None:
    evidence = replace(payment(), ledger_timestamp=2_001)
    coordinator = BrowserOperatorCoordinator(
        repository, Reader(evidence), Backend(), clock=lambda: 2_100
    )

    with pytest.raises(OperatorError, match="landed after"):
        coordinator.submit(INVOICE_ID, XRPL_HASH)


def test_queue_advances_only_after_matching_mined_hashes(
    repository: XrpInvoiceRepository,
) -> None:
    backend = Backend()
    coordinator = BrowserOperatorCoordinator(
        repository, Reader(payment()), backend, clock=lambda: 1_100
    )
    coordinator.submit(INVOICE_ID, XRPL_HASH)

    fdc = coordinator.prepare(INVOICE_ID)
    repeated_fdc = coordinator.prepare(INVOICE_ID)
    fdc_recorded = coordinator.record(INVOICE_ID, FDC_HASH)
    execute = coordinator.prepare(INVOICE_ID)
    repeated_execute = coordinator.prepare(INVOICE_ID)
    paid = coordinator.record(INVOICE_ID, EXECUTE_HASH)

    assert fdc == repeated_fdc
    assert fdc["stage"] == "awaiting_fdc_transaction"
    assert fdc["sign_request"]["purpose"] == "fdc-request"
    assert fdc_recorded["stage"] == "prepare_execute"
    assert "sign_request" not in fdc_recorded
    assert execute == repeated_execute
    assert execute["sign_request"]["purpose"] == "execute-direct-mint"
    assert paid["stage"] == "complete"
    assert paid["execute_transaction_hash"] == EXECUTE_HASH
    stored = repository.get(INVOICE_ID)
    assert stored is not None
    assert stored["state"] == "paid"
    assert stored["receipt"]["payout"]["amount_uba"] == 50_000_000
    assert backend.calls == [
        "start",
        "prepare_fdc",
        "record_fdc",
        "prepare_execute",
        "finalize",
    ]


def test_record_is_idempotent_but_rejects_a_replacement_hash(
    repository: XrpInvoiceRepository,
) -> None:
    coordinator = BrowserOperatorCoordinator(
        repository, Reader(payment()), Backend(), clock=lambda: 1_100
    )
    coordinator.submit(INVOICE_ID, XRPL_HASH)
    coordinator.prepare(INVOICE_ID)
    first = coordinator.record(INVOICE_ID, FDC_HASH)

    assert coordinator.record(INVOICE_ID, FDC_HASH) == first
    with pytest.raises(OperatorError, match="different transaction"):
        coordinator.record(INVOICE_ID, "0x" + "99" * 32)


def test_backend_state_cannot_persist_secret_like_fields(
    repository: XrpInvoiceRepository,
) -> None:
    class UnsafeBackend(Backend):
        def start(
            self, invoice: dict[str, object], evidence: XrplPaymentEvidence
        ) -> dict[str, object]:
            return {"private_key": "must-never-persist"}

    coordinator = BrowserOperatorCoordinator(
        repository, Reader(payment()), UnsafeBackend(), clock=lambda: 1_100
    )

    with pytest.raises(OperatorError, match="secret-like"):
        coordinator.submit(INVOICE_ID, XRPL_HASH)
    stored = repository.get(INVOICE_ID)
    assert stored is not None
    assert "must-never-persist" not in str(stored)
