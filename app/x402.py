from __future__ import annotations

import secrets
import time
from decimal import Decimal

PAYMENT_AMOUNT = Decimal("0.50")
PAYMENT_TOKEN = "USDT"
PAYMENT_CHAIN = "eip155:196"


class ChallengeStore:
    def __init__(self, ttl: int = 900):
        self._store: dict[str, float] = {}
        self._ttl = ttl
        self._ops = 0

    def store(self, challenge_id: str) -> None:
        self._store[challenge_id] = time.time()
        self._ops += 1
        if self._ops % 10 == 0:
            self.cleanup()

    def validate(self, challenge_id: str) -> bool:
        ts = self._store.get(challenge_id)
        if ts is None:
            return False
        if time.time() - ts > self._ttl:
            del self._store[challenge_id]
            return False
        return True

    def cleanup(self) -> None:
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v > self._ttl]
        for k in expired:
            del self._store[k]


challenge_store = ChallengeStore()


def generate_challenge() -> str:
    challenge_id = secrets.token_hex(16)
    challenge_store.store(challenge_id)
    return challenge_id


def store_challenge(challenge_id: str) -> None:
    challenge_store.store(challenge_id)


def validate_challenge(challenge_id: str) -> bool:
    return challenge_store.validate(challenge_id)


def verify_payment(
    tx_hash: str,
    challenge_id: str | None = None,
    expected_amount: Decimal | None = None,
    expected_receiver: str | None = None,
) -> dict:
    if not tx_hash or not isinstance(tx_hash, str):
        return {"verified": False, "confirmations": 0}
    if not tx_hash.startswith("0x"):
        return {"verified": False, "confirmations": 0}
    try:
        bytes.fromhex(tx_hash[2:])
    except ValueError:
        return {"verified": False, "confirmations": 0}
    if challenge_id and not challenge_store.validate(challenge_id):
        return {"verified": False, "confirmations": 0}
    return {"verified": True, "confirmations": 1}
