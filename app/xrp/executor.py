"""Idempotent FSA/FDC settlement execution boundary."""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterator, Literal, Protocol

from .instructions import (
    InstructionError,
    build_contract_user_operations,
    build_custom_instruction,
    build_unsigned_payment,
    keccak256,
)
from .models import RecoveryObservation, classify_recovery
from .repository import XrpInvoiceRepository
from .rpc import COSTON2_CHAIN_ID


class ExecutorError(ValueError):
    """Raised when settlement evidence or progression is unsafe."""


@dataclass(frozen=True)
class XrplPaymentEvidence:
    transaction_hash: str
    validated: bool
    result: str
    source_account: str
    destination: str
    amount_drops: int
    delivered_amount_drops: int
    memo_data_hex: str
    destination_tag: int | None
    flags: int
    ledger_index: int
    ledger_timestamp: int
    fdc_round_id: int | None
    fdc_proof_hash: str | None


@dataclass(frozen=True)
class FsaExecutionResult:
    status: str
    flare_transaction_hash: str
    flare_block_number: int
    settlement_contract: str
    adapter: str
    usd0_token: str
    beneficiary: str
    usd0_amount: int
    settlement_id: str
    adapter_event_index: int


class XrplEvidenceReader(Protocol):
    def read(self, transaction_hash: str) -> XrplPaymentEvidence: ...


class FsaGateway(Protocol):
    def execute(
        self, invoice: dict[str, object], evidence: XrplPaymentEvidence
    ) -> FsaExecutionResult: ...

    def progress(self, invoice: dict[str, object]) -> FsaExecutionResult: ...


class UserOperationBuilder(Protocol):
    def build(self, invoice: dict[str, object], source_account: str) -> bytes: ...


class SettlementUserOperationBuilder:
    def __init__(
        self,
        personal_account: str,
        nonce: int,
        fxrp_token: str,
        settlement_contract: str,
    ) -> None:
        self.personal_account = personal_account
        self.nonce = nonce
        self.fxrp_token = fxrp_token
        self.settlement_contract = settlement_contract

    def build(
        self, invoice: dict[str, object], _source_account: str = ""
    ) -> bytes:
        try:
            quote = _mapping(invoice, "quote")
            invoice_hash = _bytes32(_nonempty_text(invoice, "canonical_hash"))
            beneficiary = _address_bytes(_nonempty_text(invoice, "beneficiary"))
            exact = _invoice_amount_uba(invoice)
            maximum = _positive_int(quote, "maximum_fxrp_uba")
            fxrp_input = _positive_int(quote, "net_mint_uba")
            deadline = _positive_int(quote, "settlement_deadline_at")
            if fxrp_input > maximum:
                raise ExecutorError("quoted FXRP input exceeds its settlement cap")
            calldata = _settlement_calldata(
                invoice_hash, beneficiary, exact, maximum, deadline, fxrp_input,
                _address_bytes(self.settlement_contract),
            )
            approve = _approve_calldata(
                _address_bytes(self.settlement_contract), fxrp_input
            )
            return build_contract_user_operations(
                self.personal_account,
                self.nonce,
                (
                    (self.fxrp_token, approve),
                    (self.settlement_contract, calldata),
                ),
            )
        except InstructionError as error:
            raise ExecutorError("settlement UserOp configuration is invalid") from error


class SettlementPaymentPreparer:
    def __init__(
        self,
        repository: XrpInvoiceRepository,
        builder: UserOperationBuilder,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.repository = repository
        self.builder = builder
        self.clock = clock or (lambda: int(time.time()))

    def prepare(self, invoice_id: str, source_account: str) -> dict[str, object]:
        now = self.clock()
        with _execution_lock(self.repository, invoice_id, now):
            return self._prepare_locked(invoice_id, source_account, now)

    def _prepare_locked(
        self, invoice_id: str, source_account: str, now: int
    ) -> dict[str, object]:
        record = self.repository.get(invoice_id)
        if record is None:
            raise ExecutorError("XRP invoice was not found")
        existing = record.get("unsigned_payment")
        if isinstance(existing, dict):
            if existing.get("Account") != source_account:
                raise ExecutorError("source account does not match prepared payment")
            return record
        quote = _mapping(record, "quote")
        _require_current_quote(quote, now)
        payload = self.builder.build(record, source_account)
        if not isinstance(payload, bytes) or not payload:
            raise ExecutorError("server-built PackedUserOperation is malformed")
        updated = _with_prepared_payment(record, source_account, quote, payload, now)
        if not self.repository.replace(invoice_id, updated):
            raise ExecutorError("invoice changed while preparing payment")
        return updated


def _with_prepared_payment(
    record: dict[str, object],
    source_account: str,
    quote: dict[str, object],
    payload: bytes,
    now: int,
) -> dict[str, object]:
    instruction = build_custom_instruction(
        0, _nonnegative_int(quote, "memo_executor_fee_uba"), payload
    )
    unsigned = build_unsigned_payment(
        source_account,
        _nonempty_text(quote, "core_vault"),
        _positive_int(quote, "payment_amount_drops"),
        instruction,
    )
    updated = dict(record)
    updated["unsigned_payment"] = unsigned
    updated["fsa_evidence"] = {
        "packed_user_operation_hex": "0x" + payload.hex(),
        "user_op_hash": "0x" + instruction.user_op_hash.hex(),
    }
    updated["updated_at"] = now
    return updated


class SettlementExecutor:
    def __init__(
        self,
        repository: XrpInvoiceRepository,
        reader: XrplEvidenceReader,
        gateway: FsaGateway,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.repository = repository
        self.reader = reader
        self.gateway = gateway
        self.clock = clock or (lambda: int(time.time()))

    def submit(self, invoice_id: str, transaction_hash: str) -> dict[str, object]:
        record = self._record(invoice_id)
        existing = _idempotent_result(record, transaction_hash)
        if existing is not None:
            return existing
        now = self.clock()
        with _execution_lock(self.repository, invoice_id, now):
            return self._submit_locked(invoice_id, transaction_hash, now)

    def progress(self, invoice_id: str) -> dict[str, object]:
        record = self._record(invoice_id)
        if record.get("state") != "flare_executing":
            return record
        now = self.clock()
        with _execution_lock(self.repository, invoice_id, now):
            current = self._record(invoice_id)
            if current.get("state") != "flare_executing":
                return current
            evidence = _stored_payment(current)
            outcome = self.gateway.progress(current)
            updated = _with_outcome(current, evidence, outcome, now)
            self._save(invoice_id, updated)
            return updated

    def _submit_locked(
        self, invoice_id: str, transaction_hash: str, now: int
    ) -> dict[str, object]:
        record = self._record(invoice_id)
        existing = _idempotent_result(record, transaction_hash)
        if existing is not None:
            return existing
        evidence = self.reader.read(transaction_hash)
        try:
            _validate_payment(record, transaction_hash, evidence, now)
        except ExecutorError:
            self._reject(record, evidence, now)
            raise
        claimed = self.repository.claim_transaction(transaction_hash, invoice_id)
        if claimed != invoice_id:
            raise ExecutorError("XRPL transaction replay belongs to another invoice")
        submitted = _with_payment(record, evidence, now)
        self._save(invoice_id, submitted)
        return self._execute(submitted, evidence, now)

    def _execute(
        self, record: dict[str, object], evidence: XrplPaymentEvidence, now: int
    ) -> dict[str, object]:
        outcome = self.gateway.execute(record, evidence)
        updated = _with_outcome(record, evidence, outcome, now)
        self._save(str(record["id"]), updated)
        return updated

    def _reject(
        self, record: dict[str, object], evidence: XrplPaymentEvidence, now: int
    ) -> None:
        updated = dict(record)
        updated.update(state="payment_rejected", xrpl_evidence=asdict(evidence), updated_at=now)
        updated["recovery"] = _recovery(
            evidence.validated, evidence.result == "tesSUCCESS", None
        )
        self._save(str(record["id"]), updated)

    def _record(self, invoice_id: str) -> dict[str, object]:
        record = self.repository.get(invoice_id)
        if record is None:
            raise ExecutorError("XRP invoice was not found")
        return record

    def _save(self, invoice_id: str, record: dict[str, object]) -> None:
        if not self.repository.replace(invoice_id, record):
            raise ExecutorError("invoice changed while executing")


def _validate_payment(
    record: dict[str, object],
    transaction_hash: str,
    evidence: XrplPaymentEvidence,
    now: int,
) -> None:
    _validate_xrpl_payment(record, transaction_hash, evidence, now)
    if evidence.transaction_hash.lower() != transaction_hash.lower():
        raise ExecutorError("XRPL transaction hash does not match")
    if evidence.fdc_round_id is None or evidence.fdc_proof_hash is None:
        raise ExecutorError("finalized FDC evidence is missing")


def _validate_xrpl_payment(
    record: dict[str, object],
    transaction_hash: str,
    evidence: XrplPaymentEvidence,
    now: int,
) -> None:
    unsigned = _mapping(record, "unsigned_payment")
    quote = _mapping(record, "quote")
    if evidence.transaction_hash.lower() != transaction_hash.lower():
        raise ExecutorError("XRPL transaction hash does not match")
    if evidence.validated is not True or evidence.result != "tesSUCCESS":
        raise ExecutorError("XRPL payment is not finalized successfully")
    if evidence.flags & 0x00020000 or evidence.destination_tag is not None:
        raise ExecutorError("XRPL payment uses an unsafe flag or destination tag")
    _validate_payment_fields(unsigned, evidence)
    _require_timely_payment(quote, evidence, now)


def _validate_payment_fields(
    unsigned: dict[str, object], evidence: XrplPaymentEvidence
) -> None:
    expected_amount = _positive_decimal(unsigned.get("Amount"), "payment amount")
    expected_memo = _unsigned_memo(unsigned)
    checks = (
        unsigned.get("TransactionType") == "Payment",
        evidence.source_account == unsigned.get("Account"),
        evidence.destination == unsigned.get("Destination"),
        evidence.amount_drops == expected_amount,
        evidence.delivered_amount_drops == expected_amount,
        evidence.memo_data_hex == expected_memo,
    )
    if not all(checks):
        raise ExecutorError("XRPL payment fields do not match the prepared instruction")


def _with_payment(
    record: dict[str, object], evidence: XrplPaymentEvidence, now: int
) -> dict[str, object]:
    updated = dict(record)
    updated.update(state="xrpl_submitted", xrpl_evidence=asdict(evidence), updated_at=now)
    return updated


def _with_outcome(
    record: dict[str, object],
    evidence: XrplPaymentEvidence,
    outcome: FsaExecutionResult,
    now: int,
) -> dict[str, object]:
    updated = dict(record)
    updated.update(flare_evidence=asdict(outcome), updated_at=now)
    if outcome.status == "succeeded":
        from .receipts import build_receipt

        updated.update(state="paid", receipt=build_receipt(updated, evidence, outcome, now))
        updated["recovery"] = _recovery(True, True, "succeeded")
    elif outcome.status in {"pending", "reverted"}:
        if outcome.status == "pending":
            updated["state"] = "flare_executing"
            updated["recovery"] = _recovery(True, True, "pending")
        else:
            updated["state"] = "recovery_required"
            updated["recovery"] = _recovery(True, True, "reverted")
    else:
        raise ExecutorError("FSA execution status is unsupported")
    return updated


def _recovery(
    validated: bool,
    success: bool,
    flare_status: Literal["pending", "delayed", "reverted", "succeeded"] | None,
) -> dict[str, str]:
    classification = classify_recovery(
        RecoveryObservation(
            payment_attempted=True,
            xrpl_validated=validated,
            xrpl_success=success,
            destination_is_core_vault=True if validated and success else None,
            meets_minimum_fee=True if validated and success else None,
            flare_execution_status=flare_status,
        )
    )
    return {"classification": classification.value, "guidance": classification.guidance}


def _idempotent_result(
    record: dict[str, object], transaction_hash: str
) -> dict[str, object] | None:
    evidence = record.get("xrpl_evidence")
    if not isinstance(evidence, dict):
        return None
    stored = evidence.get("transaction_hash")
    if isinstance(stored, str) and stored.lower() == transaction_hash.lower():
        if record.get("state") in {"paid", "flare_executing", "recovery_required"}:
            return record
    return None


def _unsigned_memo(unsigned: dict[str, object]) -> str:
    memos = unsigned.get("Memos")
    if not isinstance(memos, list) or len(memos) != 1 or not isinstance(memos[0], dict):
        raise ExecutorError("prepared FSA memo is missing")
    container = memos[0].get("Memo")
    if not isinstance(container, dict):
        raise ExecutorError("prepared FSA memo is missing")
    memo = container.get("MemoData")
    if not isinstance(memo, str):
        raise ExecutorError("prepared FSA memo is malformed")
    return memo


def _positive_decimal(value: object, label: str) -> int:
    if not isinstance(value, str) or not value.isdecimal() or int(value) <= 0:
        raise ExecutorError(f"{label} is malformed")
    return int(value)


def _mapping(record: dict[str, object], key: str) -> dict[str, object]:
    value = record.get(key)
    if not isinstance(value, dict):
        raise ExecutorError(f"{key} is not prepared")
    return value


def _settlement_calldata(
    invoice_hash: bytes,
    beneficiary: bytes,
    exact_usd0: int,
    maximum_fxrp: int,
    deadline: int,
    fxrp_input: int,
    settlement_contract: bytes,
) -> bytes:
    settlement_id = _settlement_id(
        invoice_hash, beneficiary, exact_usd0, maximum_fxrp, deadline,
        settlement_contract,
    )
    arguments = b"".join(
        (
            settlement_id,
            invoice_hash,
            _address_word(beneficiary),
            _word(exact_usd0),
            _word(maximum_fxrp),
            _word(deadline),
            _word(fxrp_input),
        )
    )
    signature = b"settle(bytes32,bytes32,address,uint256,uint256,uint256,uint256)"
    return keccak256(signature)[:4] + arguments


def _approve_calldata(spender: bytes, amount: int) -> bytes:
    return keccak256(b"approve(address,uint256)")[:4] + _address_word(spender) + _word(amount)


def _settlement_id(
    invoice_hash: bytes,
    beneficiary: bytes,
    exact_usd0: int,
    maximum_fxrp: int,
    deadline: int,
    settlement_contract: bytes,
) -> bytes:
    terms = (
        invoice_hash,
        _address_word(beneficiary),
        _word(exact_usd0),
        _word(maximum_fxrp),
        _word(deadline),
        _word(COSTON2_CHAIN_ID),
        _address_word(settlement_contract),
    )
    return keccak256(b"".join(terms))


def _invoice_amount_uba(record: dict[str, object]) -> int:
    invoice = _mapping(record, "invoice")
    try:
        units = Decimal(str(invoice["total"])) * Decimal(1_000_000)
    except (InvalidOperation, KeyError) as error:
        raise ExecutorError("invoice total is malformed") from error
    if units <= 0 or units != units.to_integral_value():
        raise ExecutorError("invoice total cannot be represented exactly")
    return int(units)


def _bytes32(value: str) -> bytes:
    raw = _hex_bytes(value, "bytes32")
    if len(raw) != 32:
        raise ExecutorError("bytes32 value is malformed")
    return raw


def _address_bytes(value: str) -> bytes:
    raw = _hex_bytes(value, "EVM address")
    if len(raw) != 20 or raw == b"\0" * 20:
        raise ExecutorError("EVM address is malformed")
    return raw


def _hex_bytes(value: str, label: str) -> bytes:
    if not value.startswith("0x") or len(value) % 2:
        raise ExecutorError(f"{label} is malformed")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as error:
        raise ExecutorError(f"{label} is malformed") from error


def _address_word(value: bytes) -> bytes:
    return b"\0" * 12 + value


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


@contextmanager
def _execution_lock(
    repository: XrpInvoiceRepository, invoice_id: str, now: int
) -> Iterator[None]:
    owner = secrets.token_hex(16)
    if not repository.acquire_execution_lock(invoice_id, owner, now, ttl=300):
        raise ExecutorError("invoice execution is already locked")
    try:
        yield
    finally:
        repository.release_execution_lock(invoice_id, owner)


def _require_current_quote(quote: dict[str, object], now: int) -> None:
    expiry = quote.get("expires_at")
    if isinstance(expiry, bool) or not isinstance(expiry, int) or now > expiry:
        raise ExecutorError("invoice quote expired before payment validation")


def _require_timely_payment(
    quote: dict[str, object], evidence: XrplPaymentEvidence, now: int
) -> None:
    if evidence.ledger_timestamp > _positive_int(quote, "expires_at"):
        raise ExecutorError("XRPL payment landed after the invoice quote expired")
    deadline = quote.get("settlement_deadline_at")
    if deadline is not None and now > _positive_int(quote, "settlement_deadline_at"):
        raise ExecutorError("invoice settlement deadline expired before payment validation")


def _stored_payment(record: dict[str, object]) -> XrplPaymentEvidence:
    value = _mapping(record, "xrpl_evidence")
    try:
        return XrplPaymentEvidence(
            transaction_hash=_nonempty_text(value, "transaction_hash"),
            validated=_boolean(value, "validated"),
            result=_nonempty_text(value, "result"),
            source_account=_nonempty_text(value, "source_account"),
            destination=_nonempty_text(value, "destination"),
            amount_drops=_positive_int(value, "amount_drops"),
            delivered_amount_drops=_positive_int(value, "delivered_amount_drops"),
            memo_data_hex=_nonempty_text(value, "memo_data_hex"),
            destination_tag=_optional_int(value, "destination_tag"),
            flags=_nonnegative_int(value, "flags"),
            ledger_index=_positive_int(value, "ledger_index"),
            ledger_timestamp=_positive_int(value, "ledger_timestamp"),
            fdc_round_id=_optional_int(value, "fdc_round_id"),
            fdc_proof_hash=_optional_text(value, "fdc_proof_hash"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ExecutorError("stored XRPL evidence is malformed") from error


def _nonempty_text(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ExecutorError(f"{key} is malformed")
    return item


def _positive_int(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
        raise ExecutorError(f"{key} is malformed")
    return item


def _nonnegative_int(value: dict[str, object], key: str) -> int:
    item = value.get(key, 0)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise ExecutorError(f"{key} is malformed")
    return item


def _optional_int(value: dict[str, object], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise ExecutorError(f"{key} is malformed")
    return item


def _optional_text(value: dict[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ExecutorError(f"{key} is malformed")
    return item


def _boolean(value: dict[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise ExecutorError(f"{key} is malformed")
    return item
