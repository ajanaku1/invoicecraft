from collections.abc import Mapping

import pytest

from app.xrp.xaman import XamanError, XamanGateway


UNSIGNED_PAYMENT = {
    "TransactionType": "Payment",
    "Account": "rSource",
    "Destination": "rCoreVault",
    "Amount": "10100000",
    "Memos": [{"Memo": {"MemoData": "FE" + "00" * 41}}],
}


def test_missing_credentials_returns_exact_unsigned_fallback() -> None:
    result = XamanGateway(api_key="", api_secret="").create(UNSIGNED_PAYMENT)

    assert result.mode == "unsigned"
    assert result.unsigned_transaction == UNSIGNED_PAYMENT
    assert result.xaman_uuid is None


def test_xaman_posts_testnet_payload_and_returns_safe_links() -> None:
    seen: list[tuple[str, Mapping[str, object], Mapping[str, str]]] = []

    def post(url: str, body: Mapping[str, object], headers: Mapping[str, str]) -> Mapping[str, object]:
        seen.append((url, body, headers))
        return {
            "uuid": "123e4567-e89b-12d3-a456-426614174000",
            "next": {
                "always": "https://xumm.app/sign/123e4567-e89b-12d3-a456-426614174000",
                "qr_png": "https://xumm.app/sign/123e4567-e89b-12d3-a456-426614174000_q.png",
                "untrusted": "https://example.com/ignore",
            },
        }

    result = XamanGateway("key", "secret", post=post).create(UNSIGNED_PAYMENT)

    assert seen[0][0] == "https://xumm.app/api/v1/platform/payload"
    assert seen[0][1] == {"txjson": UNSIGNED_PAYMENT, "options": {"force_network": "TESTNET"}}
    assert seen[0][2]["X-API-Key"] == "key"
    assert seen[0][2]["X-API-Secret"] == "secret"
    assert result.mode == "xaman"
    assert result.xaman_uuid == "123e4567-e89b-12d3-a456-426614174000"
    assert set(result.links) == {"always", "qr_png"}


def test_xaman_rejects_malformed_or_wrong_host_links() -> None:
    def post(*_args: object) -> Mapping[str, object]:
        return {
            "uuid": "not-a-uuid",
            "next": {"always": "https://evil.example/sign/not-a-uuid"},
        }

    with pytest.raises(XamanError):
        XamanGateway("key", "secret", post=post).create(UNSIGNED_PAYMENT)

    def wrong_host(*_args: object) -> Mapping[str, object]:
        return {
            "uuid": "123e4567-e89b-12d3-a456-426614174000",
            "next": {"always": "https://evil.example/sign/123e4567-e89b-12d3-a456-426614174000"},
        }

    with pytest.raises(XamanError):
        XamanGateway("key", "secret", post=wrong_host).create(UNSIGNED_PAYMENT)


def test_xaman_transport_failure_returns_exact_unsigned_fallback() -> None:
    def unavailable(*_args: object) -> Mapping[str, object]:
        raise OSError("offline")

    result = XamanGateway("key", "secret", post=unavailable).create(UNSIGNED_PAYMENT)

    assert result.mode == "unsigned"
    assert result.unsigned_transaction == UNSIGNED_PAYMENT
    assert result.xaman_uuid is None


def test_xaman_does_not_hide_non_transport_failures() -> None:
    def broken_code(*_args: object) -> Mapping[str, object]:
        raise ValueError("bug")

    with pytest.raises(XamanError):
        XamanGateway("key", "secret", post=broken_code).create(UNSIGNED_PAYMENT)
