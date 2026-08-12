"""Public, keyless boundary for browser-operated Coston2 settlement jobs."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from typing import Callable, Protocol, runtime_checkable

from .executor import (
    ExecutorError,
    FsaExecutionResult,
    XrplEvidenceReader,
    XrplPaymentEvidence,
    _execution_lock,
    _stored_payment,
    _validate_xrpl_payment,
    _with_outcome,
    _with_payment,
)
from .repository import XrpInvoiceRepository


class OperatorError(ValueError):
    """Raised when an operator job cannot safely advance."""


@dataclass(frozen=True)
class OperatorPreparation:
    sign_request: dict[str, str]
    context: dict[str, object]
    fdc_proof_hash: str | None = None


@dataclass(frozen=True)
class OperatorProgress:
    context: dict[str, object]
    fdc_round_id: int | None = None


class OperatorBackend(Protocol):
    def start(
        self, invoice: dict[str, object], evidence: XrplPaymentEvidence
    ) -> dict[str, object]: ...

    def prepare_fdc(self, context: dict[str, object]) -> OperatorPreparation: ...

    def record_fdc(
        self, context: dict[str, object], transaction_hash: str
    ) -> OperatorProgress: ...

    def prepare_execute(self, context: dict[str, object]) -> OperatorPreparation: ...

    def finalize(
        self, context: dict[str, object], transaction_hash: str
    ) -> FsaExecutionResult: ...


@runtime_checkable
class OperatorCoordinator(Protocol):
    """Advance settlement using public intents and mined transaction hashes only."""

    def submit(
        self, invoice_id: str, transaction_hash: str
    ) -> dict[str, object]: ...

    def get_job(self, invoice_id: str) -> dict[str, object]: ...

    def prepare(self, invoice_id: str) -> dict[str, object]: ...

    def record(
        self, invoice_id: str, transaction_hash: str
    ) -> dict[str, object]: ...


class BrowserOperatorCoordinator:
    """Persist a browser queue while leaving all signing to an EIP-1193 wallet."""

    def __init__(
        self,
        repository: XrpInvoiceRepository,
        reader: XrplEvidenceReader,
        backend: OperatorBackend,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.repository = repository
        self.reader = reader
        self.backend = backend
        self.clock = clock or (lambda: int(time.time()))

    def submit(self, invoice_id: str, transaction_hash: str) -> dict[str, object]:
        record = self._record(invoice_id)
        existing = _existing_submission(record, transaction_hash)
        if existing is not None:
            return record
        now = self.clock()
        try:
            with _execution_lock(self.repository, invoice_id, now):
                return self._submit_locked(invoice_id, transaction_hash, now)
        except ExecutorError as error:
            raise OperatorError(str(error)) from error

    def get_job(self, invoice_id: str) -> dict[str, object]:
        return _job(self._record(invoice_id))

    def prepare(self, invoice_id: str) -> dict[str, object]:
        now = self.clock()
        try:
            with _execution_lock(self.repository, invoice_id, now):
                return self._prepare_locked(invoice_id, now)
        except ExecutorError as error:
            raise OperatorError(str(error)) from error

    def record(self, invoice_id: str, transaction_hash: str) -> dict[str, object]:
        normalized = _evm_hash(transaction_hash)
        now = self.clock()
        try:
            with _execution_lock(self.repository, invoice_id, now):
                return self._record_locked(invoice_id, normalized, now)
        except ExecutorError as error:
            raise OperatorError(str(error)) from error

    def _submit_locked(
        self, invoice_id: str, transaction_hash: str, now: int
    ) -> dict[str, object]:
        record = self._record(invoice_id)
        if _existing_submission(record, transaction_hash) is not None:
            return record
        evidence = self.reader.read(transaction_hash)
        _validate_xrpl_payment(record, transaction_hash, evidence, now)
        context = self.backend.start(record, evidence)
        _validate_public_state(context)
        claimed = self.repository.claim_transaction(transaction_hash, invoice_id)
        if claimed != invoice_id:
            raise OperatorError("XRPL transaction replay belongs to another invoice")
        updated = _with_payment(record, evidence, now)
        updated.update(
            state="flare_executing",
            operator_job=_new_job(invoice_id, transaction_hash, context, now),
        )
        self._save(invoice_id, updated)
        return updated

    def _prepare_locked(self, invoice_id: str, now: int) -> dict[str, object]:
        record = self._record(invoice_id)
        job = _job(record)
        stage = job.get("stage")
        if stage in {"awaiting_fdc_transaction", "awaiting_execute_transaction", "complete"}:
            return job
        if stage == "prepare_fdc":
            preparation = self.backend.prepare_fdc(_context(job))
            updated_job = _with_preparation(job, preparation, "fdc-request", now)
        elif stage == "prepare_execute":
            preparation = self.backend.prepare_execute(_context(job))
            updated_job = _with_preparation(
                job, preparation, "execute-direct-mint", now
            )
        else:
            raise OperatorError("operator job cannot prepare from its current stage")
        self._save_job(record, updated_job)
        return updated_job

    def _record_locked(
        self, invoice_id: str, transaction_hash: str, now: int
    ) -> dict[str, object]:
        record = self._record(invoice_id)
        job = _job(record)
        repeated = _repeated_transaction(job, transaction_hash)
        if repeated:
            return job
        stage = job.get("stage")
        if stage == "awaiting_fdc_transaction":
            progress = self.backend.record_fdc(_context(job), transaction_hash)
            updated_job = _after_fdc(job, progress, transaction_hash, now)
            self._save_job(record, updated_job)
            return updated_job
        if stage == "awaiting_execute_transaction":
            return self._finalize(record, job, transaction_hash, now)
        raise OperatorError("a different transaction cannot replace recorded state")

    def _finalize(
        self,
        record: dict[str, object],
        job: dict[str, object],
        transaction_hash: str,
        now: int,
    ) -> dict[str, object]:
        outcome = self.backend.finalize(_context(job), transaction_hash)
        if outcome.status != "succeeded":
            raise OperatorError("browser execution did not complete successfully")
        context = _context(job)
        evidence = replace(
            _stored_payment(record),
            fdc_round_id=_positive_job_int(job, "fdc_round_id"),
            fdc_proof_hash=_job_hash(job, "fdc_proof_hash"),
        )
        base = dict(record)
        base["xrpl_evidence"] = asdict(evidence)
        updated = _with_outcome(base, evidence, outcome, now)
        completed = dict(job)
        completed.update(
            stage="complete",
            execute_transaction_hash=transaction_hash,
            updated_at=now,
        )
        completed.pop("sign_request", None)
        updated["operator_job"] = completed
        self._save(str(record["id"]), updated)
        return completed

    def _record(self, invoice_id: str) -> dict[str, object]:
        record = self.repository.get(invoice_id)
        if record is None:
            raise OperatorError("XRP invoice was not found")
        return record

    def _save_job(
        self, record: dict[str, object], job: dict[str, object]
    ) -> None:
        updated = dict(record)
        updated["operator_job"] = job
        updated["updated_at"] = job["updated_at"]
        self._save(str(record["id"]), updated)

    def _save(self, invoice_id: str, record: dict[str, object]) -> None:
        if not self.repository.replace(invoice_id, record):
            raise OperatorError("invoice changed while advancing operator job")


def _new_job(
    invoice_id: str,
    transaction_hash: str,
    context: dict[str, object],
    now: int,
) -> dict[str, object]:
    return {
        "invoice_id": invoice_id,
        "stage": "prepare_fdc",
        "xrpl_transaction_hash": transaction_hash,
        "context": context,
        "created_at": now,
        "updated_at": now,
    }


def _with_preparation(
    job: dict[str, object],
    preparation: OperatorPreparation,
    purpose: str,
    now: int,
) -> dict[str, object]:
    _validate_public_state(preparation.context)
    _validate_sign_request(preparation.sign_request, purpose)
    updated = dict(job)
    updated.update(
        stage=f"awaiting_{'fdc' if purpose == 'fdc-request' else 'execute'}_transaction",
        context=preparation.context,
        sign_request=preparation.sign_request,
        updated_at=now,
    )
    if preparation.fdc_proof_hash is not None:
        updated["fdc_proof_hash"] = _evm_hash(preparation.fdc_proof_hash)
    return updated


def _after_fdc(
    job: dict[str, object],
    progress: OperatorProgress,
    transaction_hash: str,
    now: int,
) -> dict[str, object]:
    _validate_public_state(progress.context)
    if progress.fdc_round_id is None or progress.fdc_round_id <= 0:
        raise OperatorError("operator FDC round is malformed")
    updated = dict(job)
    updated.update(
        stage="prepare_execute",
        context=progress.context,
        fdc_transaction_hash=transaction_hash,
        updated_at=now,
        fdc_round_id=progress.fdc_round_id,
    )
    updated.pop("sign_request", None)
    return updated


def _existing_submission(
    record: dict[str, object], transaction_hash: str
) -> dict[str, object] | None:
    value = record.get("operator_job")
    if not isinstance(value, dict):
        return None
    if str(value.get("xrpl_transaction_hash", "")).lower() != transaction_hash.lower():
        raise OperatorError("a different XRPL transaction is already queued")
    return value


def _job(record: dict[str, object]) -> dict[str, object]:
    value = record.get("operator_job")
    if not isinstance(value, dict):
        raise OperatorError("operator job is not ready")
    return value


def _context(job: dict[str, object]) -> dict[str, object]:
    value = job.get("context")
    if not isinstance(value, dict):
        raise OperatorError("operator job context is malformed")
    return value


def _repeated_transaction(job: dict[str, object], transaction_hash: str) -> bool:
    recorded = (job.get("fdc_transaction_hash"), job.get("execute_transaction_hash"))
    return any(
        isinstance(value, str) and value.lower() == transaction_hash.lower()
        for value in recorded
    )


def _evm_hash(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise OperatorError("Coston2 transaction hash is malformed")
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as error:
        raise OperatorError("Coston2 transaction hash is malformed") from error
    if len(raw) != 32:
        raise OperatorError("Coston2 transaction hash is malformed")
    return "0x" + raw.hex()


def _validate_sign_request(value: dict[str, str], purpose: str) -> None:
    fields = {
        "version",
        "purpose",
        "chain_id",
        "signer",
        "to",
        "value",
        "data",
        "calldata_hash",
    }
    if set(value) != fields or any(not isinstance(item, str) for item in value.values()):
        raise OperatorError("browser sign request is malformed")
    if value["version"] != "1" or value["chain_id"] != "0x72" or value["purpose"] != purpose:
        raise OperatorError("browser sign request is not bound to the requested stage")
    _validate_public_state(value)


def _validate_public_state(value: object, key: str = "context") -> None:
    secret_markers = ("secret", "private", "seed", "password", "api_key")
    if any(marker in key.lower() for marker in secret_markers):
        raise OperatorError("operator state contains a secret-like field")
    if isinstance(value, dict):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise OperatorError("operator state keys must be text")
            _validate_public_state(child, child_key)
    elif isinstance(value, list):
        for child in value:
            _validate_public_state(child, key)
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise OperatorError("operator state is not JSON-safe")


def _positive_job_int(job: dict[str, object], key: str) -> int:
    value = job.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperatorError(f"operator {key} is malformed")
    return value


def _job_hash(job: dict[str, object], key: str) -> str:
    value = job.get(key)
    if not isinstance(value, str):
        raise OperatorError(f"operator {key} is malformed")
    return _evm_hash(value)
