// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

interface IERC20Settlement {
    function balanceOf(address account) external view returns (uint256);
    function transferFrom(address sender, address recipient, uint256 amount) external returns (bool);
}

interface ITestLiquidityAdapter {
    function swapExactOutput(
        bytes32 settlementId,
        uint256 fxrpInput,
        uint256 usd0Output,
        address beneficiary,
        uint256 deadline
    ) external returns (uint256);
}

contract InvoiceSettlement {
    error InvalidConfiguration();
    error InvalidTerms();
    error Expired();
    error AlreadySettled();
    error InputCapExceeded();
    error TransferFailed();
    error InexactPayout();
    error ReentrantCall();

    IERC20Settlement public immutable fxrp;
    IERC20Settlement public immutable usd0;
    ITestLiquidityAdapter public immutable adapter;
    mapping(bytes32 => bool) public used;
    bool private entered;

    event InvoiceSettled(
        bytes32 indexed settlementId,
        bytes32 indexed invoiceHash,
        address indexed beneficiary,
        uint256 fxrpInput,
        uint256 usd0Output
    );

    constructor(address fxrp_, address usd0_, address adapter_) {
        if (
            fxrp_ == address(0) || usd0_ == address(0) || adapter_ == address(0) || fxrp_.code.length == 0
                || usd0_.code.length == 0 || adapter_.code.length == 0
        ) revert InvalidConfiguration();
        fxrp = IERC20Settlement(fxrp_);
        usd0 = IERC20Settlement(usd0_);
        adapter = ITestLiquidityAdapter(adapter_);
    }

    function termsHash(
        bytes32 invoiceHash,
        address beneficiary,
        uint256 exactUsd0,
        uint256 maximumFxrp,
        uint256 deadline
    ) external view returns (bytes32) {
        return _termsHash(invoiceHash, beneficiary, exactUsd0, maximumFxrp, deadline);
    }

    function settle(
        bytes32 settlementId,
        bytes32 invoiceHash,
        address beneficiary,
        uint256 exactUsd0,
        uint256 maximumFxrp,
        uint256 deadline,
        uint256 fxrpInput
    ) external {
        if (entered) revert ReentrantCall();
        if (block.timestamp > deadline) revert Expired();
        if (beneficiary == address(0) || exactUsd0 == 0 || maximumFxrp == 0 || fxrpInput == 0) {
            revert InvalidTerms();
        }
        if (settlementId != _termsHash(invoiceHash, beneficiary, exactUsd0, maximumFxrp, deadline)) {
            revert InvalidTerms();
        }
        if (used[settlementId]) revert AlreadySettled();
        if (fxrpInput > maximumFxrp) revert InputCapExceeded();
        entered = true;
        used[settlementId] = true;
        uint256 beforeBalance = usd0.balanceOf(beneficiary);
        _safeTransferFrom(address(fxrp), msg.sender, address(adapter), fxrpInput);
        uint256 output = adapter.swapExactOutput(settlementId, fxrpInput, exactUsd0, beneficiary, deadline);
        if (output != exactUsd0 || usd0.balanceOf(beneficiary) - beforeBalance != exactUsd0) {
            revert InexactPayout();
        }
        entered = false;
        emit InvoiceSettled(settlementId, invoiceHash, beneficiary, fxrpInput, exactUsd0);
    }

    function _termsHash(
        bytes32 invoiceHash,
        address beneficiary,
        uint256 exactUsd0,
        uint256 maximumFxrp,
        uint256 deadline
    ) private view returns (bytes32) {
        return keccak256(
            abi.encode(invoiceHash, beneficiary, exactUsd0, maximumFxrp, deadline, block.chainid, address(this))
        );
    }

    function _safeTransferFrom(address token, address sender, address recipient, uint256 amount) private {
        (bool success, bytes memory result) =
            token.call(abi.encodeCall(IERC20Settlement.transferFrom, (sender, recipient, amount)));
        if (!success || (result.length != 0 && !abi.decode(result, (bool)))) revert TransferFailed();
    }
}
