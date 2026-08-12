from __future__ import annotations

import pytest

from scripts.prepare_product_deployment import (
    DEPLOYMENT_PROXY,
    FXRP,
    USDT0,
    DeploymentPlanError,
    build_deployment_plan,
    select_output,
    write_public_request,
)


SIGNER = "0x" + "22" * 20
SALT = "0x" + "11" * 32


def test_plan_binds_canonical_assets_caps_and_predicted_addresses() -> None:
    plan = build_deployment_plan(SIGNER, "0x6000", SALT, 100_000_000, 500_000_000, 75_000_000)

    assert plan["assets"] == {"fxrp": FXRP, "usd0": USDT0}
    assert plan["bootstrap"] == "0xf70a51fc24952a697ef93868da5ad2896b167ed1"
    assert plan["adapter"] == "0x3f58910924211088c30e6d8ea8f376680ac74aa6"
    assert plan["settlement"] == "0x503d2323177e0426d259abb1ada72547b5341d2a"
    assert plan["deployment_request"]["to"] == DEPLOYMENT_PROXY
    assert plan["deployment_request"]["purpose"] == "deploy-product-contracts"
    assert plan["funding_request"]["to"] == USDT0
    assert plan["funding_request"]["purpose"] == "fund-test-liquidity"
    assert plan["funding_request"]["data"].startswith("0xa9059cbb")


@pytest.mark.parametrize("selection", ["plan", "deployment", "funding"])
def test_cli_output_selection_returns_loadable_public_object(selection: str) -> None:
    plan = build_deployment_plan(SIGNER, "0x6000", SALT, 100, 500, 75)

    selected = select_output(plan, selection)

    expected = plan if selection == "plan" else plan[f"{selection}_request"]
    assert selected == expected


def test_public_request_writer_is_private_and_never_overwrites(tmp_path) -> None:
    target = tmp_path / "deployment-request.json"
    value = {"purpose": "deploy-product-contracts", "data": "0x1234"}

    write_public_request(target, value)

    assert target.read_text().endswith("\n")
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(DeploymentPlanError, match="already exists"):
        write_public_request(target, value)


@pytest.mark.parametrize(
    ("salt", "per_cap", "lifetime_cap", "funding"),
    [("0x01", 100, 500, 75), (SALT, 0, 500, 75), (SALT, 501, 500, 75), (SALT, 100, 500, 0)],
)
def test_plan_rejects_malformed_or_unsafe_inputs(
    salt: str, per_cap: int, lifetime_cap: int, funding: int
) -> None:
    with pytest.raises(DeploymentPlanError):
        build_deployment_plan(SIGNER, "0x6000", salt, per_cap, lifetime_cap, funding)
