from __future__ import annotations

import logging
import os
import secrets
from decimal import Decimal

from app import store

logger = logging.getLogger(__name__)

PAYMENT_AMOUNT = Decimal("0.50")
PAYMENT_TOKEN = "USDT"
PAYMENT_CHAIN = "eip155:196"
CHALLENGE_TTL = int(os.getenv("CHALLENGE_TTL", "900"))

# ERC-20 Transfer(address,address,uint256) event signature.
_TRANSFER_TOPIC_TEXT = "Transfer(address,address,uint256)"


class ChallengeStore:
    """Thin wrapper over the persistent store (kept for import compatibility)."""

    def __init__(self, ttl: int = CHALLENGE_TTL):
        self._ttl = ttl

    def store(self, challenge_id: str) -> None:
        store.add_challenge(challenge_id)

    def validate(self, challenge_id: str) -> bool:
        return store.challenge_valid(challenge_id, self._ttl)

    def consume(self, challenge_id: str) -> None:
        store.consume_challenge(challenge_id)

    def cleanup(self) -> None:
        store.cleanup_challenges(self._ttl)


challenge_store = ChallengeStore()


def generate_challenge() -> str:
    challenge_id = secrets.token_hex(16)
    challenge_store.store(challenge_id)
    return challenge_id


def store_challenge(challenge_id: str) -> None:
    challenge_store.store(challenge_id)


def validate_challenge(challenge_id: str) -> bool:
    return challenge_store.validate(challenge_id)


def _valid_tx_format(tx_hash: str) -> bool:
    if not tx_hash or not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
        return False
    try:
        bytes.fromhex(tx_hash[2:])
    except ValueError:
        return False
    return True


def _verify_onchain(
    tx_hash: str, expected_amount: Decimal, expected_receiver: str
) -> dict:
    """Verify a real USDT transfer on X Layer via web3.

    Confirms the transaction succeeded, has enough confirmations, and emitted a
    USDT Transfer to the expected receiver for at least the expected amount.
    Requires XLAYER_RPC and USDT_CONTRACT to be configured. Not exercised by the
    test suite (which runs in mock mode); verify against a live RPC before
    relying on it in production.
    """
    rpc = os.getenv("XLAYER_RPC")
    usdt = os.getenv("USDT_CONTRACT")
    if not rpc or not usdt:
        return {"verified": False, "confirmations": 0, "reason": "rpc_or_contract_not_configured"}

    try:
        from web3 import Web3
    except ImportError:
        return {"verified": False, "confirmations": 0, "reason": "web3_not_installed"}

    try:
        w3 = Web3(Web3.HTTPProvider(rpc))
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        logger.warning("On-chain lookup failed for %s", tx_hash, exc_info=True)
        return {"verified": False, "confirmations": 0, "reason": "tx_not_found"}

    if receipt is None or receipt.get("status") != 1:
        return {"verified": False, "confirmations": 0, "reason": "tx_failed"}

    confirmations = w3.eth.block_number - receipt["blockNumber"] + 1
    min_conf = int(os.getenv("MIN_CONFIRMATIONS", "1"))
    if confirmations < min_conf:
        return {"verified": False, "confirmations": confirmations, "reason": "insufficient_confirmations"}

    transfer_topic = w3.keccak(text=_TRANSFER_TOPIC_TEXT)
    usdt_addr = Web3.to_checksum_address(usdt)
    receiver = Web3.to_checksum_address(expected_receiver)
    decimals = int(os.getenv("USDT_DECIMALS", "6"))
    min_units = int((Decimal(expected_amount) * (10 ** decimals)).to_integral_value())

    for log in receipt["logs"]:
        if Web3.to_checksum_address(log["address"]) != usdt_addr:
            continue
        topics = log["topics"]
        if not topics or bytes(topics[0]) != bytes(transfer_topic):
            continue
        to_addr = Web3.to_checksum_address(bytes(topics[2])[-20:])
        if to_addr != receiver:
            continue
        value = int.from_bytes(bytes(log["data"]), "big")
        if value >= min_units:
            return {"verified": True, "confirmations": confirmations}

    return {"verified": False, "confirmations": confirmations, "reason": "no_matching_transfer"}


def verify_payment(
    tx_hash: str,
    challenge_id: str | None = None,
    expected_amount: Decimal | None = None,
    expected_receiver: str | None = None,
) -> dict:
    """Verify an x402 payment.

    PAYMENT_VERIFY_MODE controls behavior:
      - "mock" (default): validates tx-hash format and challenge only; used for
        local dev, tests, and the demo. Does NOT confirm an on-chain transfer.
      - "onchain": performs a real USDT transfer check on X Layer via web3.

    A verified challenge is consumed (one-time) to prevent replay.
    """
    if not _valid_tx_format(tx_hash):
        return {"verified": False, "confirmations": 0, "reason": "bad_tx_format"}
    if challenge_id and not challenge_store.validate(challenge_id):
        return {"verified": False, "confirmations": 0, "reason": "invalid_challenge"}

    mode = os.getenv("PAYMENT_VERIFY_MODE", "mock").lower()
    if mode == "onchain":
        if expected_amount is None or expected_receiver is None:
            return {"verified": False, "confirmations": 0, "reason": "missing_expected_params"}
        result = _verify_onchain(tx_hash, expected_amount, expected_receiver)
    else:
        result = {"verified": True, "confirmations": 1, "mode": "mock"}

    if result.get("verified"):
        # Reject replay of an on-chain payment already spent on another invoice.
        if store.tx_consumed(tx_hash):
            return {
                "verified": False,
                "confirmations": result.get("confirmations", 0),
                "reason": "tx_already_used",
            }
        if challenge_id:
            challenge_store.consume(challenge_id)
        store.consume_tx(tx_hash)
    return result
