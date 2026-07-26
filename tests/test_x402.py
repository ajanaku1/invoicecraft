import pytest

from app.x402 import (
    generate_challenge,
    store_challenge,
    validate_challenge,
    verify_payment,
)


def test_generate_challenge_length():
    challenge_id = generate_challenge()
    assert len(challenge_id) == 32
    assert isinstance(challenge_id, str)
    int(challenge_id, 16)


def test_challenge_store_and_validate():
    challenge_id = generate_challenge()
    assert validate_challenge(challenge_id) is True


def test_challenge_store_invalid():
    assert validate_challenge("nonexistent_challenge_12345678") is False


def test_challenge_empty():
    assert validate_challenge("") is False


def test_verify_payment_valid():
    cid = generate_challenge()
    result = verify_payment(
        tx_hash="0x0000000000000000000000000000000000000000000000000000000000000001",
        challenge_id=cid,
    )
    assert result["verified"] is True
    assert result["confirmations"] >= 1


def test_verify_payment_invalid_tx():
    result = verify_payment(tx_hash="not_a_hash")
    assert result["verified"] is False


def test_verify_payment_wrong_challenge():
    result = verify_payment(
        tx_hash="0x0000000000000000000000000000000000000000000000000000000000000001",
        challenge_id="bad_challenge",
    )
    assert result["verified"] is False
