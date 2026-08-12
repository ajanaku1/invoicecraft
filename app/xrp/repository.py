"""Persistence boundary for XRP invoice resources."""

from __future__ import annotations

import json

from app import store


class XrpInvoiceRepository:
    def create(self, invoice_id: str, document: dict[str, object]) -> bool:
        _validate_document(invoice_id, document)
        return store.create_xrp_invoice(invoice_id, _encode(document))

    def get(self, invoice_id: str) -> dict[str, object] | None:
        encoded = store.get_xrp_invoice(_invoice_id(invoice_id))
        if encoded is None:
            return None
        value = json.loads(encoded)
        if not isinstance(value, dict):
            raise ValueError("stored XRP invoice is malformed")
        return value

    def replace(self, invoice_id: str, document: dict[str, object]) -> bool:
        _validate_document(invoice_id, document)
        return store.replace_xrp_invoice(invoice_id, _encode(document))

    def claim_idempotency(
        self, key_hash: str, invoice_id: str, request_hash: str
    ) -> dict[str, str]:
        _invoice_id(invoice_id)
        if len(key_hash) != 64 or any(
            character not in "0123456789abcdef" for character in key_hash
        ):
            raise ValueError("idempotency hash is malformed")
        proposed = {"invoice_id": invoice_id, "request_hash": request_hash}
        stored = json.loads(store.claim_xrp_idempotency(key_hash, _encode(proposed)))
        if not isinstance(stored, dict) or set(stored) != {"invoice_id", "request_hash"}:
            raise ValueError("idempotency claim is malformed")
        return {
            "invoice_id": str(stored["invoice_id"]),
            "request_hash": str(stored["request_hash"]),
        }

    def claim_transaction(self, transaction_hash: str, invoice_id: str) -> str:
        normalized = _transaction_hash(transaction_hash)
        return store.claim_xrp_transaction(normalized, _invoice_id(invoice_id))

    def acquire_execution_lock(
        self, invoice_id: str, owner: str, now: int, ttl: int = 300
    ) -> bool:
        _invoice_id(invoice_id)
        _lock_owner(owner)
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("lock timestamp is malformed")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
            raise ValueError("lock TTL is malformed")
        return store.acquire_xrp_execution_lock(invoice_id, owner, now, ttl)

    def release_execution_lock(self, invoice_id: str, owner: str) -> bool:
        _invoice_id(invoice_id)
        _lock_owner(owner)
        return store.release_xrp_execution_lock(invoice_id, owner)


def _invoice_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("invoice ID is malformed")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("invoice ID is malformed")
    return value


def _transaction_hash(value: str) -> str:
    normalized = value.removeprefix("0x").removeprefix("0X").lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("transaction hash is malformed")
    return normalized


def _lock_owner(value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(character.isspace() for character in value):
        raise ValueError("lock owner is malformed")


def _validate_document(invoice_id: str, document: dict[str, object]) -> None:
    _invoice_id(invoice_id)
    if not isinstance(document, dict) or document.get("id") != invoice_id:
        raise ValueError("document must bind the invoice ID")


def _encode(document: dict[str, object] | dict[str, str]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
