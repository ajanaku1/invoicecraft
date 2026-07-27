from __future__ import annotations

import base64
import logging
import os
from decimal import Decimal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app import store
from app.invoice import create_invoice
from app.models import InvoiceRequest, IssuerInfo
from app.pdf_engine import generate_invoice_pdf
from app.tax import get_tax_rate
from app.x402 import generate_challenge, verify_payment

logger = logging.getLogger(__name__)
app = FastAPI(title="InvoiceCraft")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "InvoiceCraft",
        "payment_mode": os.getenv("PAYMENT_VERIFY_MODE", "mock").lower(),
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
async def invoice_endpoint(req: InvoiceRequest):
    desc = req.description.strip()

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

    asp_wallet = os.getenv("ASP_WALLET", "")
    if not asp_wallet:
        return JSONResponse(
            status_code=500,
            content={
                "error": "server_error",
                "message": "ASP_WALLET not configured",
            },
        )

    if req.payment_tx_hash:
        verify_result = verify_payment(
            tx_hash=req.payment_tx_hash,
            challenge_id=req.challenge_id,
            expected_amount=Decimal("0.50"),
            expected_receiver=asp_wallet,
        )

        if not verify_result.get("verified"):
            return JSONResponse(
                status_code=402,
                headers={
                    "X-Payment-Required": "true",
                    "X-Payment-Status": "pending",
                    "X-Payment-Tx-Hash": req.payment_tx_hash,
                },
                content={
                    "error": "payment_failed",
                    "message": "Payment verification failed on X Layer",
                    "payment": {
                        "tx_hash": req.payment_tx_hash,
                        "status": "failed",
                    },
                },
            )

        tax_rate = get_tax_rate()
        issuer = IssuerInfo(
            name=os.getenv("INVOICE_ISSUER_NAME", "InvoiceCraft AI"),
            address=asp_wallet,
            email=os.getenv("INVOICE_ISSUER_EMAIL", "invoice@craft.dev"),
        )

        try:
            invoice = await run_in_threadpool(
                create_invoice,
                description=desc,
                tax_rate=tax_rate,
                issuer=issuer,
                payment_tx_hash=req.payment_tx_hash,
            )
        except Exception as exc:
            logger.exception("Invoice creation failed")
            return JSONResponse(
                status_code=422,
                content={
                    "error": "parse_error",
                    "message": "Cannot extract invoice data from description",
                },
            )

        try:
            pdf_bytes = generate_invoice_pdf(invoice)
            pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        except Exception as exc:
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

    challenge_id = generate_challenge()

    return JSONResponse(
        status_code=402,
        headers={
            "X-Payment-Required": "true",
            "X-Payment-Challenge": challenge_id,
            "X-Payment-Amount": "0.50",
            "X-Payment-Token": "USDT",
            "X-Payment-Chain": "eip155:196",
            "X-Payment-Receiver": asp_wallet,
        },
        content={
            "error": "payment_required",
            "message": "Pay 0.50 USDT on X Layer to generate invoice",
            "payment": {
                "amount": "0.50",
                "token": "USDT",
                "chain": "eip155:196",
                "chain_id": "0xc4",
                "receiver": asp_wallet,
                "challenge_id": challenge_id,
                "memo": challenge_id,
                # On-chain details for wallet payment. token_contract is empty
                # unless the operator configures USDT_CONTRACT; when empty the UI
                # falls back to simulated (mock) payment.
                "token_contract": os.getenv("USDT_CONTRACT", ""),
                "decimals": int(os.getenv("USDT_DECIMALS", "6")),
                "rpc_url": os.getenv("XLAYER_RPC", ""),
            },
        },
    )


# Serve the demo dashboard from the same origin as the API (so the browser's
# fetch calls hit this app directly — no CORS/port juggling). Mounted last so
# it never shadows the /health and /api routes above.
_DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard")
if os.path.isdir(_DASHBOARD_DIR):
    app.mount("/", StaticFiles(directory=_DASHBOARD_DIR, html=True), name="dashboard")
