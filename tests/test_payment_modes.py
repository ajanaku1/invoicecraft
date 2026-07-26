from decimal import Decimal

from app.x402 import generate_challenge, validate_challenge, verify_payment

GOOD_TX = "0x" + "01" * 32


def test_mock_mode_verifies_wellformed_payment(monkeypatch):
    monkeypatch.setenv("PAYMENT_VERIFY_MODE", "mock")
    cid = generate_challenge()
    result = verify_payment(tx_hash=GOOD_TX, challenge_id=cid)
    assert result["verified"] is True


def test_challenge_is_consumed_after_use(monkeypatch):
    monkeypatch.setenv("PAYMENT_VERIFY_MODE", "mock")
    cid = generate_challenge()
    assert validate_challenge(cid) is True
    assert verify_payment(tx_hash=GOOD_TX, challenge_id=cid)["verified"] is True
    # Second attempt with the same challenge must fail (replay protection).
    assert validate_challenge(cid) is False
    assert verify_payment(tx_hash=GOOD_TX, challenge_id=cid)["verified"] is False


def test_tx_cannot_be_reused(monkeypatch):
    monkeypatch.setenv("PAYMENT_VERIFY_MODE", "mock")
    tx = "0x" + "02" * 32
    assert verify_payment(tx, generate_challenge())["verified"] is True
    # Same on-chain payment, a fresh challenge — must be rejected as a replay.
    replay = verify_payment(tx, generate_challenge())
    assert replay["verified"] is False
    assert replay["reason"] == "tx_already_used"


def test_bad_tx_format_rejected(monkeypatch):
    monkeypatch.setenv("PAYMENT_VERIFY_MODE", "mock")
    cid = generate_challenge()
    assert verify_payment(tx_hash="not_a_hash", challenge_id=cid)["verified"] is False


def test_onchain_mode_requires_expected_params(monkeypatch):
    monkeypatch.setenv("PAYMENT_VERIFY_MODE", "onchain")
    cid = generate_challenge()
    result = verify_payment(tx_hash=GOOD_TX, challenge_id=cid)
    assert result["verified"] is False
    assert result["reason"] == "missing_expected_params"


def test_onchain_mode_unconfigured_rpc_fails_closed(monkeypatch):
    monkeypatch.setenv("PAYMENT_VERIFY_MODE", "onchain")
    monkeypatch.delenv("XLAYER_RPC", raising=False)
    monkeypatch.delenv("USDT_CONTRACT", raising=False)
    cid = generate_challenge()
    result = verify_payment(
        tx_hash=GOOD_TX,
        challenge_id=cid,
        expected_amount=Decimal("0.50"),
        expected_receiver="0x000000000000000000000000000000000000dEaD",
    )
    assert result["verified"] is False
