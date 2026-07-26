from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, model_validator


class InvoiceRequest(BaseModel):
    description: str
    payment_tx_hash: Optional[str] = None
    challenge_id: Optional[str] = None

    @model_validator(mode="after")
    def check_challenge_if_paying(self):
        if self.payment_tx_hash and not self.challenge_id:
            raise ValueError("challenge_id is required when payment_tx_hash is provided")
        return self


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
