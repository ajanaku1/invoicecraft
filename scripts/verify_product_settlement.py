#!/usr/bin/env python3
"""Validate a paid product receipt against Coston2 contract calls and logs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.xrp.instructions import keccak256
from app.xrp.rpc import (
    COSTON2_CHAIN_ID,
    TRUSTED_COSTON2_RPC_URL,
    JsonRpcClient,
    RpcEvidenceError,
    Transport,
)


JsonObject = Mapping[str, object]
ADAPTER_LABEL = "TEST LIQUIDITY - NOT A REAL COSTON2 MARKET"
COSTON2_FXRP = "0x0b6a3645c240605887a5532109323a3e12273dc7"
COSTON2_USDT0_TEST = "0xc1a5b41512496b80903d1f32d6dea3a73212e71f"
FXRP_METADATA = ("FXRP", "FTestXRP", 6)
USDT0_NAME = "USDT0 test"
USDT0_SYMBOL = "USD₮0"
USDT0_DECIMALS = 6


class ProductEvidenceError(ValueError):
    """Raised when the exact product settlement cannot be proven."""


def _mapping(parent: JsonObject, key: str) -> JsonObject:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise ProductEvidenceError(f"{key} must be an object")
    return value


def _text(parent: JsonObject, key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise ProductEvidenceError(f"{key} must be text")
    return value


def _positive_int(parent: JsonObject, key: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProductEvidenceError(f"{key} must be a positive integer")
    return value


def _nonnegative_int(parent: JsonObject, key: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProductEvidenceError(f"{key} must be a nonnegative integer")
    return value


def _address(parent: JsonObject, key: str) -> str:
    value = _text(parent, key).lower()
    if re.fullmatch(r"0x[0-9a-f]{40}", value) is None or int(value[2:], 16) == 0:
        raise ProductEvidenceError(f"{key} is not an EVM address")
    return value


def _hash(parent: JsonObject, key: str, prefix: bool = True) -> str:
    value = _text(parent, key)
    pattern = r"0x[0-9A-Fa-f]{64}" if prefix else r"[0-9A-Fa-f]{64}"
    if re.fullmatch(pattern, value) is None:
        raise ProductEvidenceError(f"{key} is not a transaction hash")
    return value


def _selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


def _topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii")).hex()


def _word(value: int) -> str:
    return value.to_bytes(32, "big").hex()


def _address_topic(value: str) -> str:
    return "0x" + "00" * 12 + value[2:]


def _explorer(parent: JsonObject, key: str, host: str, path: str) -> None:
    parsed = urlparse(_text(parent, key))
    if parsed.scheme != "https" or parsed.hostname != host or parsed.path.lower() != path.lower():
        raise ProductEvidenceError(f"{key} does not bind the trusted explorer")


def _result_mapping(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ProductEvidenceError(f"{label} RPC result is malformed")
    return value


def _contract_code(client: JsonRpcClient, address: str, block: str) -> None:
    value = client.request("eth_getCode", [address, block])
    if not isinstance(value, str) or value in {"0x", "0x0"}:
        raise ProductEvidenceError("deployed contract code is missing")
    try:
        bytes.fromhex(value.removeprefix("0x"))
    except ValueError as error:
        raise ProductEvidenceError("deployed contract code is malformed") from error


def _call(client: JsonRpcClient, target: str, signature: str, block: str) -> str:
    value = client.request("eth_call", [{"to": target, "data": _selector(signature)}, block])
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ProductEvidenceError(f"{signature} response is malformed")
    return value


def _call_address(client: JsonRpcClient, target: str, signature: str, block: str) -> str:
    value = _call(client, target, signature, block)
    if re.fullmatch(r"0x[0-9A-Fa-f]{64}", value) is None or int(value[2:26], 16) != 0:
        raise ProductEvidenceError(f"{signature} did not return an address")
    return "0x" + value[-40:].lower()


def _call_string(client: JsonRpcClient, target: str, signature: str, block: str) -> str:
    try:
        raw = bytes.fromhex(_call(client, target, signature, block)[2:])
        offset = int.from_bytes(raw[:32], "big")
        length = int.from_bytes(raw[offset : offset + 32], "big")
        end = offset + 32 + length
        if offset != 32 or end > len(raw):
            raise ValueError
        return raw[offset + 32 : end].decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ProductEvidenceError(f"{signature} did not return text") from error


def _call_uint(client: JsonRpcClient, target: str, signature: str, block: str) -> int:
    value = _call(client, target, signature, block)
    if re.fullmatch(r"0x[0-9A-Fa-f]{64}", value) is None:
        raise ProductEvidenceError(f"{signature} did not return an integer")
    return int(value, 16)


def _validate_usdt0(client: JsonRpcClient, token: str, block: str) -> None:
    metadata = (
        _call_string(client, token, "name()", block),
        _call_string(client, token, "symbol()", block),
        _call_uint(client, token, "decimals()", block),
    )
    if token != COSTON2_USDT0_TEST or metadata != (
        USDT0_NAME,
        USDT0_SYMBOL,
        USDT0_DECIMALS,
    ):
        raise ProductEvidenceError("payout token is not the canonical Coston2 test USD₮0")


def _validate_fxrp(client: JsonRpcClient, token: str, block: str) -> None:
    metadata = (
        _call_string(client, token, "name()", block),
        _call_string(client, token, "symbol()", block),
        _call_uint(client, token, "decimals()", block),
    )
    if token != COSTON2_FXRP or metadata != FXRP_METADATA:
        raise ProductEvidenceError("settlement input is not the canonical Coston2 FXRP")


def _validate_contract_bindings(
    client: JsonRpcClient, settlement: str, adapter: str, token: str, block: str
) -> None:
    for address in (settlement, adapter, token):
        _contract_code(client, address, block)
    fxrp = _call_address(client, settlement, "fxrp()", block)
    _contract_code(client, fxrp, block)
    expected = {
        (settlement, "usd0()"): token,
        (settlement, "adapter()"): adapter,
        (adapter, "fxrp()"): fxrp,
        (adapter, "usd0()"): token,
        (adapter, "authorizedSettlement()"): settlement,
    }
    if any(_call_address(client, target, signature, block) != value for (target, signature), value in expected.items()):
        raise ProductEvidenceError("deployed contract getters do not bind the receipt")
    if _call_string(client, adapter, "label()", block) != ADAPTER_LABEL:
        raise ProductEvidenceError("deployed adapter label is not explicit")
    _validate_fxrp(client, fxrp, block)
    _validate_usdt0(client, token, block)


def _expected_logs(evidence: JsonObject) -> tuple[dict[str, object], ...]:
    payout, flare, liquidity = _mapping(evidence, "payout"), _mapping(evidence, "flare"), _mapping(evidence, "liquidity")
    beneficiary, amount = _address(payout, "beneficiary"), _positive_int(payout, "amount_uba")
    settlement, adapter, token = _address(flare, "settlement_contract"), _address(liquidity, "adapter"), _address(payout, "token")
    settlement_id, invoice_hash = _hash(flare, "settlement_id"), _hash(evidence, "canonical_hash")
    fxrp_input = _positive_int(flare, "fxrp_input_uba")
    values = "0x" + _word(fxrp_input) + _word(amount)
    return (
        {"address": settlement, "topics": [_topic("InvoiceSettled(bytes32,bytes32,address,uint256,uint256)"), settlement_id, invoice_hash, _address_topic(beneficiary)], "data": values},
        {"address": adapter, "logIndex": hex(_nonnegative_int(liquidity, "event_index")), "topics": [_topic("TestLiquidityUsed(bytes32,address,uint256,uint256)"), settlement_id, _address_topic(beneficiary)], "data": values},
        {"address": token, "topics": [_topic("Transfer(address,address,uint256)"), _address_topic(adapter), _address_topic(beneficiary)], "data": "0x" + _word(amount)},
    )


def _matches(log: object, expected: Mapping[str, object]) -> bool:
    if not isinstance(log, Mapping):
        return False
    for key, value in expected.items():
        actual = log.get(key)
        if isinstance(value, str) and isinstance(actual, str):
            if actual.lower() != value.lower():
                return False
        elif actual != value:
            return False
    return True


def _validate_receipt_logs(receipt: JsonObject, evidence: JsonObject) -> None:
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise ProductEvidenceError("Coston2 receipt has no logs")
    for expected in _expected_logs(evidence):
        if not any(_matches(log, expected) for log in logs):
            raise ProductEvidenceError("Coston2 receipt lacks an exact settlement event")


def _validate_local_receipt(evidence: JsonObject) -> tuple[str, str, str, str, int]:
    if evidence.get("schema_version") != 1 or evidence.get("status") != "paid" or evidence.get("network") != "coston2":
        raise ProductEvidenceError("product evidence is not a paid Coston2 receipt")
    payout, flare, liquidity = _mapping(evidence, "payout"), _mapping(evidence, "flare"), _mapping(evidence, "liquidity")
    if payout.get("currency") != "USD₮0" or "Test liquidity" not in _text(liquidity, "label"):
        raise ProductEvidenceError("product payout or liquidity label is incorrect")
    _positive_int(payout, "amount_uba")
    xrpl = _mapping(evidence, "xrpl")
    xrpl_hash = _hash(xrpl, "transaction_hash", prefix=False)
    flare_hash = _hash(flare, "transaction_hash")
    _explorer(xrpl, "explorer_url", "testnet.xrpl.org", f"/transactions/{xrpl_hash}")
    _explorer(flare, "explorer_url", "coston2-explorer.flare.network", f"/tx/{flare_hash}")
    return _address(flare, "settlement_contract"), _address(liquidity, "adapter"), _address(payout, "token"), _hash(flare, "transaction_hash"), _positive_int(flare, "block_number")


def validate_product_settlement(
    evidence: JsonObject, timeout_seconds: int, transport: Transport | None = None
) -> None:
    settlement, adapter, token, transaction, block_number = _validate_local_receipt(evidence)
    client = JsonRpcClient(TRUSTED_COSTON2_RPC_URL, timeout_seconds, transport)
    if client.request("eth_chainId") != hex(COSTON2_CHAIN_ID):
        raise ProductEvidenceError("RPC endpoint is not Coston2")
    receipt = _result_mapping(client.request("eth_getTransactionReceipt", [transaction]), "receipt")
    if receipt.get("status") != "0x1" or str(receipt.get("transactionHash", "")).lower() != transaction.lower():
        raise ProductEvidenceError("product settlement transaction did not succeed")
    if receipt.get("blockNumber") != hex(block_number):
        raise ProductEvidenceError("product settlement block does not match")
    block = hex(block_number)
    _validate_contract_bindings(client, settlement, adapter, token, block)
    _validate_receipt_logs(receipt, evidence)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("evidence", type=Path)
    arguments = parser.parse_args()
    try:
        value = json.loads(arguments.evidence.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ProductEvidenceError("product evidence must be an object")
        validate_product_settlement(value, arguments.timeout_seconds)
    except (OSError, json.JSONDecodeError, ProductEvidenceError, RpcEvidenceError):
        return 1
    print("PRODUCT_SETTLEMENT_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
