"""Bounded JSON-RPC validation for real XRPL and Coston2 evidence only."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

import httpx

from .instructions import InstructionError, inspect_custom_instruction, keccak256


JsonObject = Mapping[str, object]
Transport = Callable[[str, dict[str, object], int], JsonObject]
COSTON2_CHAIN_ID = 114
XRPL_TESTNET_NETWORK_ID = 1
TRUSTED_XRPL_TESTNET_RPC_URL = "https://s.altnet.rippletest.net:51234/"
TRUSTED_COSTON2_RPC_URL = "https://coston2-api.flare.network/ext/C/rpc"
FLARE_CONTRACT_REGISTRY = "0xaD67FE66660Fb8dFE9d6b1b4240d8650e30F6019"
_REGISTRY_NAME = "AssetManagerFXRP"
_MASTER_ACCOUNT_CONTROLLER_NAME = "MasterAccountController"
_PROOF_TUPLE = (
    "(bytes32[],(bytes32,bytes32,uint64,uint64,(bytes32,address),"
    "(uint64,uint64,string,bytes32,bytes32,bytes32,int256,int256,int256,int256,"
    "bool,bytes,bool,uint256,uint8)))"
)


class RpcEvidenceError(ValueError):
    """Raised when evidence is incomplete, inconsistent, or cannot be proven by RPC."""


def _selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


EXECUTE_DIRECT_MINTING_WITH_DATA_SELECTOR = _selector(
    f"executeDirectMintingWithData({_PROOF_TUPLE},bytes)"
)
_REGISTRY_SELECTOR = _selector("getContractAddressByName(string)")
_SETTINGS_SELECTOR = _selector("getSettings()")
_FEE_GETTERS = {
    "minimum_fee_uba": _selector("getDirectMintingMinimumFeeUBA()"),
    "fee_bips": _selector("getDirectMintingFeeBIPS()"),
    "standard_direct_mint_executor_fee_uba": _selector("getDirectMintingExecutorFeeUBA()"),
    "minimum_redeem_amount_uba": _selector("minimumRedeemAmountUBA()"),
    "asset_minting_granularity_uba": _selector("assetMintingGranularityUBA()"),
}
_DIRECT_MINTING_PAYMENT_ADDRESS_SELECTOR = _selector("directMintingPaymentAddress()")
_USER_OPERATION_EXECUTED_TOPIC = "0x" + keccak256(
    b"UserOperationExecuted(address,uint256)"
).hex()
_HTTP_CLIENT = httpx.Client(follow_redirects=False)


class JsonRpcClient:
    """Small strict JSON-RPC client with a replaceable transport for unit tests."""

    def __init__(self, url: str, timeout_seconds: int, transport: Transport | None = None) -> None:
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise RpcEvidenceError("timeout must be a positive integer")
        self.url = _require_url(url, "RPC URL")
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _http_transport
        self._request_id = 0

    def request(self, method: str, params: list[object] | None = None) -> object:
        self._request_id += 1
        request_id = self._request_id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or []}
        for attempt in range(2):
            try:
                response = self.transport(self.url, request, self.timeout_seconds)
                break
            except HTTPError as error:
                if 300 <= error.code < 400:
                    raise RpcEvidenceError("RPC redirect is not allowed") from error
                raise RpcEvidenceError("RPC endpoint is unreachable") from error
            except (TimeoutError, URLError, OSError) as error:
                if attempt == 1:
                    raise RpcEvidenceError("RPC endpoint is unreachable") from error
        return _validated_result(response, request_id, self.url == TRUSTED_XRPL_TESTNET_RPC_URL)


def validate_evidence(evidence: JsonObject, timeout_seconds: int, transport: Transport | None = None) -> None:
    """Validate schema and live RPC facts; pending records are always rejected."""
    _require_completed_schema(evidence)
    protocol, xrpl, flare, fees = _evidence_sections(evidence)
    _require_trusted_rpc_record(xrpl, TRUSTED_XRPL_TESTNET_RPC_URL)
    _require_trusted_rpc_record(flare, TRUSTED_COSTON2_RPC_URL)
    payload = _decode_hex(protocol, "packed_user_operation_hex", non_empty=True)
    memo = _decode_hex(protocol, "memo_data_hex", non_empty=True)
    _validate_instruction_binding(protocol, memo, payload)
    nonce = _validate_personal_account(flare, payload)
    _validate_fee_binding(fees, memo)
    delivered_amount = _validate_xrpl(
        JsonRpcClient(TRUSTED_XRPL_TESTNET_RPC_URL, timeout_seconds, transport), xrpl, memo
    )
    flare_client = JsonRpcClient(TRUSTED_COSTON2_RPC_URL, timeout_seconds, transport)
    call_target, block = _validate_flare(flare_client, flare, xrpl, payload, nonce)
    _validate_live_parameters(flare_client, fees, xrpl, delivered_amount, call_target, block)


def _require_completed_schema(evidence: JsonObject) -> None:
    if not isinstance(evidence, Mapping) or evidence.get("schema_version") != 1:
        raise RpcEvidenceError("unsupported or missing evidence schema")
    if evidence.get("status") != "completed":
        raise RpcEvidenceError("evidence is pending or not authorized for live validation")


def _evidence_sections(evidence: JsonObject) -> tuple[JsonObject, JsonObject, JsonObject, JsonObject]:
    return (
        _mapping(evidence, "protocol"),
        _mapping(evidence, "xrpl"),
        _mapping(evidence, "flare"),
        _mapping(evidence, "fees"),
    )


def _require_trusted_rpc_record(section: JsonObject, trusted_url: str) -> None:
    if _text(section, "rpc_url") != trusted_url:
        raise RpcEvidenceError("evidence RPC URL does not equal the trusted RPC endpoint")


def _validate_instruction_binding(protocol: JsonObject, memo: bytes, payload: bytes) -> None:
    expected_hash = _decode_hex(protocol, "user_op_hash_hex", non_empty=True)
    try:
        instruction = inspect_custom_instruction(memo, payload)
    except InstructionError as error:
        raise RpcEvidenceError("invalid 0xFE memo commitment") from error
    if instruction.user_op_hash != expected_hash:
        raise RpcEvidenceError("UserOp hash does not match memo commitment")
    _require_url(_text(protocol, "source_url"), "protocol source URL")


def _validate_personal_account(flare: JsonObject, payload: bytes) -> int:
    sender, nonce = _packed_user_operation_identity(payload)
    if _address(flare, "personal_account") != sender:
        raise RpcEvidenceError("personal account does not match the PackedUserOperation sender")
    return nonce


def _validate_fee_binding(fees: JsonObject, memo: bytes) -> None:
    if _decimal(fees, "memo_executor_fee_uba") != int.from_bytes(memo[2:10], "big"):
        raise RpcEvidenceError("executor fee does not match memo")
    _require_url(_text(fees, "source_url"), "fee source URL")


def _validate_xrpl(client: JsonRpcClient, xrpl: JsonObject, memo: bytes) -> int:
    server_info = _mapping_result(client.request("server_info"), "XRPL server_info")
    if _integer(_mapping(server_info, "info"), "network_id") != XRPL_TESTNET_NETWORK_ID:
        raise RpcEvidenceError("XRPL RPC endpoint is not Testnet")
    tx_hash = _xrpl_transaction_hash(xrpl, "transaction_hash")
    result = _mapping_result(client.request("tx", [{"transaction": tx_hash, "binary": False}]), "XRPL")
    result_hash = _raw_hex(_text(result, "hash"), "XRPL result hash")
    if result.get("validated") is not True or result_hash != _raw_hex(tx_hash, "XRPL transaction hash"):
        raise RpcEvidenceError("XRPL transaction is not validated or does not match")
    delivered_amount = _validate_xrpl_payment(result, xrpl, memo)
    if result.get("ledger_index") != _integer(xrpl, "validated_ledger_index"):
        raise RpcEvidenceError("XRPL validated ledger does not match")
    _require_bound_timestamp(xrpl, _integer(result, "date") + 946684800)
    _require_url(_text(xrpl, "source_url"), "XRPL source URL")
    return delivered_amount


def _validate_xrpl_payment(result: JsonObject, xrpl: JsonObject, memo: bytes) -> int:
    metadata = _mapping(result, "meta")
    if result.get("TransactionType") != "Payment" or metadata.get("TransactionResult") != "tesSUCCESS":
        raise RpcEvidenceError("XRPL transaction is not a successful Payment")
    flags = result.get("Flags", 0)
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
        raise RpcEvidenceError("XRPL Payment flags are malformed")
    if flags & 0x00020000:
        raise RpcEvidenceError("XRPL partial payments are not accepted")
    requested_amount = _native_xrp_drops(result, "Amount")
    delivered_amount = _native_xrp_drops(metadata, "delivered_amount")
    if requested_amount != delivered_amount:
        raise RpcEvidenceError("XRPL delivered amount does not equal the requested amount")
    if delivered_amount != _decimal(xrpl, "delivered_amount_drops"):
        raise RpcEvidenceError("XRPL delivered amount does not bind the evidence")
    expected = {
        "Account": _text(xrpl, "source_account"),
        "Destination": _text(xrpl, "core_vault_destination"),
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise RpcEvidenceError("XRPL Payment fields do not bind the evidence")
    if requested_amount != _decimal(xrpl, "amount_drops"):
        raise RpcEvidenceError("XRPL requested amount does not bind the evidence")
    if "DestinationTag" in result or _memo_data(result) != memo.hex().upper():
        raise RpcEvidenceError("XRPL Payment memo or destination tag is invalid")
    return delivered_amount


def _validate_flare(
    client: JsonRpcClient,
    flare: JsonObject,
    xrpl: JsonObject,
    payload: bytes,
    nonce: int,
) -> tuple[str, str]:
    if _hex_quantity(client.request("eth_chainId")) != COSTON2_CHAIN_ID:
        raise RpcEvidenceError("RPC endpoint is not Coston2")
    if _hex_quantity(_text(flare, "chain_id")) != COSTON2_CHAIN_ID:
        raise RpcEvidenceError("evidence does not bind Coston2 chain ID")
    tx_hash = _hex_hash(flare, "transaction_hash", 32)
    transaction = _mapping_result(client.request("eth_getTransactionByHash", [tx_hash]), "Flare transaction")
    if _text(transaction, "hash").lower() != tx_hash.lower():
        raise RpcEvidenceError("Flare transaction hash does not match evidence")
    target = _address(flare, "call_target")
    if _address(transaction, "to") != target:
        raise RpcEvidenceError("Flare call target does not match evidence")
    _validate_direct_mint_call(_text(transaction, "input"), xrpl, payload)
    receipt = _mapping_result(client.request("eth_getTransactionReceipt", [tx_hash]), "Flare receipt")
    if _text(receipt, "transactionHash").lower() != tx_hash.lower() or receipt.get("status") != "0x1":
        raise RpcEvidenceError("Flare transaction did not succeed")
    block = _validate_execution_block(client, flare, transaction, receipt)
    _validate_user_operation_executed(client, receipt, block, _address(flare, "personal_account"), nonce)
    _require_url(_text(flare, "source_url"), "Flare source URL")
    return target, block


def _validate_direct_mint_call(input_data: str, xrpl: JsonObject, payload: bytes) -> None:
    transaction_id, calldata_payload = _decode_direct_mint_calldata(input_data)
    if transaction_id.lower() != _hex_hash(xrpl, "transaction_hash", 32).lower():
        raise RpcEvidenceError("Flare proof has a different XRPL transaction")
    if calldata_payload != payload:
        raise RpcEvidenceError("Flare call has a different UserOp payload")


def _validate_execution_block(
    client: JsonRpcClient,
    flare: JsonObject,
    transaction: JsonObject,
    receipt: JsonObject,
) -> str:
    block = _text(flare, "block_number")
    block_hash = _hex_hash(flare, "block_hash", 32).lower()
    _hex_quantity(block)
    if _text(transaction, "blockNumber") != block or _text(receipt, "blockNumber") != block:
        raise RpcEvidenceError("Flare execution block does not match")
    if _text(transaction, "blockHash").lower() != block_hash or _text(receipt, "blockHash").lower() != block_hash:
        raise RpcEvidenceError("Flare execution block hash does not match")
    block_result = _mapping_result(client.request("eth_getBlockByNumber", [block, False]), "Flare block")
    if _hex_hash(block_result, "hash", 32).lower() != block_hash:
        raise RpcEvidenceError("Flare block RPC hash does not match evidence")
    _require_bound_timestamp(flare, _hex_quantity(_text(block_result, "timestamp")))
    return block


def _validate_live_parameters(
    client: JsonRpcClient,
    fees: JsonObject,
    xrpl: JsonObject,
    delivered_amount: int,
    target: str,
    block: str,
) -> None:
    if _text(fees, "query_block") != block:
        raise RpcEvidenceError("fee query block does not equal the execution block")
    if _registry_contract(client, _REGISTRY_NAME, block) != target:
        raise RpcEvidenceError("AssetManager registry identity does not match the call target")
    if _eth_call_string(client, target, _DIRECT_MINTING_PAYMENT_ADDRESS_SELECTOR, block) != _text(xrpl, "core_vault_destination"):
        raise RpcEvidenceError("live core vault does not match the XRPL destination")
    live_fees = {
        field: _eth_call_quantity(client, target, selector, block)
        for field, selector in _FEE_GETTERS.items()
    }
    for field in _FEE_GETTERS:
        if live_fees[field] != _decimal(fees, field):
            raise RpcEvidenceError(f"live {field} does not match evidence")
    settings = client.request("eth_call", [{"to": target, "data": _SETTINGS_SELECTOR}, block])
    lot_size, decimals = _settings_lot_values(settings)
    if lot_size != _decimal(fees, "lot_size_amg") or decimals != _decimal(fees, "asset_decimals"):
        raise RpcEvidenceError("live getSettings lot values do not match evidence")
    _validate_derived_xrp_values(fees, decimals)
    _validate_direct_mint_amount(delivered_amount, fees, lot_size, live_fees)


def _registry_contract(client: JsonRpcClient, name: str, block: str) -> str:
    data = _REGISTRY_SELECTOR + _abi_dynamic_string(name).hex()
    result = client.request("eth_call", [{"to": FLARE_CONTRACT_REGISTRY, "data": data}, block])
    return _abi_return_address(result)


def _validate_derived_xrp_values(fees: JsonObject, decimals: int) -> None:
    lot_size_uba = _decimal(fees, "lot_size_uba")
    if _text(fees, "lot_size_xrp") != _format_units(lot_size_uba, decimals):
        raise RpcEvidenceError("lot size XRP amount does not match live values")
    minimum = _decimal(fees, "minimum_redeem_amount_uba")
    if _text(fees, "minimum_redeem_amount_xrp") != _format_units(minimum, decimals):
        raise RpcEvidenceError("minimum redeem XRP amount does not match live values")


def _validate_direct_mint_amount(
    delivered_amount: int,
    fees: JsonObject,
    lot_size_amg: int,
    live_fees: Mapping[str, int],
) -> None:
    lot_size_uba = lot_size_amg * live_fees["asset_minting_granularity_uba"]
    if lot_size_uba != _decimal(fees, "lot_size_uba"):
        raise RpcEvidenceError("lot size UBA does not match lot size AMG and granularity")
    relative_fee_uba = (delivered_amount * live_fees["fee_bips"]) // 10_000
    fee_with_minimum_uba = max(live_fees["minimum_fee_uba"], relative_fee_uba)
    minting_fee_uba = min(fee_with_minimum_uba, delivered_amount)
    if minting_fee_uba != _decimal(fees, "minting_fee_uba"):
        raise RpcEvidenceError("minting fee does not match direct-mint rounding")
    net_minted_uba = delivered_amount - minting_fee_uba - _decimal(fees, "memo_executor_fee_uba")
    if net_minted_uba < 0 or net_minted_uba != _decimal(fees, "net_minted_uba"):
        raise RpcEvidenceError("net minted amount does not match smart-account fee handling")
    if net_minted_uba < lot_size_uba:
        raise RpcEvidenceError("net minted amount is below one live lot")


def _decode_direct_mint_calldata(input_data: str) -> tuple[str, bytes]:
    raw = _raw_hex(input_data, "Flare input")
    if not raw.startswith(bytes.fromhex(EXECUTE_DIRECT_MINTING_WITH_DATA_SELECTOR[2:])):
        raise RpcEvidenceError("Flare call selector is not executeDirectMintingWithData")
    arguments = raw[4:]
    proof_offset, payload_offset = _word_at(arguments, 0), _word_at(arguments, 32)
    proof_start = _offset(arguments, proof_offset, 64, "proof")
    response_start = proof_start + _offset(arguments, _word_at(arguments, proof_start + 32), 64, "proof response")
    transaction_id = "0x" + arguments[response_start + 128 : response_start + 160].hex()
    if len(transaction_id) != 66:
        raise RpcEvidenceError("Flare proof is too short")
    return transaction_id, _abi_bytes_at(arguments, payload_offset, "UserOp payload")


def _packed_user_operation_identity(payload: bytes) -> tuple[str, int]:
    tuple_start = _word_at(payload, 0)
    if tuple_start != 32 or len(payload) < tuple_start + 288:
        raise RpcEvidenceError("PackedUserOperation ABI encoding is malformed")
    sender_word = payload[tuple_start : tuple_start + 32]
    if sender_word[:12] != b"\x00" * 12:
        raise RpcEvidenceError("PackedUserOperation sender is malformed")
    for field_offset in (64, 96, 224, 256):
        _abi_bytes_at(payload, tuple_start + _word_at(payload, tuple_start + field_offset), "PackedUserOperation")
    return "0x" + sender_word[12:].hex(), _word_at(payload, tuple_start + 32)


def _validate_user_operation_executed(
    client: JsonRpcClient,
    receipt: JsonObject,
    block: str,
    personal_account: str,
    nonce: int,
) -> None:
    controller = _registry_contract(client, _MASTER_ACCOUNT_CONTROLLER_NAME, block)
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise RpcEvidenceError("Flare receipt lacks UserOperationExecuted logs")
    expected_account_topic = "0x" + "00" * 12 + personal_account[2:]
    for log in logs:
        if isinstance(log, Mapping) and _event_matches(log, controller, expected_account_topic, nonce):
            return
    raise RpcEvidenceError("Flare receipt lacks matching UserOperationExecuted event")


def _event_matches(log: Mapping[str, object], controller: str, account_topic: str, nonce: int) -> bool:
    address = log.get("address")
    topics = log.get("topics")
    data = log.get("data")
    if not isinstance(address, str) or address.lower() != controller:
        return False
    if not isinstance(topics, list) or len(topics) != 2 or any(not isinstance(topic, str) for topic in topics):
        return False
    if topics[0].lower() != _USER_OPERATION_EXECUTED_TOPIC or topics[1].lower() != account_topic:
        return False
    try:
        raw_data = _raw_hex(data, "UserOperationExecuted data") if isinstance(data, str) else b""
    except RpcEvidenceError:
        return False
    return len(raw_data) == 32 and int.from_bytes(raw_data, "big") == nonce


def _settings_lot_values(value: object) -> tuple[int, int]:
    raw = _raw_hex_quantity_bytes(value, "getSettings result")
    start = _word_at(raw, 0)
    if start != 32 or len(raw) < start + 20 * 32:
        raise RpcEvidenceError("getSettings result is too short for the official struct layout")
    decimals, lot_size = _word_at(raw, start + 11 * 32), _word_at(raw, start + 19 * 32)
    if decimals > 0xFF or lot_size > 0xFFFFFFFFFFFFFFFF:
        raise RpcEvidenceError("getSettings assetDecimals or lotSizeAMG is malformed")
    return lot_size, decimals


def _eth_call_quantity(client: JsonRpcClient, target: str, data: str, block: str) -> int:
    return _hex_quantity(client.request("eth_call", [{"to": target, "data": data}, block]))


def _eth_call_string(client: JsonRpcClient, target: str, data: str, block: str) -> str:
    result = client.request("eth_call", [{"to": target, "data": data}, block])
    return _abi_return_string(result)


def _http_transport(url: str, request: dict[str, object], timeout: int) -> JsonObject:
    try:
        response = _HTTP_CLIENT.post(url, json=request, timeout=timeout)
    except httpx.TimeoutException as error:
        raise TimeoutError("RPC request timed out") from error
    except httpx.TransportError as error:
        raise URLError(str(error)) from error
    if not 200 <= response.status_code < 300:
        raise HTTPError(
            url, response.status_code, response.reason_phrase,
            dict(response.headers), None,
        )
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise RpcEvidenceError("RPC response is not a JSON object")
    return payload


def _validated_result(response: JsonObject, request_id: int, allow_xrpl_legacy: bool = False) -> object:
    if allow_xrpl_legacy and _is_xrpl_legacy_success(response, request_id):
        return response["result"]
    if not isinstance(response, Mapping) or response.get("jsonrpc") != "2.0":
        raise RpcEvidenceError("invalid JSON-RPC response")
    if response.get("id") != request_id:
        raise RpcEvidenceError("JSON-RPC response id does not match request")
    if "error" in response:
        raise RpcEvidenceError("JSON-RPC response contains an error")
    if "result" not in response:
        raise RpcEvidenceError("JSON-RPC response lacks a result")
    return response["result"]


def _is_xrpl_legacy_success(response: object, request_id: int) -> bool:
    if not isinstance(response, Mapping) or "error" in response or "jsonrpc" in response:
        return False
    if "id" in response and response["id"] != request_id:
        raise RpcEvidenceError("JSON-RPC response id does not match request")
    result = response.get("result")
    return isinstance(result, Mapping) and result.get("status") == "success"


def _mapping(parent: JsonObject, key: str) -> JsonObject:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise RpcEvidenceError(f"missing object: {key}")
    return value


def _mapping_result(value: object, name: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise RpcEvidenceError(f"{name} result is absent or malformed")
    return value


def _text(parent: JsonObject, key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise RpcEvidenceError(f"missing text: {key}")
    return value


def _address(parent: JsonObject, key: str) -> str:
    raw = _text(parent, key)
    if not raw.startswith("0x") or len(raw) != 42:
        raise RpcEvidenceError(f"{key} must be a 20-byte address")
    try:
        bytes.fromhex(raw[2:])
    except ValueError as error:
        raise RpcEvidenceError(f"{key} must be a 20-byte address") from error
    return raw.lower()


def _decode_hex(parent: JsonObject, key: str, non_empty: bool) -> bytes:
    decoded = _raw_hex(_text(parent, key), key)
    if non_empty and not decoded:
        raise RpcEvidenceError(f"empty hex: {key}")
    return decoded


def _raw_hex(value: str, label: str) -> bytes:
    raw = value[2:] if value.startswith("0x") else value
    try:
        return bytes.fromhex(raw)
    except ValueError as error:
        raise RpcEvidenceError(f"invalid hex: {label}") from error


def _hex_hash(parent: JsonObject, key: str, byte_length: int) -> str:
    raw = _text(parent, key)
    if len(_raw_hex(raw, key)) != byte_length:
        raise RpcEvidenceError(f"{key} must be {byte_length} bytes")
    return raw if raw.startswith("0x") else "0x" + raw


def _xrpl_transaction_hash(parent: JsonObject, key: str) -> str:
    """Return canonical evidence-schema XRPL hashes for the XRPL tx API."""
    raw = _text(parent, key)
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", raw):
        raise RpcEvidenceError(f"{key} must be a canonical bare XRPL hash")
    return raw.upper()


def _decimal(parent: JsonObject, key: str) -> int:
    value = _text(parent, key)
    if not value.isdecimal():
        raise RpcEvidenceError(f"{key} must be an unsigned decimal integer")
    return int(value)


def _native_xrp_drops(parent: JsonObject, key: str) -> int:
    value = parent.get(key)
    if not isinstance(value, str) or not value.isdecimal():
        raise RpcEvidenceError(f"{key} must be native XRP drops")
    return int(value)


def _integer(parent: JsonObject, key: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RpcEvidenceError(f"{key} must be a non-negative integer")
    return value


def _hex_quantity(value: object) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise RpcEvidenceError("RPC quantity is malformed")
    try:
        return int(value, 16)
    except ValueError as error:
        raise RpcEvidenceError("RPC quantity is malformed") from error


def _raw_hex_quantity_bytes(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise RpcEvidenceError(f"{label} is malformed")
    return _raw_hex(value, label)


def _word_at(value: bytes, offset: int) -> int:
    if offset < 0 or offset + 32 > len(value):
        raise RpcEvidenceError("ABI data is too short")
    return int.from_bytes(value[offset : offset + 32], "big")


def _offset(value: bytes, offset: int, minimum: int, label: str) -> int:
    if offset % 32 or offset < minimum or offset >= len(value):
        raise RpcEvidenceError(f"ABI {label} offset is malformed")
    return offset


def _abi_bytes_at(value: bytes, offset: int, label: str) -> bytes:
    start = _offset(value, offset, 0, label)
    length = _word_at(value, start)
    end = start + 32 + length
    if end > len(value):
        raise RpcEvidenceError(f"ABI {label} bytes are malformed")
    return value[start + 32 : end]


def _abi_dynamic_string(value: str) -> bytes:
    encoded = value.encode("ascii")
    padding = (-len(encoded)) % 32
    return (32).to_bytes(32, "big") + len(encoded).to_bytes(32, "big") + encoded + b"\x00" * padding


def _abi_return_address(value: object) -> str:
    raw = _raw_hex_quantity_bytes(value, "registry result")
    if len(raw) != 32 or raw[:12] != b"\x00" * 12:
        raise RpcEvidenceError("registry result is not an address")
    return "0x" + raw[12:].hex()


def _abi_return_string(value: object) -> str:
    raw = _raw_hex_quantity_bytes(value, "string result")
    try:
        encoded = _abi_bytes_at(raw, _word_at(raw, 0), "string result")
        return encoded.decode("ascii")
    except (UnicodeDecodeError, RpcEvidenceError) as error:
        raise RpcEvidenceError("string result is malformed") from error


def _format_units(value: int, decimals: int) -> str:
    scale = 10 ** decimals
    whole, fraction = divmod(value, scale)
    return str(whole) if fraction == 0 else f"{whole}.{fraction:0{decimals}d}".rstrip("0")


def _memo_data(transaction: JsonObject) -> str:
    memos = transaction.get("Memos")
    if not isinstance(memos, list) or len(memos) != 1 or not isinstance(memos[0], Mapping):
        raise RpcEvidenceError("XRPL Payment must contain exactly one memo")
    memo = memos[0].get("Memo")
    if not isinstance(memo, Mapping) or not isinstance(memo.get("MemoData"), str):
        raise RpcEvidenceError("XRPL Payment memo is malformed")
    return memo["MemoData"].upper()


def _require_bound_timestamp(parent: JsonObject, unix_seconds: int) -> None:
    try:
        expected = datetime.fromtimestamp(unix_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError) as error:
        raise RpcEvidenceError("RPC timestamp is malformed") from error
    if _text(parent, "timestamp") != expected:
        raise RpcEvidenceError("evidence timestamp does not match the validated chain fact")


def _require_url(value: str, label: str) -> str:
    if not value.startswith(("https://", "http://")):
        raise RpcEvidenceError(f"{label} must be an HTTP(S) URL")
    return value
