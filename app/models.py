from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class InvoiceRequest(BaseModel):
    # Optional so an unpaid probe (empty body) still reaches the x402 402 path
    # instead of failing request validation.
    description: Optional[str] = None
    payment_tx_hash: Optional[str] = None
    # Legacy field, kept for backward compatibility; x402 uses the on-chain tx
    # as the payment proof, so no server-issued challenge is required.
    challenge_id: Optional[str] = None


class InvoiceLineItem(BaseModel):
    description: str
    quantity: int
    unit_price: str
    amount: str


class IssuerInfo(BaseModel):
    name: str
    address: str
    email: str


class ClientInfo(BaseModel):
    name: str
    email: str


class PaymentInfo(BaseModel):
    amount: str
    token: str
    chain: str
    receiver: str
    challenge_id: str
    memo: str


class Invoice(BaseModel):
    issuer: IssuerInfo
    client: ClientInfo
    line_items: list[InvoiceLineItem]
    invoice_number: str
    subtotal: str
    tax_rate: str
    tax_amount: str
    total: str
    currency: str = "USD"
    due_date: str
    status: str
    notes: str = ""


class InvoiceResponse(BaseModel):
    invoice: Invoice
    pdf: str


class ErrorResponse(BaseModel):
    error: str
    message: str
    payment: Optional[PaymentInfo] = None
