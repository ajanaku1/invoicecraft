"""Xaman TESTNET signing-request boundary with an exact unsigned fallback."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

Post = Callable[[str, Mapping[str, object], Mapping[str, str]], Mapping[str, object]]
XAMAN_PAYLOAD_URL = "https://xumm.app/api/v1/platform/payload"
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


class XamanError(ValueError):
    """Raised when Xaman cannot safely represent the signing request."""


@dataclass(frozen=True)
class SigningResult:
    mode: str
    unsigned_transaction: dict[str, object]
    xaman_uuid: str | None = None
    links: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "unsigned_transaction": self.unsigned_transaction,
            "xaman_uuid": self.xaman_uuid,
            "links": self.links,
        }


class XamanGateway:
    def __init__(self, api_key: str, api_secret: str, post: Post | None = None) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.post = post or _post_xaman

    def create(self, unsigned_payment: dict[str, object]) -> SigningResult:
        payment = _payment_copy(unsigned_payment)
        if not self.api_key or not self.api_secret:
            return SigningResult(mode="unsigned", unsigned_transaction=payment)
        body = {"txjson": payment, "options": {"force_network": "TESTNET"}}
        headers = {
            "X-API-Key": self.api_key,
            "X-API-Secret": self.api_secret,
            "User-Agent": "InvoiceCraftXRP/1.0",
        }
        try:
            response = self.post(XAMAN_PAYLOAD_URL, body, headers)
        except (OSError, TimeoutError):
            return SigningResult(mode="unsigned", unsigned_transaction=payment)
        except Exception as error:
            raise XamanError("Xaman request failed") from error
        uuid = _uuid(response.get("uuid"))
        links = _safe_links(response.get("next"), uuid)
        if not links:
            raise XamanError("Xaman response contained no trusted signing link")
        return SigningResult(
            mode="xaman",
            unsigned_transaction=payment,
            xaman_uuid=uuid,
            links=links,
        )


def _payment_copy(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("TransactionType") != "Payment":
        raise XamanError("unsigned transaction must be an XRPL Payment")
    try:
        copy = json.loads(json.dumps(value))
    except (TypeError, ValueError) as error:
        raise XamanError("unsigned transaction is not JSON-safe") from error
    if not isinstance(copy, dict):
        raise XamanError("unsigned transaction is malformed")
    return copy


def _uuid(value: object) -> str:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise XamanError("Xaman response UUID is malformed")
    return value.lower()


def _safe_links(value: object, uuid: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, str] = {}
    for name in ("always", "no_push_msg_received", "qr_png"):
        link = value.get(name)
        if isinstance(link, str) and _xaman_link(link, uuid):
            safe[name] = link
    return safe


def _xaman_link(value: str, uuid: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc == "xumm.app" and uuid in parsed.path


def _post_xaman(
    url: str, body: Mapping[str, object], headers: Mapping[str, str]
) -> Mapping[str, object]:
    from .live import LiveError, _post_json

    try:
        return _post_json(url, body, headers["X-API-Key"], headers["X-API-Secret"])
    except LiveError as error:
        raise OSError("Xaman transport failed") from error
