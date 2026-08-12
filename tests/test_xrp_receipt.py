from __future__ import annotations

import pytest

from app.xrp.executor import FsaExecutionResult, XrplPaymentEvidence
from app.xrp.receipts import ReceiptError, build_receipt


BENEFICIARY = "0x" + "12" * 20


def evidence() -> XrplPaymentEvidence:
    return XrplPaymentEvidence(
        transaction_hash="AB" * 32,
        validated=True,
        result="tesSUCCESS",
        source_account="rSource",
        destination="rCoreVault",
        amount_drops=10_200_000,
        delivered_amount_drops=10_200_000,
        memo_data_hex="FE" + "00" * 41,
        destination_tag=None,
        flags=0,
        ledger_index=9_001,
        ledger_timestamp=1_099,
        fdc_round_id=812,
        fdc_proof_hash="0x" + "56" * 32,
    )


def outcome(**changes: object) -> FsaExecutionResult:
    values: dict[str, object] = {
        "status": "succeeded",
        "flare_transaction_hash": "0x" + "78" * 32,
        "flare_block_number": 12_345,
        "settlement_contract": "0x" + "90" * 20,
        "adapter": "0x" + "ab" * 20,
        "usd0_token": "0x" + "cd" * 20,
        "beneficiary": BENEFICIARY,
        "usd0_amount": 50_000_000,
        "settlement_id": "0x" + "ef" * 32,
        "adapter_event_index": 4,
    }
    values.update(changes)
    return FsaExecutionResult(**values)


def invoice() -> dict[str, object]:
    return {
        "id": "xrp_receipt_1",
        "network": "coston2",
        "beneficiary": BENEFICIARY,
        "canonical_hash": "0x" + "34" * 32,
        "invoice": {"total": "50.00", "currency": "USD"},
        "quote": {"net_mint_uba": 10_200_000},
        "fsa_evidence": {
            "packed_user_operation_hex": "0x1234",
            "user_op_hash": "0x" + "99" * 32,
        },
    }


def test_receipt_binds_both_ledgers_fdc_fsa_and_test_liquidity() -> None:
    receipt = build_receipt(invoice(), evidence(), outcome(), settled_at=1_234)

    assert receipt["status"] == "paid"
    assert receipt["network"] == "coston2"
    assert receipt["xrpl"]["explorer_url"].startswith("https://testnet.xrpl.org/transactions/")
    assert receipt["flare"]["explorer_url"].startswith("https://coston2-explorer.flare.network/tx/")
    assert receipt["fdc"]["round_id"] == 812
    assert receipt["fsa"]["packed_user_operation_hex"] == "0x1234"
    assert receipt["flare"]["fxrp_input_uba"] == 10_200_000
    assert receipt["liquidity"]["label"] == "Test liquidity — not a real Coston2 market"


@pytest.mark.parametrize(
    "changed",
    [
        {"beneficiary": "0x" + "44" * 20},
        {"usd0_amount": 49_999_999},
        {"status": "pending"},
        {"fdc_proof_hash": None},
        {"flare_transaction_hash": "0x1234"},
        {"adapter": "0x1234"},
        {"adapter_event_index": -1},
    ],
)
def test_receipt_rejects_inexact_or_incomplete_evidence(changed: dict[str, object]) -> None:
    payment = evidence()
    settlement = outcome()
    if "fdc_proof_hash" in changed:
        payment = XrplPaymentEvidence(**{**payment.__dict__, **changed})
    else:
        settlement = outcome(**changed)

    with pytest.raises(ReceiptError):
        build_receipt(invoice(), payment, settlement, settled_at=1_234)
