from __future__ import annotations

from collections.abc import Mapping

import pytest

import app.xrp.live as live
from app.xrp.instructions import build_contract_user_operations, build_custom_instruction
from app.xrp.operator import OperatorError
from app.xrp.operator_live import (
    Coston2OperatorConfig,
    LiveCoston2OperatorBackend,
    LiveSettlementUserOperationBuilder,
    TrustedXrplPaymentReader,
    _settlement_outcome,
)
from app.xrp.rpc import _packed_user_operation_identity


XRPL_HASH = "AB" * 32
SIGNER = "0x" + "12" * 20
PERSONAL = "0x" + "34" * 20
SETTLEMENT = "0x" + "56" * 20
ADAPTER = "0x" + "78" * 20
USD0 = "0xc1a5b41512496b80903d1f32d6dea3a73212e71f"
FXRP = "0x0b6a3645c240605887a5532109323a3e12273dc7"
PACKED = build_contract_user_operations(
    PERSONAL, 7, ((SETTLEMENT, bytes.fromhex("12345678")),)
)
MEMO = build_custom_instruction(0, 0, PACKED)


def _rpc_response(request: dict[str, object], result: object) -> Mapping[str, object]:
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def test_trusted_reader_parses_only_a_final_exact_testnet_payment() -> None:
    def transport(
        _url: str, request: dict[str, object], _timeout: int
    ) -> Mapping[str, object]:
        method = request["method"]
        if method == "server_info":
            result = {
                "info": {
                    "network_id": 1,
                    "validated_ledger": {"seq": 9_005},
                }
            }
        else:
            result = {
                "hash": XRPL_HASH,
                "validated": True,
                "TransactionType": "Payment",
                "Account": "rSource",
                "Destination": "rCoreVault",
                "Amount": "10200000",
                "Flags": 0,
                "ledger_index": 9_001,
                "date": 1_000,
                "Memos": [{"Memo": {"MemoData": MEMO.memo_data_hex}}],
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "delivered_amount": "10200000",
                },
            }
        return _rpc_response(request, result)

    evidence = TrustedXrplPaymentReader(transport=transport).read(XRPL_HASH)

    assert evidence.transaction_hash == XRPL_HASH
    assert evidence.source_account == "rSource"
    assert evidence.memo_data_hex == MEMO.memo_data_hex
    assert evidence.ledger_timestamp == 946_685_800
    assert evidence.fdc_round_id is None
    assert evidence.fdc_proof_hash is None


def test_trusted_reader_requires_three_validated_ledgers() -> None:
    def transport(
        _url: str, request: dict[str, object], _timeout: int
    ) -> Mapping[str, object]:
        if request["method"] == "server_info":
            result = {"info": {"network_id": 1, "validated_ledger": {"seq": 9_002}}}
        else:
            result = {
                "hash": XRPL_HASH,
                "validated": True,
                "TransactionType": "Payment",
                "Account": "rSource",
                "Destination": "rCoreVault",
                "Amount": "10200000",
                "ledger_index": 9_001,
                "date": 1_000,
                "Memos": [{"Memo": {"MemoData": MEMO.memo_data_hex}}],
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "delivered_amount": "10200000",
                },
            }
        return _rpc_response(request, result)

    with pytest.raises(OperatorError, match="three validated-ledger"):
        TrustedXrplPaymentReader(transport=transport).read(XRPL_HASH)


def test_trusted_reader_rejects_a_non_payment_even_with_payment_like_fields() -> None:
    transaction = {
        "hash": XRPL_HASH,
        "validated": True,
        "TransactionType": "OfferCreate",
        "Account": "rSource",
        "Destination": "rCoreVault",
        "Amount": "10200000",
        "ledger_index": 9_001,
        "Memos": [{"Memo": {"MemoData": MEMO.memo_data_hex}}],
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "delivered_amount": "10200000",
        },
    }

    with pytest.raises(OperatorError, match="not a Payment"):
        TrustedXrplPaymentReader._evidence(transaction)


def _config() -> Coston2OperatorConfig:
    return Coston2OperatorConfig(
        signer=SIGNER,
        verifier_url="https://fdc-verifiers-testnet.flare.network",
        verifier_api_key="test-verifier-key-placeholder",
        da_layer_url="https://ctn2-data-availability.flare.network",
        settlement_contract=SETTLEMENT,
        adapter=ADAPTER,
        fxrp_token=FXRP,
        usd0_token=USD0,
    )


def test_operator_config_rejects_token_impostors() -> None:
    with pytest.raises(OperatorError, match="canonical Coston2"):
        Coston2OperatorConfig(
            signer=SIGNER,
            verifier_url="https://fdc-verifiers-testnet.flare.network",
            verifier_api_key="key",
            da_layer_url="https://ctn2-data-availability.flare.network",
            settlement_contract=SETTLEMENT,
            adapter=ADAPTER,
            fxrp_token="0x" + "99" * 20,
            usd0_token=USD0,
        )


def _invoice() -> dict[str, object]:
    return {
        "id": "xrp_live",
        "beneficiary": "0x" + "90" * 20,
        "canonical_hash": "0x" + "ab" * 32,
        "invoice": {"total": "50.00", "currency": "USD"},
        "quote": {"net_mint_uba": 25_000_000},
        "fsa_evidence": {"packed_user_operation_hex": "0x" + PACKED.hex()},
    }


def _payment():
    return TrustedXrplPaymentReader._evidence(
        {
            "hash": XRPL_HASH,
            "validated": True,
            "TransactionType": "Payment",
            "Account": "rSource",
            "Destination": "rCoreVault",
            "Amount": "10200000",
            "ledger_index": 9_001,
            "date": 1_000,
            "Memos": [{"Memo": {"MemoData": MEMO.memo_data_hex}}],
            "meta": {
                "TransactionResult": "tesSUCCESS",
                "delivered_amount": "10200000",
            },
        }
    )


def test_live_backend_reuses_phase0_state_without_persisting_verifier_key(
    monkeypatch,
) -> None:
    monkeypatch.setattr(live, "_trusted_clients", lambda _timeout: (object(), object()))
    monkeypatch.setattr(live, "_validate_networks", lambda _xrpl, _flare: None)
    monkeypatch.setattr(
        live,
        "_resolve_contracts",
        lambda _flare: {"asset_manager": "0x" + "cd" * 20},
    )
    monkeypatch.setattr(
        live,
        "_live_settings",
        lambda _flare, _target: {"core_vault": "rCoreVault"},
    )
    backend = LiveCoston2OperatorBackend(_config())

    context = backend.start(_invoice(), _payment())

    assert context["stage"] == "poll-xaman"
    assert context["signer"] == SIGNER
    assert context["personal_account"] == PERSONAL
    assert context["nonce"] == "7"
    assert context["packed_user_operation_hex"] == PACKED.hex()
    assert "test-verifier-key-placeholder" not in str(context)


def test_live_userop_builder_reads_the_current_fsa_nonce(monkeypatch) -> None:
    monkeypatch.setattr(live, "_trusted_clients", lambda _timeout: (object(), object()))
    monkeypatch.setattr(live, "_validate_flare_chain", lambda _flare: None)
    monkeypatch.setattr(
        live, "_registry", lambda _flare, name: "0x" + "cd" * 20
    )
    monkeypatch.setattr(live, "_eth_address_call", lambda *_arguments: PERSONAL)
    monkeypatch.setattr(live, "_eth_quantity_call", lambda *_arguments: 19)
    invoice = _invoice()
    invoice["quote"] = {
        "maximum_fxrp_uba": 25_000_000,
        "net_mint_uba": 25_000_000,
        "settlement_deadline_at": 2_000,
    }
    builder = LiveSettlementUserOperationBuilder(
        FXRP, SETTLEMENT, timeout_seconds=15
    )

    payload = builder.build(invoice, "rSource")

    sender, nonce = _packed_user_operation_identity(payload)
    assert sender == PERSONAL
    assert nonce == 19


def test_live_backend_delegates_sign_requests_and_public_progress(monkeypatch) -> None:
    backend = LiveCoston2OperatorBackend(_config())
    context = {"stage": "poll-xaman"}

    def prepare_fdc(_config_value, store, _timeout):
        state = store.read()
        state.update(
            stage="prepare-fdc",
            fdc_sign_request=live.make_sign_request(
                SIGNER, "0x" + "11" * 20, 1, "0x1234", "fdc-request"
            ),
        )
        store.write(state)
        return state["fdc_sign_request"]

    def record_fdc(_config_value, store, transaction_hash, _timeout):
        state = store.read()
        state.update(
            stage="record-fdc",
            fdc_transaction_hash=transaction_hash,
            fdc_round_id="812",
        )
        store.write(state)
        return state

    def prepare_execute(_config_value, store, _timeout):
        state = store.read()
        state.update(
            stage="prepare-execute",
            fdc_response_hash="0x" + "22" * 32,
            execute_sign_request=live.make_sign_request(
                SIGNER,
                "0x" + "33" * 20,
                0,
                "0x5678",
                "execute-direct-mint",
            ),
        )
        store.write(state)
        return state["execute_sign_request"]

    monkeypatch.setattr(live, "prepare_fdc", prepare_fdc)
    monkeypatch.setattr(live, "record_fdc", record_fdc)
    monkeypatch.setattr(live, "prepare_execute", prepare_execute)

    fdc = backend.prepare_fdc(context)
    progress = backend.record_fdc(fdc.context, "0x" + "44" * 32)
    execute = backend.prepare_execute(progress.context)

    assert fdc.sign_request["purpose"] == "fdc-request"
    assert progress.fdc_round_id == 812
    assert execute.sign_request["purpose"] == "execute-direct-mint"
    assert execute.fdc_proof_hash == "0x" + "22" * 32


def test_product_outcome_requires_exact_settlement_adapter_and_transfer_logs() -> None:
    beneficiary = "0x" + "90" * 20
    canonical_hash = "0x" + "ab" * 32
    settlement_id = "0x" + "bc" * 32
    fxrp_input, usd0_output = 25_000_000, 50_000_000
    product = {
        "canonical_hash": canonical_hash,
        "beneficiary": beneficiary,
        "exact_usd0_uba": usd0_output,
        "fxrp_input_uba": fxrp_input,
        "settlement_contract": SETTLEMENT,
        "adapter": ADAPTER,
        "fxrp_token": FXRP,
        "usd0_token": USD0,
    }
    values = "0x" + f"{fxrp_input:064x}{usd0_output:064x}"
    address_topic = lambda value: "0x" + "00" * 12 + value[2:]
    topic = lambda signature: "0x" + live.keccak256(signature.encode()).hex()
    receipt = {
        "status": "0x1",
        "transactionHash": "0x" + "44" * 32,
        "blockNumber": "0x3039",
        "logs": [
            {
                "address": SETTLEMENT,
                "topics": [
                    topic("InvoiceSettled(bytes32,bytes32,address,uint256,uint256)"),
                    settlement_id,
                    canonical_hash,
                    address_topic(beneficiary),
                ],
                "data": values,
            },
            {
                "address": ADAPTER,
                "logIndex": "0x4",
                "topics": [
                    topic("TestLiquidityUsed(bytes32,address,uint256,uint256)"),
                    settlement_id,
                    address_topic(beneficiary),
                ],
                "data": values,
            },
            {
                "address": USD0,
                "topics": [
                    topic("Transfer(address,address,uint256)"),
                    address_topic(ADAPTER),
                    address_topic(beneficiary),
                ],
                "data": "0x" + f"{usd0_output:064x}",
            },
        ],
    }

    outcome = _settlement_outcome(product, "0x" + "44" * 32, receipt)

    assert outcome.usd0_amount == usd0_output
    assert outcome.settlement_id == settlement_id
    assert outcome.adapter_event_index == 4
    receipt["logs"][2]["data"] = "0x" + f"{usd0_output - 1:064x}"
    with pytest.raises(OperatorError, match="USD0 Transfer"):
        _settlement_outcome(product, "0x" + "44" * 32, receipt)
