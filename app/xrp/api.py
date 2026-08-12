"""Resource-oriented Phase 1 API for persistent XRP invoices and quotes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from decimal import Decimal
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse, JSONResponse

from app.invoice import create_invoice
from app.models import ClientInfo, IssuerInfo

from .executor import ExecutorError, SettlementExecutor, SettlementPaymentPreparer
from .models import (
    OperatorTransactionRequest,
    XrpInvoiceCreateRequest,
    XrpSigningRequest,
    XrpSubmissionRequest,
)
from .operator import OperatorCoordinator, OperatorError
from .quotes import Coston2QuoteProvider, QuoteError, QuoteProvider, build_quote
from .repository import XrpInvoiceRepository
from .xaman import SigningResult, XamanError, XamanGateway

logger = logging.getLogger(__name__)
router = APIRouter()
PAY_PAGE = Path(__file__).resolve().parents[2] / "dashboard" / "pay.html"


@router.post("/api/v1/xrp/invoices")
def create_resource(
    request: Request,
    payload: XrpInvoiceCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    repository = XrpInvoiceRepository()
    request_hash = _request_hash(payload)
    try:
        invoice_id = _new_invoice_id()
        if idempotency_key is not None:
            claim = repository.claim_idempotency(
                _idempotency_hash(idempotency_key), invoice_id, request_hash
            )
            if claim["request_hash"] != request_hash:
                return _error(409, "idempotency_conflict", "Idempotency-Key was reused")
            invoice_id = claim["invoice_id"]
    except ValueError:
        return _error(400, "invalid_idempotency_key", "Idempotency-Key is malformed")
    existing = repository.get(invoice_id)
    if existing is not None:
        return _idempotent_response(existing, request_hash)
    try:
        record = _create_record(payload, invoice_id, request_hash, _clock(request))
    except (ArithmeticError, ValueError):
        return _error(422, "invoice_parse_error", "Invoice description could not be parsed")
    if repository.create(invoice_id, record):
        return JSONResponse(status_code=201, content=record)
    winner = repository.get(invoice_id)
    return _idempotent_response(winner, request_hash)


@router.get("/api/v1/xrp/invoices/{invoice_id}")
def get_resource(invoice_id: str, request: Request) -> JSONResponse:
    record = XrpInvoiceRepository().get(invoice_id)
    executor = _executor(request)
    if record is not None and record.get("state") == "flare_executing" and executor is not None:
        try:
            record = executor.progress(invoice_id)
        except ExecutorError:
            logger.warning("XRP settlement progress unavailable", exc_info=True)
    return _record_response(record)


@router.get("/pay/{invoice_id}")
def get_shared_resource(invoice_id: str) -> FileResponse:
    return FileResponse(PAY_PAGE, media_type="text/html")


@router.post("/api/v1/xrp/invoices/{invoice_id}/quote")
def quote_resource(invoice_id: str, request: Request) -> JSONResponse:
    repository = XrpInvoiceRepository()
    record = repository.get(invoice_id)
    if record is None:
        return _not_found()
    now = _clock(request)
    if _quote_is_current(record.get("quote"), now):
        return JSONResponse(content=record)
    try:
        parameters = _quote_provider(request).read()
        quote = build_quote(
            invoice_id=invoice_id,
            invoice_amount_usd=Decimal(_invoice_total(record)),
            beneficiary=str(record["beneficiary"]),
            parameters=parameters,
            now=now,
        )
    except (ArithmeticError, QuoteError):
        logger.warning("XRP quote unavailable", exc_info=True)
        return _error(503, "quote_unavailable", "Live Coston2 quote is unavailable")
    updated = dict(record)
    for key in ("unsigned_payment", "fsa_evidence", "signing_request"):
        updated.pop(key, None)
    updated.update(
        state="quoted",
        quote=quote.to_dict(),
        canonical_hash=quote.quote_hash,
        updated_at=now,
    )
    if not repository.replace(invoice_id, updated):
        return _error(409, "invoice_conflict", "Invoice changed while quoting")
    return JSONResponse(content=updated)


@router.post("/api/v1/xrp/invoices/{invoice_id}/signing-request")
def signing_resource(
    invoice_id: str, payload: XrpSigningRequest, request: Request
) -> JSONResponse:
    repository = XrpInvoiceRepository()
    record = repository.get(invoice_id)
    if record is None:
        return _not_found()
    try:
        record, unsigned = _prepared_unsigned(
            record, invoice_id, payload.source_account, request
        )
    except ExecutorError as error:
        return _error(409, "signing_not_ready", str(error))
    if unsigned.get("Account") != payload.source_account:
        return _error(409, "source_account_mismatch", "Source account does not match")
    if isinstance(record.get("signing_request"), dict):
        return JSONResponse(content=record)
    try:
        result = _xaman_gateway(request).create(unsigned)
    except XamanError:
        return _error(502, "signing_unavailable", "Xaman signing request failed")
    updated = _with_signing_result(record, result, _clock(request))
    if not repository.replace(invoice_id, updated):
        return _error(409, "invoice_conflict", "Invoice changed while signing")
    return JSONResponse(content=updated)


@router.post("/api/v1/xrp/invoices/{invoice_id}/submit")
def submit_resource(
    invoice_id: str, payload: XrpSubmissionRequest, request: Request
) -> JSONResponse:
    if XrpInvoiceRepository().get(invoice_id) is None:
        return _not_found()
    operator = _operator_coordinator(request)
    if operator is not None:
        try:
            return JSONResponse(
                content=operator.submit(invoice_id, payload.xrpl_transaction_hash)
            )
        except OperatorError as error:
            return _error(409, "settlement_rejected", str(error))
    executor = _executor(request)
    if executor is None:
        return _error(409, "executor_not_ready", "Settlement executor is not configured")
    try:
        return JSONResponse(content=executor.submit(invoice_id, payload.xrpl_transaction_hash))
    except ExecutorError as error:
        return _error(409, "settlement_rejected", str(error))


@router.get("/api/v1/xrp/invoices/{invoice_id}/operator-job")
def get_operator_job(
    invoice_id: str,
    request: Request,
    operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
) -> JSONResponse:
    coordinator, denied = _operator_access(request, operator_token)
    if denied is not None:
        return denied
    if coordinator is None:
        return _operator_unavailable()
    return _operator_response(lambda: coordinator.get_job(invoice_id))


@router.post("/api/v1/xrp/invoices/{invoice_id}/operator-job")
def prepare_operator_job(
    invoice_id: str,
    request: Request,
    operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
) -> JSONResponse:
    coordinator, denied = _operator_access(request, operator_token)
    if denied is not None:
        return denied
    if coordinator is None:
        return _operator_unavailable()
    return _operator_response(lambda: coordinator.prepare(invoice_id))


@router.patch("/api/v1/xrp/invoices/{invoice_id}/operator-job")
def record_operator_transaction(
    invoice_id: str,
    payload: OperatorTransactionRequest,
    request: Request,
    operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
) -> JSONResponse:
    coordinator, denied = _operator_access(request, operator_token)
    if denied is not None:
        return denied
    if coordinator is None:
        return _operator_unavailable()
    return _operator_response(
        lambda: coordinator.record(invoice_id, payload.transaction_hash)
    )


def _operator_response(action: Callable[[], dict[str, object]]) -> JSONResponse:
    try:
        return JSONResponse(content=action())
    except OperatorError as error:
        return _error(409, "operator_job_rejected", str(error))


@router.get("/api/v1/xrp/invoices/{invoice_id}/receipt")
def receipt_resource(invoice_id: str) -> JSONResponse:
    record = XrpInvoiceRepository().get(invoice_id)
    if record is None:
        return _not_found()
    receipt = record.get("receipt")
    if not isinstance(receipt, dict):
        return _error(409, "receipt_not_ready", "Settlement receipt is not available")
    return JSONResponse(content=receipt)


def _create_record(
    payload: XrpInvoiceCreateRequest, invoice_id: str, request_hash: str, now: int
) -> dict[str, object]:
    invoice = create_invoice(
        description=payload.description,
        tax_rate=Decimal(payload.tax_rate),
        issuer=_issuer(payload),
        client=_client(payload),
        currency=payload.currency,
    )
    return {
        "schema_version": 1,
        "id": invoice_id,
        "share_url": f"/pay/{invoice_id}",
        "network": "coston2",
        "state": "open",
        "beneficiary": payload.beneficiary,
        "request_hash": request_hash,
        "canonical_hash": None,
        "invoice": invoice.model_dump(mode="json"),
        "quote": None,
        "unsigned_payment": None,
        "signing_request": None,
        "receipt": None,
        "created_at": now,
        "updated_at": now,
    }


def _issuer(payload: XrpInvoiceCreateRequest) -> IssuerInfo:
    party = payload.issuer
    return IssuerInfo(
        name=(party.name or "Your Business") if party else "Your Business",
        email=(party.email or "") if party else "",
        address=(party.address or "") if party else "",
    )


def _client(payload: XrpInvoiceCreateRequest) -> ClientInfo:
    party = payload.client
    return ClientInfo(
        name=(party.name or "") if party else "",
        email=(party.email or "") if party else "",
        address=(party.address or "") if party else "",
    )


def _request_hash(payload: XrpInvoiceCreateRequest) -> str:
    value = payload.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _new_invoice_id() -> str:
    return "xrp_" + secrets.token_hex(12)


def _idempotency_hash(idempotency_key: str) -> str:
    key = idempotency_key.strip()
    if not 1 <= len(key) <= 128 or any(ord(character) < 33 or ord(character) > 126 for character in key):
        raise ValueError("malformed idempotency key")
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def _idempotent_response(
    record: dict[str, object] | None, request_hash: str
) -> JSONResponse:
    if record is not None and record.get("request_hash") == request_hash:
        return JSONResponse(content=record)
    return _error(409, "idempotency_conflict", "Idempotency-Key was reused")


def _record_response(record: dict[str, object] | None) -> JSONResponse:
    return _not_found() if record is None else JSONResponse(content=record)


def _not_found() -> JSONResponse:
    return _error(404, "invoice_not_found", "XRP invoice was not found")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _clock(request: Request) -> int:
    clock = getattr(request.app.state, "xrp_clock", time.time)
    return int(clock())


def _quote_provider(request: Request) -> QuoteProvider:
    provider = getattr(request.app.state, "xrp_quote_provider", None)
    return provider if provider is not None else Coston2QuoteProvider()


def _xaman_gateway(request: Request) -> XamanGateway:
    gateway = getattr(request.app.state, "xrp_xaman_gateway", None)
    if gateway is not None:
        return gateway
    return XamanGateway(os.getenv("XAMAN_API_KEY", ""), os.getenv("XAMAN_API_SECRET", ""))


def _executor(request: Request) -> SettlementExecutor | None:
    value = getattr(request.app.state, "xrp_executor", None)
    return value if isinstance(value, SettlementExecutor) else None


def _payment_preparer(request: Request) -> SettlementPaymentPreparer | None:
    value = getattr(request.app.state, "xrp_payment_preparer", None)
    return value if isinstance(value, SettlementPaymentPreparer) else None


def _operator_coordinator(request: Request) -> OperatorCoordinator | None:
    value = getattr(request.app.state, "xrp_operator_coordinator", None)
    return value if isinstance(value, OperatorCoordinator) else None


def _operator_access(
    request: Request, supplied: str | None
) -> tuple[OperatorCoordinator | None, JSONResponse | None]:
    denied = _authorize_operator(request, supplied)
    if denied is not None:
        return None, denied
    coordinator = _operator_coordinator(request)
    if coordinator is None:
        return None, _operator_unavailable()
    return coordinator, None


def _authorize_operator(
    request: Request, supplied: str | None
) -> JSONResponse | None:
    configured = getattr(request.app.state, "xrp_operator_token", None)
    if configured is None:
        configured = os.getenv("XRP_OPERATOR_TOKEN", "")
    if not isinstance(configured, str) or not configured:
        return _operator_unavailable()
    if supplied is None or not secrets.compare_digest(supplied, configured):
        return _error(403, "operator_forbidden", "Operator authorization failed")
    return None


def _operator_unavailable() -> JSONResponse:
    return _error(503, "operator_unavailable", "Browser operator is not configured")


def _prepared_unsigned(
    record: dict[str, object],
    invoice_id: str,
    source_account: str,
    request: Request,
) -> tuple[dict[str, object], dict[str, object]]:
    unsigned = record.get("unsigned_payment")
    if not isinstance(unsigned, dict):
        preparer = _payment_preparer(request)
        if preparer is None:
            raise ExecutorError("Settlement UserOp is not prepared")
        record = preparer.prepare(invoice_id, source_account)
        unsigned = record.get("unsigned_payment")
    if not isinstance(unsigned, dict):
        raise ExecutorError("Settlement UserOp is not prepared")
    return record, unsigned


def _quote_is_current(value: object, now: int) -> bool:
    return isinstance(value, dict) and isinstance(value.get("expires_at"), int) and value["expires_at"] > now


def _invoice_total(record: dict[str, object]) -> str:
    invoice = record.get("invoice")
    if not isinstance(invoice, dict) or not isinstance(invoice.get("total"), str):
        raise QuoteError("stored invoice total is malformed")
    return invoice["total"]


def _with_signing_result(
    record: dict[str, object], result: SigningResult, now: int
) -> dict[str, object]:
    updated = dict(record)
    updated.update(
        state="awaiting_signature",
        signing_request=result.to_dict(),
        updated_at=now,
    )
    return updated
