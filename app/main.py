from __future__ import annotations

import base64
import json
import logging
import os
from decimal import Decimal
from typing import Optional

from fastapi import Body, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app import store
from app.invoice import create_invoice
from app.models import ClientInfo, InvoiceRequest, IssuerInfo
from app.okx_payments import install_payment_middleware
from app.pdf_engine import generate_invoice_pdf
from app.tax import get_tax_rate
from app.x402 import verify_payment

logger = logging.getLogger(__name__)
app = FastAPI(title="InvoiceCraft")

# Reference recorded on invoices settled through the OKX Payment SDK, where the
# on-chain settlement is performed by the facilitator rather than by the caller.
SDK_PAYMENT_REFERENCE = "OKX x402 (Onchain OS Payment SDK)"

PRICE_USDT = Decimal(os.getenv("INVOICE_PRICE_USDT", "0.50"))


def _x402_requirements(resource_url: str, asp_wallet: str) -> dict:
    """Build the standard x402 (v2) payment-requirements object."""
    decimals = int(os.getenv("USDT_DECIMALS", "6"))
    amount_base = str(int((PRICE_USDT * (10 ** decimals)).to_integral_value()))
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {
            "url": resource_url,
            "description": "InvoiceCraft invoice generation",
            "mimeType": "",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": os.getenv("PAYMENT_CHAIN", "eip155:196"),
                "amount": amount_base,
                "asset": os.getenv("USDT_CONTRACT", ""),
                "payTo": asp_wallet,
                "maxTimeoutSeconds": 300,
                "extra": {
                    "name": os.getenv("USDT_NAME", "USD₮0"),
                    "version": os.getenv("USDT_VERSION", "1"),
                },
            }
        ],
    }


def _field(req: Optional[InvoiceRequest], party: str, name: str) -> str:
    """Read a party field from the request, or "" when it wasn't supplied."""
    source = getattr(req, party, None) if req else None
    return (getattr(source, name, None) or "").strip() if source else ""


def _issuer_from(req: Optional[InvoiceRequest]) -> IssuerInfo:
    """The billing business: caller-supplied, falling back to server defaults."""
    return IssuerInfo(
        name=_field(req, "issuer", "name")
        or os.getenv("INVOICE_ISSUER_NAME", "Your Business"),
        address=_field(req, "issuer", "address"),
        email=_field(req, "issuer", "email") or os.getenv("INVOICE_ISSUER_EMAIL", ""),
    )


def _client_from(req: Optional[InvoiceRequest]) -> ClientInfo:
    return ClientInfo(
        name=_field(req, "client", "name"),
        email=_field(req, "client", "email"),
        address=_field(req, "client", "address"),
    )


def _currency_from(req: Optional[InvoiceRequest]) -> str:
    currency = (req.currency or "").strip().upper() if req else ""
    return currency or os.getenv("INVOICE_CURRENCY", "USD")


def _requested_tax_rate(req: Optional[InvoiceRequest]) -> Decimal:
    """Caller-supplied tax rate, else the server default. Raises if malformed."""
    raw = (req.tax_rate or "").strip() if req else ""
    if not raw:
        return get_tax_rate()
    rate = Decimal(raw)
    if not (0 <= rate <= 1):
        raise ValueError("tax_rate out of range")
    return rate


def _payment_required_response(resource_url: str, asp_wallet: str) -> JSONResponse:
    """A 402 that advertises x402 requirements in both the header and body."""
    body = _x402_requirements(resource_url, asp_wallet)
    header = base64.b64encode(
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return JSONResponse(status_code=402, headers={"payment-required": header}, content=body)


# Payment gate. The official OKX SDK owns the 402/PAYMENT-SIG handshake when
# credentials are configured; `_payment_required_response` below is only the
# fallback for local dev and tests. Installed before CORS so that CORS stays the
# outermost middleware and 402 responses still carry its headers.
OKX_PAYMENTS_ACTIVE = install_payment_middleware(
    app,
    fallback=lambda request: _payment_required_response(
        str(request.url), os.getenv("ASP_WALLET", "")
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # x402 clients read the payment requirements and settlement proof from
    # response headers, which cross-origin callers cannot see unless exposed.
    expose_headers=["*"],
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "InvoiceCraft",
        "payment_mode": os.getenv("PAYMENT_VERIFY_MODE", "mock").lower(),
        "payment_sdk": "okxweb3-app-x402" if OKX_PAYMENTS_ACTIVE else "fallback",
        # "upstash" survives restarts; "sqlite" is ephemeral on a free dyno.
        "persistence": store.backend(),
        "ai_enabled": bool(os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY")),
    }


@app.get("/stats")
async def stats():
    count = store.get_invoice_count()
    fee = Decimal("0.50")
    return {
        "invoices_generated": count,
        "usdt_collected": str((count * fee).quantize(Decimal("0.01"))),
        "price_per_invoice": "0.50",
    }


@app.post("/api/v1/invoice")
async def invoice_endpoint(
    request: Request,
    req: Optional[InvoiceRequest] = Body(default=None),
):
    resource_url = str(request.url)

    asp_wallet = os.getenv("ASP_WALLET", "")
    if not asp_wallet:
        return JSONResponse(
            status_code=500,
            content={
                "error": "server_error",
                "message": "ASP_WALLET not configured",
            },
        )

    tx_hash = req.payment_tx_hash if req else None
    # Set by the OKX SDK middleware once it has verified PAYMENT-SIG; a request
    # that reaches the handler with it has already paid.
    paid_via_sdk = getattr(request.state, "payment_payload", None) is not None

    # x402: any unpaid request (including an empty probe body) must receive the
    # payment challenge first, before any business-input validation.
    if not paid_via_sdk and not tx_hash:
        return _payment_required_response(resource_url, asp_wallet)

    desc = (req.description or "").strip() if req else ""
    if not (10 <= len(desc) <= 2000):
        msg = (
            "Description must be at least 10 characters"
            if len(desc) < 10
            else "Description must not exceed 2000 characters"
        )
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_description", "message": msg},
        )

    if paid_via_sdk:
        payment_reference = SDK_PAYMENT_REFERENCE
    else:
        verify_result = verify_payment(
            tx_hash=tx_hash,
            challenge_id=req.challenge_id if req else None,
            expected_amount=PRICE_USDT,
            expected_receiver=asp_wallet,
        )
        if not verify_result.get("verified"):
            # Re-advertise x402 requirements so the caller can retry correctly.
            return _payment_required_response(resource_url, asp_wallet)
        payment_reference = tx_hash

    try:
        tax_rate = _requested_tax_rate(req)
    except (ArithmeticError, ValueError):
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_tax_rate",
                "message": "tax_rate must be a decimal between 0 and 1, e.g. 0.08",
            },
        )

    try:
        invoice = await run_in_threadpool(
            create_invoice,
            description=desc,
            tax_rate=tax_rate,
            issuer=_issuer_from(req),
            payment_tx_hash=payment_reference,
            client=_client_from(req),
            currency=_currency_from(req),
        )
    except Exception:
        logger.exception("Invoice creation failed")
        return JSONResponse(
            status_code=422,
            content={
                "error": "parse_error",
                "message": "Cannot extract invoice data from description",
            },
        )

    try:
        pdf_bytes = generate_invoice_pdf(invoice, logo=(req.logo or "") if req else "")
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    except Exception:
        logger.exception("PDF generation failed")
        return JSONResponse(
            status_code=500,
            content={
                "error": "server_error",
                "message": "PDF generation failed",
            },
        )

    try:
        store.incr_invoices()
    except Exception:
        logger.warning("Failed to record invoice stat", exc_info=True)

    return {
        "invoice": invoice.model_dump(),
        "pdf": pdf_b64,
    }


# Serve the demo dashboard from the same origin as the API (so the browser's
# fetch calls hit this app directly — no CORS/port juggling). Mounted last so
# it never shadows the /health and /api routes above.
_DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard")
if os.path.isdir(_DASHBOARD_DIR):
    app.mount("/", StaticFiles(directory=_DASHBOARD_DIR, html=True), name="dashboard")
