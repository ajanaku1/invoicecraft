#!/usr/bin/env python3
"""Prepare public, deterministic Coston2 deployment and funding requests."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.xrp.instructions import keccak256
from app.xrp.live import make_sign_request


DEPLOYMENT_PROXY = "0x4e59b44847b379578588920ca78fbf26c0b4956c"
FXRP = "0x0b6a3645c240605887a5532109323a3e12273dc7"
USDT0 = "0xc1a5b41512496b80903d1f32d6dea3a73212e71f"
DEFAULT_SALT = "0x" + keccak256(b"InvoiceCraftXRP Coston2 deployment v1").hex()


class DeploymentPlanError(ValueError):
    """Raised when a deployment plan cannot be safely constructed."""


def _hex(value: str, length: int | None = None) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"0x(?:[0-9A-Fa-f]{2})+", value) is None:
        raise DeploymentPlanError("hex value is malformed")
    raw = bytes.fromhex(value[2:])
    if length is not None and len(raw) != length:
        raise DeploymentPlanError("hex value has the wrong length")
    return raw


def _address_word(value: str) -> bytes:
    raw = _hex(value, 20)
    if raw == b"\0" * 20:
        raise DeploymentPlanError("address cannot be zero")
    return b"\0" * 12 + raw


def _positive(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DeploymentPlanError(f"{label} must be a positive integer")
    return value


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _create2(factory: str, salt: bytes, init_code: bytes) -> str:
    digest = keccak256(b"\xff" + _hex(factory, 20) + salt + keccak256(init_code))
    return "0x" + digest[-20:].hex()


def _create(deployer: str, nonce: int) -> str:
    if not 1 <= nonce <= 0x7F:
        raise DeploymentPlanError("child deployment nonce is unsupported")
    encoded = b"\xd6\x94" + _hex(deployer, 20) + bytes([nonce])
    return "0x" + keccak256(encoded)[-20:].hex()


def _init_code(bytecode: str, per_cap: int, lifetime_cap: int) -> bytes:
    return _hex(bytecode) + b"".join(
        (_address_word(FXRP), _address_word(USDT0), _word(per_cap), _word(lifetime_cap))
    )


def _funding_calldata(adapter: str, amount: int) -> str:
    selector = keccak256(b"transfer(address,uint256)")[:4]
    return "0x" + (selector + _address_word(adapter) + _word(amount)).hex()


def _validate_amounts(per_cap: int, lifetime_cap: int, funding: int) -> None:
    _positive(per_cap, "per-settlement cap")
    _positive(lifetime_cap, "lifetime cap")
    _positive(funding, "funding amount")
    if per_cap > lifetime_cap or funding > lifetime_cap:
        raise DeploymentPlanError("deployment caps or funding are inconsistent")


def _requests(
    signer: str, salt: bytes, init_code: bytes, adapter: str, funding: int
) -> tuple[dict[str, str], dict[str, str]]:
    deployment_data = "0x" + (salt + init_code).hex()
    deployment = make_sign_request(
        signer, DEPLOYMENT_PROXY, 0, deployment_data, "deploy-product-contracts"
    )
    fund = make_sign_request(
        signer, USDT0, 0, _funding_calldata(adapter, funding), "fund-test-liquidity"
    )
    return deployment, fund


def _plan_result(
    salt_hex: str,
    init_code: bytes,
    addresses: tuple[str, str, str],
    caps: tuple[int, int],
    funding: int,
    requests: tuple[dict[str, str], dict[str, str]],
) -> dict[str, object]:
    bootstrap, adapter, settlement = addresses
    deployment, fund = requests
    return {
        "schema_version": 1, "network": "coston2", "chain_id": 114,
        "assets": {"fxrp": FXRP, "usd0": USDT0},
        "caps": {"per_settlement_uba": caps[0], "lifetime_uba": caps[1]},
        "funding_uba": funding, "salt": salt_hex.lower(),
        "init_code_hash": "0x" + keccak256(init_code).hex(),
        "bootstrap": bootstrap, "adapter": adapter, "settlement": settlement,
        "deployment_request": deployment, "funding_request": fund,
        "broadcast": False,
    }


def build_deployment_plan(
    signer: str,
    bytecode: str,
    salt_hex: str,
    per_cap: int,
    lifetime_cap: int,
    funding: int,
) -> dict[str, object]:
    _validate_amounts(per_cap, lifetime_cap, funding)
    salt = _hex(salt_hex, 32)
    init_code = _init_code(bytecode, per_cap, lifetime_cap)
    bootstrap = _create2(DEPLOYMENT_PROXY, salt, init_code)
    adapter, settlement = _create(bootstrap, 1), _create(bootstrap, 2)
    requests = _requests(signer, salt, init_code, adapter, funding)
    return _plan_result(
        salt_hex, init_code, (bootstrap, adapter, settlement),
        (per_cap, lifetime_cap), funding, requests,
    )


def _compiled_bytecode() -> str:
    command = [
        "forge", "inspect", "--root", "contracts",
        "src/InvoiceSettlementDeployment.sol:InvoiceSettlementDeployment", "bytecode",
    ]
    try:
        result = subprocess.run(
            command, cwd=PROJECT_ROOT, check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise DeploymentPlanError("unable to compile deployment bytecode") from error
    return result.stdout.strip()


def select_output(plan: dict[str, object], selection: str) -> dict[str, object]:
    if selection == "plan":
        return plan
    key = f"{selection}_request"
    value = plan.get(key)
    if selection not in {"deployment", "funding"} or not isinstance(value, dict):
        raise DeploymentPlanError("deployment output selection is malformed")
    return value


def write_public_request(path: Path, value: dict[str, object]) -> None:
    """Write a browser-uploadable public request without replacing an existing file."""
    if path.exists() or path.is_symlink():
        raise DeploymentPlanError("output path already exists")
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signer", required=True)
    parser.add_argument("--salt", default=DEFAULT_SALT)
    parser.add_argument("--per-cap-uba", type=int, default=100_000_000)
    parser.add_argument("--lifetime-cap-uba", type=int, default=500_000_000)
    parser.add_argument("--funding-uba", type=int, required=True)
    parser.add_argument("--emit", choices=("plan", "deployment", "funding"), default="plan")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        plan = build_deployment_plan(
            arguments.signer,
            _compiled_bytecode(),
            arguments.salt,
            arguments.per_cap_uba,
            arguments.lifetime_cap_uba,
            arguments.funding_uba,
        )
    except DeploymentPlanError:
        return 1
    selected = select_output(plan, arguments.emit)
    if arguments.output is not None:
        try:
            write_public_request(arguments.output, selected)
        except (DeploymentPlanError, OSError):
            return 1
    else:
        print(json.dumps(selected, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
