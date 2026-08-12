"""Fail-closed, browser-signed Phase 0 Testnet runner primitives."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import ParseResult, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .instructions import CustomInstruction, build_custom_instruction, build_unsigned_payment, keccak256
from .rpc import (
    COSTON2_CHAIN_ID,
    FLARE_CONTRACT_REGISTRY,
    JsonRpcClient,
    TRUSTED_COSTON2_RPC_URL,
    TRUSTED_XRPL_TESTNET_RPC_URL,
    XRPL_TESTNET_NETWORK_ID,
    EXECUTE_DIRECT_MINTING_WITH_DATA_SELECTOR,
)


STATE_VERSION = 1
CHECKPOINT = "0xEE6D54382aA623f4D16e856193f5f8384E487002"
CHECKPOINT_SELECTOR = "0x80abd133"
VERIFIER_HOST = "fdc-verifiers-testnet.flare.network"
DA_LAYER_HOST = "ctn2-data-availability.flare.network"
XAMAN_USER_AGENT = "InvoiceCraft-Phase0/1.0"
STAGES = ("inspect", "xaman-pending-reconcile", "create-xaman", "poll-xaman", "prepare-fdc", "record-fdc", "prepare-execute", "finalize")
SIGN_REQUEST_PURPOSES = {
    "fdc-request",
    "execute-direct-mint",
    "deploy-product-contracts",
    "fund-test-liquidity",
}
_SECRET_MARKERS = ("secret", "api_key", "seed", "private", "authorization", "password")


class LiveError(ValueError):
    """Raised when a live step cannot establish all required public bindings."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, *_args: object) -> Request | None:
        raise HTTPError(request.full_url, 302, "redirect refused", {}, None)


@dataclass(frozen=True)
class LiveConfig:
    xaman_api_key: str
    xaman_api_secret: str
    xrpl_address: str
    signer: str
    verifier_url: str
    verifier_api_key: str
    da_layer_url: str


class StateStore:
    """A private, atomic public-state store that never accepts secret-like fields."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "run-state.json"

    def read(self) -> dict[str, object]:
        _refuse_symlink(self.directory)
        if not self.path.exists():
            raise LiveError("run state does not exist; run inspect first")
        _refuse_symlink(self.path)
        return _state_object(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, state: Mapping[str, object]) -> None:
        _refuse_symlink(self.directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _refuse_symlink(self.path)
        checked = _state_object(dict(state))
        if self.path.exists():
            previous_index = STAGES.index(self.read()["stage"])
            next_index = STAGES.index(checked["stage"])
            if next_index < previous_index:
                raise LiveError("run state stage cannot regress")
            if next_index > previous_index + 1:
                raise LiveError("run state can advance only one stage at a time")
        _reject_secret_state(checked)
        descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(checked, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write_sign_request(self, name: str, request: Mapping[str, str]) -> Path:
        if name not in {"fdc-sign-request.json", "execute-sign-request.json"}:
            raise LiveError("unrecognized sign request filename")
        _reject_secret_state(request)
        _sign_request_object(request)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / name
        _refuse_symlink(path)
        descriptor, temporary = tempfile.mkstemp(prefix=".request-", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(dict(request), output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path


def compute_gross_payment(lot_size_uba: int, fee_bips: int, minimum_fee_uba: int, memo_executor_fee_uba: int = 0) -> int:
    """Return the least gross drops whose post-protocol, memo-fee amount is one lot."""
    _nonnegative(lot_size_uba, "lot size")
    _nonnegative(fee_bips, "fee bips")
    _nonnegative(minimum_fee_uba, "minimum fee")
    _nonnegative(memo_executor_fee_uba, "memo executor fee")
    required = lot_size_uba + memo_executor_fee_uba
    gross = max(required + minimum_fee_uba, 1)
    while _net_minted(gross, fee_bips, minimum_fee_uba, memo_executor_fee_uba) < lot_size_uba:
        gross += max(1, (gross * fee_bips + 9_999) // 10_000)
    while gross > 1 and _net_minted(gross - 1, fee_bips, minimum_fee_uba, memo_executor_fee_uba) >= lot_size_uba:
        gross -= 1
    return gross


def build_checkpoint_user_op(personal_account: str, nonce: int) -> tuple[bytes, CustomInstruction]:
    """Encode the settled wallet-0 zero-value Checkpoint user operation."""
    account = _address(personal_account, "personal account")
    _nonnegative(nonce, "nonce")
    call_data = _encode_execute_user_op(account, CHECKPOINT, CHECKPOINT_SELECTOR)
    payload = _encode_packed_user_op(account, nonce, call_data)
    return payload, build_custom_instruction(0, 0, payload)


def load_config(path: Path) -> LiveConfig:
    """Read required configuration without exposing it to callers or logs."""
    _refuse_symlink(path)
    values = _dotenv(path)
    required = ("XAMAN_API_KEY", "XAMAN_API_SECRET", "XRPL_TESTNET_ADDRESS", "COSTON2_SIGNER_ADDRESS", "VERIFIER_URL_TESTNET", "VERIFIER_API_KEY_TESTNET", "COSTON2_DA_LAYER_URL")
    if any(not values.get(key) for key in required):
        raise LiveError(".env is missing required non-empty Phase 0 configuration")
    if values.get("COSTON2_RPC_URL", TRUSTED_COSTON2_RPC_URL) != TRUSTED_COSTON2_RPC_URL:
        raise LiveError("Coston2 RPC must equal the trusted Testnet endpoint")
    if values.get("XRPL_TESTNET_RPC_URL", TRUSTED_XRPL_TESTNET_RPC_URL) != TRUSTED_XRPL_TESTNET_RPC_URL:
        raise LiveError("XRPL RPC must equal the trusted Testnet endpoint")
    return LiveConfig(
        values["XAMAN_API_KEY"], values["XAMAN_API_SECRET"], _xrpl_classic_address(values["XRPL_TESTNET_ADDRESS"]),
        _address(values["COSTON2_SIGNER_ADDRESS"], "Coston2 signer"), _official_base_url(values["VERIFIER_URL_TESTNET"], VERIFIER_HOST, "verifier URL"),
        values["VERIFIER_API_KEY_TESTNET"], _official_base_url(values["COSTON2_DA_LAYER_URL"], DA_LAYER_HOST, "DA layer URL"),
    )


def inspect(config: LiveConfig, store: StateStore, timeout_seconds: int = 15) -> dict[str, object]:
    """Read live public values, construct the commitment, and persist inspect state."""
    xrpl, flare = _trusted_clients(timeout_seconds)
    state = _inspect_state(config, xrpl, flare)
    store.write(state)
    return state


def create_xaman(config: LiveConfig, store: StateStore, timeout_seconds: int = 15, xaman_uuid: str | None = None) -> dict[str, object]:
    """Create one Xaman signing payload only after rebuilding fresh commitments."""
    previous = store.read()
    existing = _existing_xaman(previous, xaman_uuid)
    if existing is not None:
        return existing
    if previous["stage"] == "xaman-pending-reconcile":
        return _reconcile_pending_xaman(config, store, previous)
    if previous["stage"] != "inspect":
        raise LiveError("create-xaman requires fresh inspect state")
    xrpl, flare = _trusted_clients(timeout_seconds)
    fresh = _inspect_state(config, xrpl, flare)
    if _commitment(previous) != _commitment(fresh):
        raise LiveError("live commitment changed; inspect again and review the new public state")
    payment, intent_digest = _xaman_payment(config, fresh)
    reconciled = _reconcile_or_record_intent(config, store, previous, fresh, payment, intent_digest, xaman_uuid)
    if reconciled is not None:
        return reconciled
    response = _post_json("https://xumm.app/api/v1/platform/payload", payment, config.xaman_api_key, config.xaman_api_secret)
    pending = _record_xaman_pending(fresh, store, response, intent_digest)
    return _reconcile_pending_xaman(config, store, pending)


def _existing_xaman(state: Mapping[str, object], xaman_uuid: str | None) -> dict[str, object] | None:
    if state["stage"] != "create-xaman":
        return None
    if xaman_uuid is not None and xaman_uuid != state.get("xaman_uuid"):
        raise LiveError("a different Xaman UUID cannot replace recorded state")
    return {"uuid": _text(state, "xaman_uuid"), "next": _mapping(state, "xaman_next")}


def _reconcile_pending_xaman(config: LiveConfig, store: StateStore, state: Mapping[str, object]) -> dict[str, object]:
    payment, intent_digest = _xaman_payment(config, state)
    if state.get("xaman_intent_digest") != intent_digest:
        raise LiveError("stored Xaman intent does not match pending reconciliation state")
    return _reconcile_xaman(config, store, dict(state), payment, intent_digest, _xaman_uuid(_text(state, "xaman_uuid")))


def _xaman_payment(config: LiveConfig, state: Mapping[str, object]) -> tuple[dict[str, object], str]:
    instruction = build_custom_instruction(0, 0, bytes.fromhex(_text(state, "packed_user_operation_hex")))
    payment = {
        "txjson": build_unsigned_payment(config.xrpl_address, str(_object(state["settings"], "saved settings")["core_vault"]), int(state["gross_drops"]), instruction),
        "options": {"force_network": "TESTNET"},
    }
    return payment, "0x" + keccak256(_canonical_json(payment)).hex()


def _reconcile_or_record_intent(config: LiveConfig, store: StateStore, previous: Mapping[str, object], fresh: dict[str, object], payment: Mapping[str, object], intent_digest: str, xaman_uuid: str | None) -> dict[str, object] | None:
    recorded = previous.get("xaman_intent_digest")
    if recorded is None:
        pending = dict(previous)
        pending["xaman_intent_digest"] = intent_digest
        store.write(pending)
        return None
    if recorded != intent_digest:
        raise LiveError("stored Xaman intent does not match the fresh commitment")
    if not xaman_uuid:
        raise LiveError("Xaman payload creation may have succeeded; retry with --xaman-uuid <existing UUID> to reconcile without creating another payload")
    pending = dict(fresh)
    pending.update({"stage": "xaman-pending-reconcile", "xaman_uuid": _xaman_uuid(xaman_uuid), "xaman_intent_digest": intent_digest})
    store.write(pending)
    return _reconcile_pending_xaman(config, store, pending)


def _record_xaman_pending(fresh: dict[str, object], store: StateStore, response: Mapping[str, object], intent_digest: str) -> dict[str, object]:
    uuid = _xaman_uuid(_text(response, "uuid"))
    fresh.update({"stage": "xaman-pending-reconcile", "xaman_uuid": uuid, "xaman_intent_digest": intent_digest})
    store.write(fresh)
    return fresh


def _reconcile_xaman(config: LiveConfig, store: StateStore, fresh: dict[str, object], payment: Mapping[str, object], intent_digest: str, xaman_uuid: str) -> dict[str, object]:
    xaman_uuid = _xaman_uuid(xaman_uuid)
    payload = _get_json(
        f"https://xumm.app/api/v1/platform/payload/{xaman_uuid}",
        _xaman_headers(config), "xumm.app",
    )
    txjson = _mapping(_mapping(payload, "payload"), "request_json")
    force_network = _text(_mapping(payload, "meta"), "force_network")
    if force_network != "TESTNET":
        raise LiveError("supplied Xaman UUID was not requested for Testnet")
    request_body = {"txjson": dict(txjson), "options": {"force_network": force_network}}
    if "0x" + keccak256(_canonical_json(request_body)).hex() != intent_digest or _canonical_json(request_body) != _canonical_json(payment):
        raise LiveError("supplied Xaman UUID does not match the saved unsigned payment intent")
    fresh.update({"stage": "create-xaman", "xaman_uuid": xaman_uuid, "xaman_next": {"always": _xaman_sign_url(xaman_uuid)}, "xaman_intent_digest": intent_digest})
    store.write(fresh)
    return {"uuid": xaman_uuid, "next": fresh["xaman_next"]}


def _inspect_state(config: LiveConfig, xrpl: JsonRpcClient, flare: JsonRpcClient) -> dict[str, object]:
    _validate_networks(xrpl, flare)
    contracts = _resolve_contracts(flare)
    personal = _eth_address_call(
        flare, contracts["master_account_controller"],
        _selector("getPersonalAccount(string)") + _abi_string(config.xrpl_address)[2:],
    )
    nonce = _eth_quantity_call(
        flare, contracts["master_account_controller"], _selector("getNonce(address)") + _abi_address(personal).hex(),
    )
    settings = _live_settings(flare, contracts["asset_manager"])
    payload, instruction = build_checkpoint_user_op(personal, nonce)
    gross = compute_gross_payment(settings["lot_size_uba"], settings["fee_bips"], settings["minimum_fee_uba"])
    return {
        "version": STATE_VERSION, "stage": "inspect", "xrpl_address": config.xrpl_address,
        "signer": config.signer, "personal_account": personal, "nonce": str(nonce),
        "packed_user_operation_hex": payload.hex(), "memo_data_hex": instruction.memo_data_hex,
        "gross_drops": gross, "contracts": contracts, "settings": settings,
    }


def make_sign_request(signer: str, target: str, value: int, calldata: str, purpose: str) -> dict[str, str]:
    """Public, browser-safe EIP-1193 request bound to exact transaction bytes."""
    raw = _hex_bytes(calldata, "calldata")
    request = {"version": "1", "purpose": purpose, "chain_id": "0x72", "signer": _address(signer, "signer"), "to": _address(target, "target"), "value": hex(value), "data": "0x" + raw.hex(), "calldata_hash": "0x" + keccak256(raw).hex()}
    _sign_request_object(request)
    return request


def poll_xaman(config: LiveConfig, store: StateStore, timeout_seconds: int = 15) -> dict[str, object]:
    """Recheck signed Xaman payload and the final XRPL payment without mutation."""
    state = store.read()
    if state["stage"] == "poll-xaman":
        return state
    if state["stage"] != "create-xaman":
        raise LiveError("poll-xaman requires a created Xaman payload")
    payload = _get_json(
        f"https://xumm.app/api/v1/platform/payload/{_text(state, 'xaman_uuid')}",
        _xaman_headers(config), "xumm.app",
    )
    meta = _mapping(payload, "meta")
    response = _mapping(payload, "response")
    _validate_xaman_resolution(meta, response)
    txid = _xrp_hash(_text(response, "txid"))
    xrpl, flare = _trusted_clients(timeout_seconds)
    _validate_networks(xrpl, flare)
    result = _object(xrpl.request("tx", [{"transaction": txid, "binary": False}]), "XRPL transaction")
    if _xrp_hash(_text(result, "hash")) != txid:
        raise LiveError("XRPL transaction hash does not match Xaman response")
    _validate_signed_xrpl_payment(result, state)
    transaction_ledger = _integer(result.get("ledger_index"))
    current_ledger = _current_validated_ledger(xrpl)
    confirmations = current_ledger - transaction_ledger + 1
    if confirmations < 3:
        raise LiveError("XRPL payment has fewer than three validated-ledger confirmations")
    next_state = dict(state)
    next_state.update({"stage": "poll-xaman", "xrpl_transaction_hash": txid, "xrpl_validated_ledger_index": transaction_ledger, "xrpl_current_validated_ledger_index": current_ledger, "xrpl_confirmations": confirmations})
    store.write(next_state)
    return next_state


def _validate_xaman_resolution(meta: Mapping[str, object], response: Mapping[str, object]) -> None:
    if meta.get("resolved") is not True or meta.get("signed") is not True:
        raise LiveError("Xaman payload is not resolved and signed")
    if response.get("dispatched_nodetype") != "TESTNET":
        raise LiveError("Xaman payload was not dispatched through Testnet")


def prepare_fdc(config: LiveConfig, store: StateStore, timeout_seconds: int = 15) -> dict[str, object]:
    """Ask the verifier for exact bytes and prepare a browser FDC request transaction."""
    state = store.read()
    if state["stage"] == "prepare-fdc":
        return _mapping(state, "fdc_sign_request")
    if state["stage"] != "poll-xaman":
        raise LiveError("prepare-fdc requires a final XRPL payment")
    verifier = _verifier_endpoint(config.verifier_url)
    request_body = {
        "attestationType": "0x" + b"XRPPayment".ljust(32, b"\0").hex(),
        "sourceId": "0x" + b"testXRP".ljust(32, b"\0").hex(),
        "requestBody": {"transactionId": _text(state, "xrpl_transaction_hash"), "proofOwner": config.signer},
    }
    verifier_response = _post_public_json(verifier, request_body, {"X-API-KEY": config.verifier_api_key})
    request_bytes = _hex_bytes(_text(verifier_response, "abiEncodedRequest"), "FDC ABI request")
    _validate_fdc_request(request_bytes, _text(state, "xrpl_transaction_hash"), config.signer)
    xrpl, flare = _trusted_clients(timeout_seconds)
    _validate_networks(xrpl, flare)
    contracts = _require_live_contracts(flare, state, ("fdc_hub",))
    fee_configuration = _eth_address_call(flare, contracts["fdc_hub"], _selector("fdcRequestFeeConfigurations()"))
    fee = _eth_quantity_call(flare, fee_configuration, _selector("getRequestFee(bytes)") + _abi_dynamic(request_bytes).hex())
    calldata = _selector("requestAttestation(bytes)") + _abi_dynamic(request_bytes).hex()
    sign_request = make_sign_request(config.signer, contracts["fdc_hub"], fee, calldata, "fdc-request")
    next_state = dict(state)
    next_state.update({"stage": "prepare-fdc", "fdc_request_bytes": "0x" + request_bytes.hex(), "fdc_fee_wei": str(fee), "fdc_sign_request": sign_request})
    store.write(next_state)
    store.write_sign_request("fdc-sign-request.json", sign_request)
    return sign_request


def record_fdc(config: LiveConfig, store: StateStore, transaction_hash: str, timeout_seconds: int = 15) -> dict[str, object]:
    """Record only a mined browser transaction matching the saved FDC request."""
    state = store.read()
    tx_hash = _evm_hash(transaction_hash)
    if state["stage"] == "record-fdc":
        if state.get("fdc_transaction_hash", "").lower() != tx_hash.lower():
            raise LiveError("a different FDC transaction cannot replace recorded state")
        return state
    if state["stage"] != "prepare-fdc":
        raise LiveError("record-fdc requires a prepared FDC request")
    _, flare = _trusted_clients(timeout_seconds)
    _validate_flare_chain(flare)
    transaction = _object(flare.request("eth_getTransactionByHash", [tx_hash]), "FDC transaction")
    receipt = _object(flare.request("eth_getTransactionReceipt", [tx_hash]), "FDC receipt")
    verify_sign_request(_mapping(state, "fdc_sign_request"), transaction, config.signer)
    _validate_mined_transaction(transaction, receipt, tx_hash, "FDC request")
    if receipt.get("status") != "0x1":
        raise LiveError("FDC request transaction is not mined successfully")
    contracts = _require_live_contracts(flare, state, ("fdc_hub", "flare_systems_manager"))
    block_number, round_id = _fdc_round(flare, receipt, contracts["flare_systems_manager"])
    next_state = dict(state)
    next_state.update({"stage": "record-fdc", "fdc_transaction_hash": tx_hash, "fdc_block_number": block_number, "fdc_round_id": str(round_id)})
    store.write(next_state)
    return next_state


def _fdc_round(flare: JsonRpcClient, receipt: Mapping[str, object], manager: str) -> tuple[str, int]:
    block_number = _text(receipt, "blockNumber")
    block = _object(flare.request("eth_getBlockByNumber", [block_number, False]), "FDC block")
    if _text(block, "hash").lower() != _text(receipt, "blockHash").lower():
        raise LiveError("FDC block hash does not match transaction receipt")
    timestamp = _quantity(block.get("timestamp"))
    start = _eth_quantity_call(flare, manager, _selector("firstVotingRoundStartTs()"))
    epoch = _eth_quantity_call(flare, manager, _selector("votingEpochDurationSeconds()"))
    if epoch <= 0 or timestamp < start:
        raise LiveError("cannot bind FDC voting round")
    return block_number, (timestamp - start) // epoch


def prepare_execute(config: LiveConfig, store: StateStore, timeout_seconds: int = 15) -> dict[str, object]:
    """Fetch one finalized FDC proof and prepare exact direct-mint browser bytes."""
    return _prepare_execute(config, store, timeout_seconds, None)


def prepare_recovery_execute(config: LiveConfig, store: StateStore, timeout_seconds: int = 15) -> dict[str, object]:
    """Prepare an authorized recovery direct mint with an intentionally empty data field."""
    state = store.read()
    if state.get("packed_user_operation_hex") != "":
        raise LiveError("recovery direct mint requires an empty packed user operation")
    _xrp_hash(_text(state, "recovery_target_transaction_hash"))
    return _prepare_execute(config, store, timeout_seconds, "")


def _prepare_execute(config: LiveConfig, store: StateStore, timeout_seconds: int, user_operation_hex: str | None) -> dict[str, object]:
    state = store.read()
    if state["stage"] == "prepare-execute":
        return _mapping(state, "execute_sign_request")
    if state["stage"] != "record-fdc":
        raise LiveError("prepare-execute requires a recorded FDC transaction")
    _, flare = _trusted_clients(timeout_seconds)
    _validate_flare_chain(flare)
    contracts = _require_live_contracts(flare, state, ("fdc_verification", "relay", "asset_manager"))
    round_id = int(_text(state, "fdc_round_id"))
    _require_finalized_round(flare, contracts, round_id)
    sign_request, response_body, merkle = _execute_sign_request(config, state, contracts["asset_manager"], round_id, user_operation_hex)
    next_state = dict(state)
    next_state.update({"stage": "prepare-execute", "fdc_response_hash": "0x" + keccak256(response_body).hex(), "fdc_merkle_proof": "0x" + merkle.hex(), "execute_sign_request": sign_request})
    store.write(next_state)
    store.write_sign_request("execute-sign-request.json", sign_request)
    return sign_request


def _require_finalized_round(flare: JsonRpcClient, contracts: Mapping[str, str], round_id: int) -> None:
    protocol_id = _eth_quantity_call(flare, contracts["fdc_verification"], _selector("fdcProtocolId()"))
    calldata = _selector("isFinalized(uint256,uint256)") + _word(protocol_id).hex() + _word(round_id).hex()
    if _eth_quantity_call(flare, contracts["relay"], calldata) != 1:
        raise LiveError("FDC voting round is not finalized")


def _execute_sign_request(config: LiveConfig, state: Mapping[str, object], target: str, round_id: int, user_operation_hex: str | None = None) -> tuple[dict[str, str], bytes, bytes]:
    proof_response = _post_public_json(_da_proof_endpoint(config.da_layer_url), {"votingRoundId": round_id, "requestBytes": _text(state, "fdc_request_bytes")}, {})
    response_body = _response_tuple_body(_hex_bytes(_text(proof_response, "response_hex"), "FDC response"), state)
    merkle = _merkle_array(proof_response.get("proof"))
    proof = _word(64) + _word(64 + len(merkle)) + merkle + response_body
    operation_hex = _text(state, "packed_user_operation_hex") if user_operation_hex is None else user_operation_hex
    payload = _word(64) + _word(64 + len(proof)) + proof + _abi_bytes(bytes.fromhex(operation_hex))
    calldata = EXECUTE_DIRECT_MINTING_WITH_DATA_SELECTOR + payload.hex()
    return make_sign_request(config.signer, target, 0, calldata, "execute-direct-mint"), response_body, merkle


def finalize(config: LiveConfig, store: StateStore, transaction_hash: str, timeout_seconds: int = 15) -> dict[str, object]:
    """Verify final browser transaction, event/nonce binding, and emit evidence candidate."""
    state = store.read()
    tx_hash = _evm_hash(transaction_hash)
    if state["stage"] == "finalize":
        if state.get("execute_transaction_hash", "").lower() != tx_hash.lower():
            raise LiveError("a different execution transaction cannot replace finalized state")
        return _object(state.get("candidate_evidence"), "candidate evidence")
    if state["stage"] != "prepare-execute":
        raise LiveError("finalize requires a prepared direct-mint request")
    xrpl, flare = _trusted_clients(timeout_seconds)
    _validate_flare_chain(flare)
    transaction = _object(flare.request("eth_getTransactionByHash", [tx_hash]), "execute transaction")
    receipt = _object(flare.request("eth_getTransactionReceipt", [tx_hash]), "execute receipt")
    verify_sign_request(_mapping(state, "execute_sign_request"), transaction, config.signer)
    _validate_mined_transaction(transaction, receipt, tx_hash, "direct-mint")
    if receipt.get("status") != "0x1":
        raise LiveError("direct-mint transaction is not mined successfully")
    evidence = _candidate_evidence(state, xrpl, flare, transaction, receipt)
    from .rpc import RpcEvidenceError, validate_evidence
    try:
        validate_evidence(evidence, timeout_seconds)
    except RpcEvidenceError as error:
        raise LiveError("candidate evidence did not pass independent RPC verification") from error
    next_state = dict(state)
    next_state.update({"stage": "finalize", "execute_transaction_hash": tx_hash, "candidate_evidence": evidence})
    store.write(next_state)
    return evidence


def verify_sign_request(request: Mapping[str, object], tx: Mapping[str, object], signer: str) -> None:
    """Fail closed if a submitted public transaction differs from its request."""
    if request.get("chain_id") != "0x72" or _address(str(request.get("signer", "")), "request signer") != _address(signer, "signer"):
        raise LiveError("sign request signer or chain is invalid")
    if _address(str(tx.get("from", "")), "transaction signer") != _address(signer, "signer"):
        raise LiveError("submitted transaction signer does not match")
    if _address(str(tx.get("to", "")), "transaction target") != _address(str(request.get("to", "")), "request target"):
        raise LiveError("submitted transaction target does not match")
    if _quantity(tx.get("value")) != _quantity(request.get("value")) or str(tx.get("input", tx.get("data", ""))).lower() != str(request.get("data")).lower():
        raise LiveError("submitted transaction value or calldata does not match")
    if "0x" + keccak256(_hex_bytes(str(request["data"]), "request calldata")).hex() != request.get("calldata_hash"):
        raise LiveError("sign request calldata hash is invalid")


def _trusted_clients(timeout_seconds: int) -> tuple[JsonRpcClient, JsonRpcClient]:
    return JsonRpcClient(TRUSTED_XRPL_TESTNET_RPC_URL, timeout_seconds), JsonRpcClient(TRUSTED_COSTON2_RPC_URL, timeout_seconds)


def _validate_networks(xrpl: JsonRpcClient, flare: JsonRpcClient) -> None:
    info = _mapping(xrpl.request("server_info"), "info")
    if _integer(info.get("network_id")) != XRPL_TESTNET_NETWORK_ID or _quantity(flare.request("eth_chainId")) != COSTON2_CHAIN_ID:
        raise LiveError("trusted endpoint did not report XRPL Testnet and Coston2")


def _validate_flare_chain(flare: JsonRpcClient) -> None:
    if _quantity(flare.request("eth_chainId")) != COSTON2_CHAIN_ID:
        raise LiveError("trusted endpoint did not report Coston2")


def _current_validated_ledger(xrpl: JsonRpcClient) -> int:
    info = _mapping(xrpl.request("server_info"), "info")
    validated = _mapping(info, "validated_ledger")
    return _integer(validated.get("seq"))


def _validate_mined_transaction(transaction: Mapping[str, object], receipt: Mapping[str, object], transaction_hash: str, label: str) -> None:
    if _text(transaction, "hash").lower() != transaction_hash.lower() or _text(receipt, "transactionHash").lower() != transaction_hash.lower():
        raise LiveError(f"{label} transaction hash does not match the requested hash")
    transaction_block = _text(transaction, "blockNumber")
    receipt_block = _text(receipt, "blockNumber")
    if transaction_block != receipt_block:
        raise LiveError(f"{label} transaction and receipt block numbers do not match")
    tx_block_hash, receipt_block_hash = transaction.get("blockHash"), receipt.get("blockHash")
    if tx_block_hash is not None or receipt_block_hash is not None:
        if not isinstance(tx_block_hash, str) or not isinstance(receipt_block_hash, str) or tx_block_hash.lower() != receipt_block_hash.lower():
            raise LiveError(f"{label} transaction and receipt block hashes do not match")


def _require_live_contracts(client: JsonRpcClient, state: Mapping[str, object], keys: tuple[str, ...]) -> dict[str, str]:
    stored = _object(state.get("contracts"), "saved contracts")
    names = {"asset_manager": "AssetManagerFXRP", "fdc_hub": "FdcHub", "fdc_verification": "FdcVerification", "relay": "Relay", "flare_systems_manager": "FlareSystemsManager"}
    live_contracts = {key: _registry(client, names[key]) for key in keys}
    if any(live_contracts[key] != _address(str(stored.get(key, "")), f"saved {key}") for key in keys):
        raise LiveError("live contract registry no longer matches saved commitment")
    return live_contracts


def _resolve_contracts(client: JsonRpcClient) -> dict[str, str]:
    names = {"asset_manager": "AssetManagerFXRP", "master_account_controller": "MasterAccountController", "fdc_hub": "FdcHub", "fdc_verification": "FdcVerification", "relay": "Relay", "flare_systems_manager": "FlareSystemsManager"}
    return {key: _registry(client, name) for key, name in names.items()}


def _live_settings(client: JsonRpcClient, asset_manager: str) -> dict[str, object]:
    minimum = _eth_quantity_call(client, asset_manager, _selector("getDirectMintingMinimumFeeUBA()"))
    bips = _eth_quantity_call(client, asset_manager, _selector("getDirectMintingFeeBIPS()"))
    granularity = _eth_quantity_call(client, asset_manager, _selector("assetMintingGranularityUBA()"))
    raw = _hex_bytes(_eth_call(client, asset_manager, _selector("getSettings()")), "settings")
    if len(raw) < 32 + 20 * 32 or int.from_bytes(raw[:32], "big") != 32:
        raise LiveError("AssetManager settings ABI is malformed")
    decimals, lot_amg = int.from_bytes(raw[32 + 11 * 32:64 + 11 * 32], "big"), int.from_bytes(raw[32 + 19 * 32:64 + 19 * 32], "big")
    return {"minimum_fee_uba": minimum, "fee_bips": bips, "asset_minting_granularity_uba": granularity, "lot_size_amg": lot_amg, "lot_size_uba": lot_amg * granularity, "asset_decimals": decimals, "core_vault": _eth_string_call(client, asset_manager, _selector("directMintingPaymentAddress()"))}


def _registry(client: JsonRpcClient, name: str) -> str:
    return _eth_address_call(client, FLARE_CONTRACT_REGISTRY, _selector("getContractAddressByName(string)") + _abi_string(name)[2:])


def _eth_call(client: JsonRpcClient, target: str, data: str) -> str:
    return _eth_call_at(client, target, data, "latest")


def _eth_quantity_call(client: JsonRpcClient, target: str, data: str) -> int:
    return _quantity(_eth_call(client, target, data))


def _eth_address_call(client: JsonRpcClient, target: str, data: str) -> str:
    raw = _hex_bytes(_eth_call(client, target, data), "address return")
    if len(raw) != 32 or raw[:12] != b"\0" * 12:
        raise LiveError("contract address return is malformed")
    return _address("0x" + raw[12:].hex(), "contract address")


def _eth_string_call(client: JsonRpcClient, target: str, data: str) -> str:
    raw = _hex_bytes(_eth_call(client, target, data), "string return")
    if len(raw) < 64 or int.from_bytes(raw[:32], "big") != 32:
        raise LiveError("contract string return is malformed")
    length = int.from_bytes(raw[32:64], "big")
    if 64 + length > len(raw):
        raise LiveError("contract string return is malformed")
    try:
        return raw[64:64 + length].decode("ascii")
    except UnicodeDecodeError as error:
        raise LiveError("contract string is not ASCII") from error


def _encode_execute_user_op(sender: str, target: str, selector: str) -> bytes:
    call = _abi_address(target) + _word(0) + _word(96) + _abi_bytes(_hex_bytes(selector, "Checkpoint selector"))
    array = _word(1) + _word(32) + call
    return bytes.fromhex(_selector("executeUserOp((address,uint256,bytes)[])")[2:]) + _word(32) + array


def _encode_packed_user_op(sender: str, nonce: int, call_data: bytes) -> bytes:
    dynamic_values = (b"", call_data, b"", b"")
    dynamic_offsets: list[int] = []
    offset = 9 * 32
    for value in dynamic_values:
        dynamic_offsets.append(offset)
        offset += len(_abi_bytes(value))
    head = [
        _abi_address(sender), _word(nonce), _word(dynamic_offsets[0]), _word(dynamic_offsets[1]),
        _word(0), _word(0), _word(0), _word(dynamic_offsets[2]), _word(dynamic_offsets[3]),
    ]
    return _word(32) + b"".join(head) + b"".join(_abi_bytes(value) for value in dynamic_values)


def _selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


def _abi_string(value: str) -> str:
    return "0x" + (_word(32) + _abi_bytes(value.encode("ascii"))).hex()


def _abi_address(value: str) -> bytes:
    return b"\0" * 12 + bytes.fromhex(_address(value, "address")[2:])


def _abi_bytes(value: bytes) -> bytes:
    return _word(len(value)) + value + b"\0" * ((-len(value)) % 32)


def _abi_dynamic(value: bytes) -> bytes:
    return _word(32) + _abi_bytes(value)


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _net_minted(gross: int, bips: int, minimum: int, memo_fee: int) -> int:
    return gross - min(max(gross * bips // 10_000, minimum), gross) - memo_fee


def _dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        raise LiveError(".env is required")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _post_json(url: str, body: Mapping[str, object], api_key: str, api_secret: str) -> Mapping[str, object]:
    parsed = _https_url(url, "external URL")
    if parsed.netloc != "xumm.app":
        raise LiveError("Xaman request host is not pinned")
    headers = {"Content-Type": "application/json", **_xaman_headers_values(api_key, api_secret)}
    request = Request(url, json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            if urlparse(response.geturl()).netloc != parsed.netloc:
                raise LiveError("external request changed host")
            result = json.loads(response.read().decode())
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
        raise LiveError("external request failed") from error
    if not isinstance(result, Mapping):
        raise LiveError("external response is malformed")
    return result


def _xaman_headers(config: LiveConfig) -> dict[str, str]:
    return _xaman_headers_values(config.xaman_api_key, config.xaman_api_secret)


def _xaman_headers_values(api_key: str, api_secret: str) -> dict[str, str]:
    return {"X-API-Key": api_key, "X-API-Secret": api_secret, "User-Agent": XAMAN_USER_AGENT}


def _get_json(url: str, headers: Mapping[str, str], expected_host: str) -> Mapping[str, object]:
    return _json_request(url, None, headers, "GET", expected_host)


def _post_public_json(url: str, body: Mapping[str, object], headers: Mapping[str, str]) -> Mapping[str, object]:
    return _json_request(url, body, headers, "POST", urlparse(url).netloc)


def _json_request(url: str, body: Mapping[str, object] | None, headers: Mapping[str, str], method: str, expected_host: str) -> Mapping[str, object]:
    parsed = _https_url(url, "external URL")
    if parsed.netloc != expected_host:
        raise LiveError("external request host is not pinned")
    request_headers = {"Accept": "application/json", **headers}
    data = None if body is None else _canonical_json(body)
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            final = urlparse(response.geturl())
            if final.scheme != "https" or final.netloc != expected_host:
                raise LiveError("external request changed host")
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
        raise LiveError("external request failed") from error
    if not isinstance(result, Mapping):
        raise LiveError("external response is malformed")
    return result


def _verifier_endpoint(base: str) -> str:
    parsed = _https_url(base, "verifier URL")
    return parsed.geturl().rstrip("/") + "/verifier/xrp/XRPPayment/prepareRequest"


def _xrp_hash(value: str) -> str:
    raw = value[2:] if value.startswith("0x") else value
    if len(raw) != 64:
        raise LiveError("XRPL transaction hash is malformed")
    try:
        bytes.fromhex(raw)
    except ValueError as error:
        raise LiveError("XRPL transaction hash is malformed") from error
    return raw.upper()


def _xaman_uuid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", value):
        raise LiveError("Xaman UUID is malformed")
    return value.lower()


def _xaman_sign_url(value: str) -> str:
    return f"https://xumm.app/sign/{_xaman_uuid(value)}"


def _evm_hash(value: str) -> str:
    raw = _hex_bytes(value, "Coston2 transaction hash")
    if len(raw) != 32:
        raise LiveError("Coston2 transaction hash is malformed")
    return "0x" + raw.hex()


def _validate_signed_xrpl_payment(result: Mapping[str, object], state: Mapping[str, object]) -> None:
    if result.get("validated") is not True or result.get("TransactionType") != "Payment":
        raise LiveError("Xaman transaction is not a validated XRPL Payment")
    meta = _mapping(result, "meta")
    flags = result.get("Flags", 0)
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
        raise LiveError("XRPL payment flags are malformed")
    if meta.get("TransactionResult") != "tesSUCCESS" or flags & 0x00020000:
        raise LiveError("XRPL payment failed or permits partial delivery")
    settings = _object(state.get("settings"), "saved settings")
    if result.get("Account") != state.get("xrpl_address") or result.get("Destination") != settings.get("core_vault"):
        raise LiveError("XRPL payment account or destination does not match commitment")
    if result.get("Amount") != str(state.get("gross_drops")) or meta.get("delivered_amount") != str(state.get("gross_drops")):
        raise LiveError("XRPL payment amount does not match commitment")
    memos = result.get("Memos")
    if not isinstance(memos, list) or len(memos) != 1 or not isinstance(memos[0], Mapping):
        raise LiveError("XRPL payment memo is malformed")
    memo = _mapping(memos[0], "Memo")
    if memo.get("MemoData") != str(state.get("memo_data_hex")).upper() or "DestinationTag" in result:
        raise LiveError("XRPL payment memo or destination tag does not match commitment")


def _da_proof_endpoint(base: str) -> str:
    parsed = _https_url(base, "DA layer URL")
    return parsed.geturl().rstrip("/") + "/api/v1/fdc/proof-by-request-round-raw"


def _response_tuple_body(raw: bytes, state: Mapping[str, object]) -> bytes:
    if len(raw) < 32 + 7 * 32 or int.from_bytes(raw[:32], "big") != 32:
        raise LiveError("FDC response ABI outer tuple offset is malformed")
    body = raw[32:]
    if int.from_bytes(body[192:224], "big") != 224:
        raise LiveError("FDC response ABI response body offset is malformed")
    if body[:32] != b"XRPPayment".ljust(32, b"\0") or body[32:64] != b"testXRP".ljust(32, b"\0"):
        raise LiveError("FDC response does not bind XRPPayment on testXRP")
    if int.from_bytes(body[64:96], "big") != int(_text(state, "fdc_round_id")):
        raise LiveError("FDC response voting round does not match recorded request")
    if body[128:160] != bytes.fromhex(_text(state, "xrpl_transaction_hash")):
        raise LiveError("FDC response has a different XRPL transaction")
    if body[160:172] != b"\0" * 12 or "0x" + body[172:192].hex() != _address(str(state["signer"]), "saved signer"):
        raise LiveError("FDC response proof owner does not match signer")
    _validate_response_body_offsets(body)
    return body


def _validate_fdc_request(raw: bytes, transaction_hash: str, proof_owner: str) -> None:
    if len(raw) != 5 * 32:
        raise LiveError("FDC request ABI must contain exactly five words")
    if raw[:32] != b"XRPPayment".ljust(32, b"\0") or raw[32:64] != b"testXRP".ljust(32, b"\0"):
        raise LiveError("FDC request does not bind XRPPayment on testXRP")
    if raw[96:128] != bytes.fromhex(_xrp_hash(transaction_hash)):
        raise LiveError("FDC request has a different XRPL transaction")
    owner_word = raw[128:160]
    if owner_word[:12] != b"\0" * 12 or "0x" + owner_word[12:].hex() != _address(proof_owner, "proof owner"):
        raise LiveError("FDC request proof owner does not match signer")


def _validate_response_body_offsets(response: bytes) -> None:
    body_start = int.from_bytes(response[192:224], "big")
    if body_start + 15 * 32 > len(response):
        raise LiveError("FDC response body is truncated")
    for offset_index in (2, 11):
        offset = int.from_bytes(response[body_start + offset_index * 32:body_start + (offset_index + 1) * 32], "big")
        if offset % 32 or offset < 15 * 32 or body_start + offset + 32 > len(response):
            raise LiveError("FDC response dynamic offset is malformed")
        length = int.from_bytes(response[body_start + offset:body_start + offset + 32], "big")
        if body_start + offset + 32 + length > len(response):
            raise LiveError("FDC response dynamic value is truncated")


def _merkle_array(value: object) -> bytes:
    if not isinstance(value, list):
        raise LiveError("FDC proof array is malformed")
    items: list[bytes] = []
    for item in value:
        if not isinstance(item, str):
            raise LiveError("FDC proof item is malformed")
        raw = _hex_bytes(item, "FDC proof item")
        if len(raw) != 32:
            raise LiveError("FDC proof item is not bytes32")
        items.append(raw)
    return _word(len(items)) + b"".join(items)


def _candidate_evidence(
    state: Mapping[str, object], xrpl: JsonRpcClient, flare: JsonRpcClient,
    transaction: Mapping[str, object], receipt: Mapping[str, object],
) -> dict[str, object]:
    xrpl_tx = _object(xrpl.request("tx", [{"transaction": _text(state, "xrpl_transaction_hash"), "binary": False}]), "XRPL transaction")
    block_number = _text(receipt, "blockNumber")
    block = _object(flare.request("eth_getBlockByNumber", [block_number, False]), "execution block")
    target = _address(_text(_mapping(state, "execute_sign_request"), "to"), "execution target")
    delivered = _decimal_text(_mapping(xrpl_tx, "meta").get("delivered_amount"), "XRPL delivered amount")
    fees = _fees_at(flare, target, block_number, delivered, _canonical_fsa_memo_bytes(_text(state, "memo_data_hex"))[2:10])
    return {
        "schema_version": 1, "status": "completed",
        "protocol": _protocol_section(state),
        "xrpl": _xrpl_section(state, xrpl_tx, delivered),
        "flare": _flare_section(state, transaction, block, target, block_number),
        "fees": fees,
    }


def _protocol_section(state: Mapping[str, object]) -> dict[str, str]:
    payload = _text(state, "packed_user_operation_hex")
    instruction = build_custom_instruction(0, 0, bytes.fromhex(payload))
    return {"packed_user_operation_hex": payload, "user_op_hash_hex": instruction.user_op_hash.hex(), "memo_data_hex": _text(state, "memo_data_hex"), "source_url": "https://dev.flare.network/smart-accounts/custom-instruction"}


def _xrpl_section(state: Mapping[str, object], transaction: Mapping[str, object], delivered: str) -> dict[str, object]:
    return {"rpc_url": TRUSTED_XRPL_TESTNET_RPC_URL, "transaction_hash": _text(state, "xrpl_transaction_hash"), "source_account": _text(state, "xrpl_address"), "core_vault_destination": str(_object(state["settings"], "saved settings")["core_vault"]), "amount_drops": str(state["gross_drops"]), "delivered_amount_drops": delivered, "validated_ledger_index": _integer(transaction.get("ledger_index")), "timestamp": _timestamp(_integer(transaction.get("date")) + 946684800), "source_url": "https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/tx"}


def _flare_section(state: Mapping[str, object], transaction: Mapping[str, object], block: Mapping[str, object], target: str, block_number: str) -> dict[str, str]:
    return {"rpc_url": TRUSTED_COSTON2_RPC_URL, "chain_id": "0x72", "transaction_hash": _text(transaction, "hash"), "personal_account": _text(state, "personal_account"), "call_target": target, "block_number": block_number, "block_hash": _text(block, "hash"), "timestamp": _timestamp(_quantity(block.get("timestamp"))), "source_url": "https://dev.flare.network/fassets/reference/IAssetManager"}


def _fees_at(client: JsonRpcClient, target: str, block: str, delivered: str, memo_fee: bytes) -> dict[str, str]:
    minimum = _eth_quantity_at(client, target, "getDirectMintingMinimumFeeUBA()", block)
    bips = _eth_quantity_at(client, target, "getDirectMintingFeeBIPS()", block)
    standard = _eth_quantity_at(client, target, "getDirectMintingExecutorFeeUBA()", block)
    granularity = _eth_quantity_at(client, target, "assetMintingGranularityUBA()", block)
    redeem = _eth_quantity_at(client, target, "minimumRedeemAmountUBA()", block)
    raw = _hex_bytes(_eth_call_at(client, target, _selector("getSettings()"), block), "settings")
    if len(raw) < 32 + 20 * 32 or int.from_bytes(raw[:32], "big") != 32:
        raise LiveError("AssetManager settings ABI is malformed")
    decimals, lot_amg = int.from_bytes(raw[32 + 11 * 32:64 + 11 * 32], "big"), int.from_bytes(raw[32 + 19 * 32:64 + 19 * 32], "big")
    amount, memo = int(delivered), int.from_bytes(memo_fee, "big")
    minting = min(max(amount * bips // 10_000, minimum), amount)
    lot_uba = lot_amg * granularity
    return {"minimum_fee_uba": str(minimum), "fee_bips": str(bips), "memo_executor_fee_uba": str(memo), "standard_direct_mint_executor_fee_uba": str(standard), "asset_minting_granularity_uba": str(granularity), "lot_size_amg": str(lot_amg), "lot_size_uba": str(lot_uba), "minting_fee_uba": str(minting), "net_minted_uba": str(amount - minting - memo), "asset_decimals": str(decimals), "lot_size_xrp": _format_units(lot_uba, decimals), "minimum_redeem_amount_uba": str(redeem), "minimum_redeem_amount_xrp": _format_units(redeem, decimals), "query_block": block, "source_url": "https://dev.flare.network/fassets/developer-guides/fassets-settings-node"}


def _eth_call_at(client: JsonRpcClient, target: str, data: str, block: str) -> str:
    result = client.request("eth_call", [{"to": target, "data": _eth_calldata(data)}, block])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise LiveError("eth_call result is malformed")
    return result


def _eth_calldata(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        raise LiveError("eth_call calldata is malformed")
    try:
        bytes.fromhex(value[2:])
    except ValueError as error:
        raise LiveError("eth_call calldata is malformed") from error
    return value


def _eth_quantity_at(client: JsonRpcClient, target: str, signature: str, block: str) -> int:
    return _quantity(_eth_call_at(client, target, _selector(signature), block))


def _decimal_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.isdecimal():
        raise LiveError(f"{label} is malformed")
    return value


def _timestamp(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_units(value: int, decimals: int) -> str:
    whole, fraction = divmod(value, 10 ** decimals)
    return str(whole) if fraction == 0 else f"{whole}.{fraction:0{decimals}d}".rstrip("0")


def _safe_xaman_links(response: Mapping[str, object]) -> dict[str, str]:
    next_step = response.get("next")
    if not isinstance(next_step, Mapping):
        return {}
    return {key: value for key, value in next_step.items() if key in {"always", "no_push_msg_received", "qr_png"} and isinstance(value, str)}


def _commitment(state: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(state.get(key) for key in ("personal_account", "nonce", "packed_user_operation_hex", "memo_data_hex", "gross_drops", "settings", "contracts"))


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _https_url(value: str, label: str) -> ParseResult:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise LiveError(f"{label} is not an absolute HTTPS URL")
    return parsed


def _official_base_url(value: str, host: str, label: str) -> str:
    parsed = _https_url(value, label)
    if parsed.netloc != host or parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise LiveError(f"{label} must be the official Testnet endpoint")
    return f"https://{host}"


def _sign_request_object(value: Mapping[str, str]) -> None:
    fields = {"version", "purpose", "chain_id", "signer", "to", "value", "data", "calldata_hash"}
    if set(value) != fields or value.get("version") != "1" or value.get("chain_id") != "0x72":
        raise LiveError("sign request schema is malformed")
    if value.get("purpose") not in SIGN_REQUEST_PURPOSES:
        raise LiveError("sign request purpose is malformed")
    _address(value.get("signer", ""), "sign request signer")
    _address(value.get("to", ""), "sign request target")
    _quantity(value.get("value"))
    data = _hex_bytes(value.get("data", ""), "sign request calldata")
    expected_hash = "0x" + keccak256(data).hex()
    if value.get("calldata_hash") != expected_hash:
        raise LiveError("sign request calldata hash is malformed")


def _state_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION or value.get("stage") not in STAGES:
        raise LiveError("run state is malformed")
    return value


def _reject_secret_state(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in _SECRET_MARKERS):
                raise LiveError("state must not contain secrets")
            _reject_secret_state(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_state(item)


def _refuse_symlink(path: Path) -> None:
    if path.is_symlink():
        raise LiveError("symlink paths are refused")


def _address(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        raise LiveError(f"{label} is not an EVM address")
    try:
        bytes.fromhex(value[2:])
    except ValueError as error:
        raise LiveError(f"{label} is not an EVM address") from error
    return value.lower()


def _hex_bytes(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise LiveError(f"{label} is not hex")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as error:
        raise LiveError(f"{label} is not hex") from error


def _canonical_fsa_memo_bytes(value: str) -> bytes:
    """Decode the uppercase, bare 42-byte FSA memo schema used in state/evidence."""
    if not isinstance(value, str) or len(value) != 84 or not re.fullmatch(r"[0-9A-F]{84}", value):
        raise LiveError("canonical FSA memo is malformed")
    return bytes.fromhex(value)


def _quantity(value: object) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise LiveError("RPC quantity is malformed")
    try:
        return int(value, 16)
    except ValueError as error:
        raise LiveError("RPC quantity is malformed") from error


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LiveError("integer is malformed")
    return value


def _mapping(value: object, key: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not isinstance(value.get(key), Mapping):
        raise LiveError("RPC object is malformed")
    return value[key]


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LiveError(f"{label} is malformed")
    return value


def _text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise LiveError("external response is malformed")
    return result


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LiveError(f"{label} must be non-negative")


def _xrpl_classic_address(value: str) -> str:
    alphabet = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
    if not isinstance(value, str) or not 25 <= len(value) <= 35 or not value.startswith("r"):
        raise LiveError("XRPL Testnet address is not a classic address")
    number = 0
    try:
        for char in value:
            number = number * 58 + alphabet.index(char)
    except ValueError as error:
        raise LiveError("XRPL Testnet address is not a classic address") from error
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    raw = b"\0" * (25 - len(raw)) + raw
    if len(raw) != 25 or raw[0] != 0 or raw[-4:] != sha256(sha256(raw[:-4]).digest()).digest()[:4]:
        raise LiveError("XRPL Testnet address checksum is invalid")
    return value
