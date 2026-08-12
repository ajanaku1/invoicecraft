#!/usr/bin/env python3
"""Validate the manual evidence required for final InvoiceCraftXRP acceptance."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse


class AcceptanceError(ValueError):
    """Raised when an acceptance claim lacks concrete evidence."""


def _mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise AcceptanceError(f"{key} must be an object")
    return value


def _text(parent: Mapping[str, object], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceError(f"{key} must be non-empty text")
    return value.strip()


def _approved(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = _mapping(parent, key)
    if value.get("status") not in {"approved", "passed"}:
        raise AcceptanceError(f"{key} is not approved")
    return value


def _validate_selected_ui(record: Mapping[str, object], selected: str) -> None:
    ui = _approved(record, "selected_ui")
    if ui.get("proposal") != selected:
        raise AcceptanceError("selected UI does not match the recorded proposal")
    viewports = ui.get("viewports")
    if not isinstance(viewports, list) or len(viewports) < 2:
        raise AcceptanceError("selected UI needs desktop and mobile reviews")
    sizes: list[int] = []
    for viewport in viewports:
        if not isinstance(viewport, Mapping) or viewport.get("result") != "passed":
            raise AcceptanceError("every viewport review must pass")
        if viewport.get("horizontal_overflow") is not False:
            raise AcceptanceError("viewport review reports horizontal overflow")
        width = viewport.get("width")
        if isinstance(width, bool) or not isinstance(width, int) or width < 1:
            raise AcceptanceError("viewport width is malformed")
        sizes.append(width)
    if not any(width >= 1024 for width in sizes) or not any(width <= 420 for width in sizes):
        raise AcceptanceError("desktop and mobile viewport evidence is required")


def _validate_xaman(record: Mapping[str, object]) -> str:
    xaman = _approved(record, "xaman")
    if xaman.get("network") != "XRPL Testnet" or xaman.get("opened_in_app") is not True:
        raise AcceptanceError("Xaman device and testnet opening are not proven")
    if len(_text(xaman, "device")) < 4:
        raise AcceptanceError("Xaman device description is too short")
    transaction = _text(xaman, "transaction_hash")
    if re.fullmatch(r"[0-9A-Fa-f]{64}", transaction) is None:
        raise AcceptanceError("Xaman transaction hash is malformed")
    link = urlparse(_text(xaman, "deep_link"))
    if link.scheme != "https" or link.hostname != "xumm.app":
        raise AcceptanceError("Xaman deep link host is not trusted")
    if re.fullmatch(r"/sign/[0-9a-fA-F-]{36}", link.path) is None:
        raise AcceptanceError("Xaman deep link does not contain a UUID")
    return transaction.upper()


def _project_file(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as error:
        raise AcceptanceError("evidence path leaves the project") from error
    if not target.is_file():
        raise AcceptanceError(f"evidence file is missing: {relative}")
    return target


def _validate_demo(record: Mapping[str, object], root: Path) -> None:
    demo = _approved(record, "demo")
    duration = demo.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 90 <= duration <= 120:
        raise AcceptanceError("demo must run for 90 to 120 seconds")
    if demo.get("rehearsed") is not True:
        raise AcceptanceError("demo pacing was not rehearsed")
    _project_file(root, _text(demo, "script"))


def _validate_explorers(record: Mapping[str, object], xrpl_hash: str) -> None:
    explorers = _mapping(record, "explorers")
    xrpl = urlparse(_text(explorers, "xrpl"))
    if xrpl.scheme != "https" or xrpl.hostname != "testnet.xrpl.org":
        raise AcceptanceError("XRPL explorer host is not trusted")
    if xrpl.path != f"/transactions/{xrpl_hash}":
        raise AcceptanceError("XRPL explorer does not bind the Xaman transaction")
    coston2 = urlparse(_text(explorers, "coston2"))
    if coston2.scheme != "https" or coston2.hostname != "coston2-explorer.flare.network":
        raise AcceptanceError("Coston2 explorer host is not trusted")
    if re.fullmatch(r"/tx/0x[0-9A-Fa-f]{64}", coston2.path) is None:
        raise AcceptanceError("Coston2 explorer transaction is malformed")


def _validate_recovery(record: Mapping[str, object]) -> None:
    recovery = _approved(record, "recovery_copy")
    if recovery.get("guided_only") is not True:
        raise AcceptanceError("recovery is not explicitly guided-only")
    if recovery.get("automatic_fund_movement_claimed") is not False:
        raise AcceptanceError("recovery copy claims automatic fund movement")


def _evm_address(parent: Mapping[str, object], key: str) -> str:
    value = _text(parent, key).lower()
    if re.fullmatch(r"0x[0-9a-f]{40}", value) is None or int(value[2:], 16) == 0:
        raise AcceptanceError(f"{key} is not a deployed EVM address")
    return value


def _validate_product_deployment(record: Mapping[str, object]) -> dict[str, str]:
    deployment = _approved(record, "product_deployment")
    if deployment.get("chain_id") != 114:
        raise AcceptanceError("product contracts are not bound to Coston2")
    addresses = {
        "settlement": _evm_address(deployment, "invoice_settlement"),
        "adapter": _evm_address(deployment, "test_liquidity_adapter"),
        "token": _evm_address(deployment, "usd0_token"),
    }
    if len(set(addresses.values())) != len(addresses):
        raise AcceptanceError("product contract addresses must be distinct")
    if deployment.get("adapter_label") != "TEST LIQUIDITY - NOT A REAL COSTON2 MARKET":
        raise AcceptanceError("test-liquidity adapter label is missing")
    return addresses


def _load_receipt(smoke: Mapping[str, object], root: Path) -> Mapping[str, object]:
    try:
        value = json.loads(_project_file(root, _text(smoke, "receipt")).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise AcceptanceError("product receipt is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise AcceptanceError("product receipt must be an object")
    return value


def _validate_product_receipt(
    receipt: Mapping[str, object], addresses: Mapping[str, str]
) -> tuple[str, str]:
    if receipt.get("schema_version") != 1 or receipt.get("status") != "paid" or receipt.get("network") != "coston2":
        raise AcceptanceError("product receipt is not a paid Coston2 receipt")
    payout = _mapping(receipt, "payout")
    if payout.get("currency") != "USD₮0" or _text(payout, "token").lower() != addresses["token"]:
        raise AcceptanceError("product receipt does not bind the USD₮0 token")
    amount = payout.get("amount_uba")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise AcceptanceError("product receipt payout is not exact and positive")
    _evm_address(payout, "beneficiary")
    flare = _mapping(receipt, "flare")
    if _text(flare, "settlement_contract").lower() != addresses["settlement"]:
        raise AcceptanceError("receipt does not bind InvoiceSettlement")
    liquidity = _mapping(receipt, "liquidity")
    if _text(liquidity, "adapter").lower() != addresses["adapter"] or "Test liquidity" not in _text(liquidity, "label"):
        raise AcceptanceError("receipt does not bind the labelled test adapter")
    return _text(_mapping(receipt, "xrpl"), "explorer_url"), _text(flare, "explorer_url")


def _validate_product_smoke(
    record: Mapping[str, object], root: Path, addresses: Mapping[str, str]
) -> None:
    smoke = _approved(record, "product_smoke")
    xrpl_url, flare_url = _validate_product_receipt(_load_receipt(smoke, root), addresses)
    explorers = _mapping(record, "explorers")
    if xrpl_url != _text(explorers, "xrpl") or flare_url != _text(explorers, "coston2"):
        raise AcceptanceError("product receipt and acceptance explorers do not match")


def _validate_before_new(record: Mapping[str, object], root: Path) -> None:
    disclosure = _approved(record, "before_new")
    counts = (disclosure.get("before_count"), disclosure.get("new_count"))
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in counts):
        raise AcceptanceError("before/new disclosure counts are missing")
    source = _project_file(root, _text(disclosure, "source")).read_text(encoding="utf-8")
    if "Existing before Bounty 1" not in source or "New for Bounty 1" not in source:
        raise AcceptanceError("before/new disclosure headings are missing")


def validate_record(record: Mapping[str, object], selected: str, root: Path) -> None:
    if record.get("schema_version") != 1 or record.get("status") != "completed":
        raise AcceptanceError("acceptance record is incomplete")
    _validate_selected_ui(record, selected)
    xrpl_hash = _validate_xaman(record)
    _validate_demo(record, root)
    _validate_explorers(record, xrpl_hash)
    _validate_recovery(record)
    addresses = _validate_product_deployment(record)
    _validate_product_smoke(record, root, addresses)
    _validate_before_new(record, root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("record", type=Path)
    parser.add_argument("--selected-proposal", required=True)
    arguments = parser.parse_args()
    try:
        value = json.loads(arguments.record.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise AcceptanceError("acceptance record must be an object")
        validate_record(value, arguments.selected_proposal, Path.cwd())
    except (OSError, ValueError, AcceptanceError):
        return 1
    print("ACCEPTANCE_RECORD_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
