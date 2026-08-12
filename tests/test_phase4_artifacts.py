from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.verify_acceptance_record import AcceptanceError, validate_record
from scripts.verify_no_secrets import SecretScanError, scan_paths


XRPL_HASH = "1CBE730F2C98A858DA769C654BB2CD3B2F7F5DB2165BE0AAA76A11D654F788CC"
FLARE_HASH = "0x9b16227528c10af0f5ec4e46018d9f9dabe5af6235dfb8955d6932f4bf3fa1b6"
XAMAN_UUID = "82a5ad2d-2df1-4c7b-b19e-d411c7c81053"
SETTLEMENT = "0x" + "11" * 20
ADAPTER = "0x" + "22" * 20
USD0 = "0x" + "33" * 20
BENEFICIARY = "0x" + "44" * 20


def product_evidence() -> dict[str, object]:
    return {
        "product_deployment": {
            "status": "passed",
            "chain_id": 114,
            "invoice_settlement": SETTLEMENT,
            "test_liquidity_adapter": ADAPTER,
            "usd0_token": USD0,
            "adapter_label": "TEST LIQUIDITY - NOT A REAL COSTON2 MARKET",
        },
        "product_smoke": {
            "status": "passed",
            "receipt": "evidence/product-settlement-smoke.json",
        },
    }


def acceptance_record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "completed",
        "selected_ui": {
            "status": "approved",
            "proposal": "option-1.html",
            "viewports": [
                {"width": 1440, "height": 900, "result": "passed", "horizontal_overflow": False},
                {"width": 390, "height": 844, "result": "passed", "horizontal_overflow": False},
            ],
        },
        "xaman": {
            "status": "passed",
            "device": "iPhone with Xaman",
            "network": "XRPL Testnet",
            "opened_in_app": True,
            "deep_link": f"https://xumm.app/sign/{XAMAN_UUID}",
            "transaction_hash": XRPL_HASH,
        },
        "demo": {"status": "passed", "duration_seconds": 120, "rehearsed": True, "script": "README.md"},
        "explorers": {
            "xrpl": f"https://testnet.xrpl.org/transactions/{XRPL_HASH}",
            "coston2": f"https://coston2-explorer.flare.network/tx/{FLARE_HASH}",
        },
        "recovery_copy": {"status": "approved", "guided_only": True, "automatic_fund_movement_claimed": False},
        **product_evidence(),
        "before_new": {"status": "approved", "source": "README.md", "before_count": 4, "new_count": 8},
    }


def write_project_files(root: Path) -> None:
    (root / "README.md").write_text("Existing before Bounty 1\nNew for Bounty 1\n", encoding="utf-8")
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    receipt = {
        "schema_version": 1,
        "status": "paid",
        "network": "coston2",
        "payout": {"beneficiary": BENEFICIARY, "currency": "USD₮0", "amount_uba": 75_000_000, "token": USD0},
        "xrpl": {"transaction_hash": XRPL_HASH, "explorer_url": f"https://testnet.xrpl.org/transactions/{XRPL_HASH}"},
        "flare": {"transaction_hash": FLARE_HASH, "settlement_contract": SETTLEMENT, "explorer_url": f"https://coston2-explorer.flare.network/tx/{FLARE_HASH}"},
        "liquidity": {"label": "Test liquidity — not a real Coston2 market", "adapter": ADAPTER},
    }
    (evidence / "product-settlement-smoke.json").write_text(json.dumps(receipt), encoding="utf-8")


def test_completed_acceptance_record_validates(tmp_path: Path) -> None:
    write_project_files(tmp_path)

    validate_record(acceptance_record(), "option-1.html", tmp_path)


def test_completed_acceptance_record_does_not_require_interviews(tmp_path: Path) -> None:
    write_project_files(tmp_path)
    record = acceptance_record()
    record["interviews"] = []

    validate_record(record, "option-1.html", tmp_path)


def test_acceptance_record_requires_product_deployment_and_exact_receipt(tmp_path: Path) -> None:
    write_project_files(tmp_path)
    record = acceptance_record()
    record.pop("product_deployment")

    with pytest.raises(AcceptanceError):
        validate_record(record, "option-1.html", tmp_path)


def test_acceptance_record_rejects_malformed_receipt(tmp_path: Path) -> None:
    write_project_files(tmp_path)
    receipt_path = tmp_path / "evidence" / "product-settlement-smoke.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["payout"]["token"] = None
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(AcceptanceError):
        validate_record(acceptance_record(), "option-1.html", tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.update(status="pending"),
        lambda record: record["xaman"].update(opened_in_app=False),
        lambda record: record["demo"].update(duration_seconds=121),
        lambda record: record["recovery_copy"].update(automatic_fund_movement_claimed=True),
    ],
)
def test_acceptance_record_rejects_unproven_requirements(
    tmp_path: Path, mutation: Callable[[dict[str, object]], None]
) -> None:
    write_project_files(tmp_path)
    record = acceptance_record()
    mutation(record)

    with pytest.raises(AcceptanceError):
        validate_record(record, "option-1.html", tmp_path)


def test_secret_scan_accepts_hashes_and_placeholders(tmp_path: Path) -> None:
    safe = tmp_path / "safe.json"
    safe.write_text(json.dumps({"transaction_hash": FLARE_HASH, "api_key": "your-api-key-here"}), encoding="utf-8")

    scan_paths([safe])


def test_phase4_verifier_requires_rpc_validated_product_settlement() -> None:
    verifier = (Path(__file__).parents[1] / "verify.sh").read_text(encoding="utf-8")

    assert "verify_product_settlement" in verifier
    assert "evidence/product-settlement-smoke.json" in verifier


@pytest.mark.parametrize(
    "content",
    [
        "-----BEGIN " + "PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        "XAMAN_API_" + "SECRET=abcdefghijklmnopqrstuvwxyz123456",
        'mnemonic = "' + "one two three four five six seven eight nine ten eleven twelve" + '"',
    ],
)
def test_secret_scan_rejects_private_material(tmp_path: Path, content: str) -> None:
    unsafe = tmp_path / "unsafe.txt"
    unsafe.write_text(content, encoding="utf-8")

    with pytest.raises(SecretScanError):
        scan_paths([unsafe])
