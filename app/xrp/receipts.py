"""Validated dual-ledger XRP settlement receipts."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .executor import FsaExecutionResult, XrplPaymentEvidence


class ReceiptError(ValueError):
    """Raised when settlement evidence cannot prove an exact payout."""


def build_receipt(
    invoice: dict[str, object],
    payment: XrplPaymentEvidence,
    settlement: FsaExecutionResult,
    settled_at: int,
) -> dict[str, object]:
    expected = _expected_payout(invoice)
    beneficiary = _text(invoice, "beneficiary").lower()
    _validate_evidence(payment, settlement, beneficiary, expected, settled_at)
    return {
        "schema_version": 1,
        "status": "paid",
        "network": "coston2",
        "invoice_id": _text(invoice, "id"),
        "canonical_hash": _text(invoice, "canonical_hash"),
        "settled_at": settled_at,
        "payout": {
            "beneficiary": beneficiary,
            "currency": "USD₮0",
            "amount_uba": expected,
            "token": settlement.usd0_token,
        },
        "xrpl": _xrpl_section(payment),
        "fdc": {"round_id": payment.fdc_round_id, "proof_hash": payment.fdc_proof_hash},
        "fsa": _fsa_section(invoice),
        "flare": _flare_section(settlement, _fxrp_input(invoice)),
        "liquidity": _liquidity_section(settlement),
    }


def _validate_evidence(
    payment: XrplPaymentEvidence,
    settlement: FsaExecutionResult,
    beneficiary: str,
    expected: int,
    settled_at: int,
) -> None:
    if settlement.status != "succeeded" or settlement.beneficiary.lower() != beneficiary:
        raise ReceiptError("settlement does not bind the beneficiary")
    if settlement.usd0_amount != expected:
        raise ReceiptError("settlement payout is not exact")
    if payment.fdc_round_id is None or payment.fdc_proof_hash is None:
        raise ReceiptError("FDC evidence is incomplete")
    if not payment.validated or payment.result != "tesSUCCESS":
        raise ReceiptError("XRPL payment is not finalized successfully")
    if isinstance(settled_at, bool) or not isinstance(settled_at, int) or settled_at < 0:
        raise ReceiptError("settlement timestamp is malformed")
    _hash(payment.transaction_hash, "XRPL transaction hash", prefix=False)
    _hash(payment.fdc_proof_hash, "FDC proof hash")
    _hash(settlement.flare_transaction_hash, "Flare transaction hash")
    _hash(settlement.settlement_id, "settlement ID")
    _address(settlement.settlement_contract, "settlement contract")
    _address(settlement.adapter, "test-liquidity adapter")
    _address(settlement.usd0_token, "USD₮0 token")
    if settlement.flare_block_number <= 0 or settlement.adapter_event_index < 0:
        raise ReceiptError("Flare receipt position is malformed")


def _expected_payout(invoice: dict[str, object]) -> int:
    details = invoice.get("invoice")
    if not isinstance(details, dict) or details.get("currency") not in {"USD", "USD₮0"}:
        raise ReceiptError("invoice currency is not supported")
    try:
        units = Decimal(str(details["total"])) * Decimal(1_000_000)
    except (InvalidOperation, KeyError) as error:
        raise ReceiptError("invoice total is malformed") from error
    if units <= 0 or units != units.to_integral_value():
        raise ReceiptError("invoice total cannot be represented exactly")
    return int(units)


def _xrpl_section(payment: XrplPaymentEvidence) -> dict[str, object]:
    return {
        "transaction_hash": payment.transaction_hash,
        "validated_ledger_index": payment.ledger_index,
        "delivered_amount_drops": payment.delivered_amount_drops,
        "explorer_url": f"https://testnet.xrpl.org/transactions/{payment.transaction_hash}",
    }


def _fsa_section(invoice: dict[str, object]) -> dict[str, object]:
    value = invoice.get("fsa_evidence")
    if not isinstance(value, dict):
        raise ReceiptError("FSA evidence is missing")
    packed = _text(value, "packed_user_operation_hex")
    user_hash = _text(value, "user_op_hash")
    _hex(packed, "PackedUserOperation", minimum_bytes=1)
    _hash(user_hash, "UserOp hash")
    return {"packed_user_operation_hex": packed, "user_op_hash": user_hash}


def _fxrp_input(invoice: dict[str, object]) -> int:
    quote = invoice.get("quote")
    value = quote.get("net_mint_uba") if isinstance(quote, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReceiptError("settlement FXRP input is missing")
    return value


def _flare_section(
    settlement: FsaExecutionResult, fxrp_input_uba: int
) -> dict[str, object]:
    return {
        "transaction_hash": settlement.flare_transaction_hash,
        "block_number": settlement.flare_block_number,
        "settlement_contract": settlement.settlement_contract,
        "settlement_id": settlement.settlement_id,
        "fxrp_input_uba": fxrp_input_uba,
        "explorer_url": f"https://coston2-explorer.flare.network/tx/{settlement.flare_transaction_hash}",
    }


def _liquidity_section(settlement: FsaExecutionResult) -> dict[str, object]:
    return {
        "label": "Test liquidity — not a real Coston2 market",
        "adapter": settlement.adapter,
        "event_index": settlement.adapter_event_index,
    }


def _text(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ReceiptError(f"{key} is missing")
    return item


def _hash(value: str, label: str, prefix: bool = True) -> None:
    normalized = value[2:] if prefix and value.startswith("0x") else value
    if len(normalized) != 64:
        raise ReceiptError(f"{label} is malformed")
    _hex("0x" + normalized, label, minimum_bytes=32)


def _address(value: str, label: str) -> None:
    if not value.startswith("0x") or len(value) != 42:
        raise ReceiptError(f"{label} is malformed")
    _hex(value, label, minimum_bytes=20)


def _hex(value: str, label: str, minimum_bytes: int) -> None:
    if not value.startswith("0x") or len(value) < 2 + minimum_bytes * 2 or len(value) % 2:
        raise ReceiptError(f"{label} is malformed")
    try:
        bytes.fromhex(value[2:])
    except ValueError as error:
        raise ReceiptError(f"{label} is malformed") from error
