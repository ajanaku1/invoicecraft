// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

import {InvoiceSettlement} from "./InvoiceSettlement.sol";
import {TestLiquidityAdapter} from "./TestLiquidityAdapter.sol";

/// @notice One-transaction Coston2 bootstrap for the explicitly test-only path.
contract InvoiceSettlementDeployment {
    TestLiquidityAdapter public immutable adapter;
    InvoiceSettlement public immutable settlement;

    event ProductContractsDeployed(address indexed settlement, address indexed adapter);

    constructor(address fxrp, address usd0, uint256 perSettlementUsd0Cap, uint256 lifetimeUsd0Cap) {
        TestLiquidityAdapter createdAdapter =
            new TestLiquidityAdapter(fxrp, usd0, perSettlementUsd0Cap, lifetimeUsd0Cap, address(this));
        InvoiceSettlement createdSettlement = new InvoiceSettlement(fxrp, usd0, address(createdAdapter));
        createdAdapter.setAuthorizedSettlement(address(createdSettlement));
        adapter = createdAdapter;
        settlement = createdSettlement;
        emit ProductContractsDeployed(address(createdSettlement), address(createdAdapter));
    }
}
