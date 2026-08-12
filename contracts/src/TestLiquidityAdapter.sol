// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

interface IERC20Transfer {
    function transfer(address recipient, uint256 amount) external returns (bool);
}

contract TestLiquidityAdapter {
    string public constant label = "TEST LIQUIDITY - NOT A REAL COSTON2 MARKET";

    error Unauthorized();
    error InvalidConfiguration();
    error Expired();
    error CapExceeded();
    error TransferFailed();
    error ReentrantCall();

    address public immutable fxrp;
    address public immutable usd0;
    address public immutable administrator;
    uint256 public immutable perSettlementUsd0Cap;
    uint256 public immutable lifetimeUsd0Cap;
    address public authorizedSettlement;
    uint256 public totalUsd0Out;
    bool private entered;

    event SettlementAuthorized(address indexed settlement);
    event TestLiquidityUsed(
        bytes32 indexed settlementId, address indexed beneficiary, uint256 fxrpInput, uint256 usd0Output
    );

    constructor(
        address fxrp_,
        address usd0_,
        uint256 perSettlementUsd0Cap_,
        uint256 lifetimeUsd0Cap_,
        address administrator_
    ) {
        if (
            fxrp_ == address(0) || usd0_ == address(0) || administrator_ == address(0) || perSettlementUsd0Cap_ == 0
                || lifetimeUsd0Cap_ < perSettlementUsd0Cap_
        ) revert InvalidConfiguration();
        fxrp = fxrp_;
        usd0 = usd0_;
        administrator = administrator_;
        perSettlementUsd0Cap = perSettlementUsd0Cap_;
        lifetimeUsd0Cap = lifetimeUsd0Cap_;
    }

    function setAuthorizedSettlement(address settlement) external {
        if (msg.sender != administrator || authorizedSettlement != address(0)) revert Unauthorized();
        if (settlement == address(0) || settlement.code.length == 0) revert InvalidConfiguration();
        authorizedSettlement = settlement;
        emit SettlementAuthorized(settlement);
    }

    function swapExactOutput(
        bytes32 settlementId,
        uint256 fxrpInput,
        uint256 usd0Output,
        address beneficiary,
        uint256 deadline
    ) external returns (uint256) {
        if (msg.sender != authorizedSettlement) revert Unauthorized();
        if (entered) revert ReentrantCall();
        if (block.timestamp > deadline) revert Expired();
        if (fxrpInput == 0 || usd0Output == 0 || beneficiary == address(0)) revert InvalidConfiguration();
        if (usd0Output > perSettlementUsd0Cap || totalUsd0Out + usd0Output > lifetimeUsd0Cap) {
            revert CapExceeded();
        }
        entered = true;
        totalUsd0Out += usd0Output;
        _safeTransfer(usd0, beneficiary, usd0Output);
        entered = false;
        emit TestLiquidityUsed(settlementId, beneficiary, fxrpInput, usd0Output);
        return usd0Output;
    }

    function _safeTransfer(address token, address recipient, uint256 amount) private {
        (bool success, bytes memory result) = token.call(abi.encodeCall(IERC20Transfer.transfer, (recipient, amount)));
        if (!success || (result.length != 0 && !abi.decode(result, (bool)))) revert TransferFailed();
    }
}
