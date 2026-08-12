// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;

import {InvoiceSettlement} from "../src/InvoiceSettlement.sol";
import {InvoiceSettlementDeployment} from "../src/InvoiceSettlementDeployment.sol";
import {TestLiquidityAdapter} from "../src/TestLiquidityAdapter.sol";

interface Vm {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }
    function recordLogs() external;
    function getRecordedLogs() external returns (Log[] memory);
}

contract MockToken {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address account, uint256 amount) external {
        balanceOf[account] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(balanceOf[from] >= amount && allowance[from][msg.sender] >= amount, "allowance");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract ExternalCaller {
    function setSettlement(TestLiquidityAdapter adapter, address settlement) external {
        adapter.setAuthorizedSettlement(settlement);
    }

    function useAdapter(TestLiquidityAdapter adapter) external {
        adapter.swapExactOutput(bytes32(0), 1, 1, address(this), type(uint256).max);
    }
}

contract InvoiceSettlementTest {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    bytes32 private constant USED_TOPIC = keccak256("TestLiquidityUsed(bytes32,address,uint256,uint256)");
    MockToken private fxrp;
    MockToken private usd0;
    TestLiquidityAdapter private adapter;
    InvoiceSettlement private settlement;
    address private beneficiary = address(0xBEEF);
    bytes32 private invoiceHash = keccak256("invoice-1");

    function setUp() public {
        fxrp = new MockToken();
        usd0 = new MockToken();
        adapter = new TestLiquidityAdapter(address(fxrp), address(usd0), 100e6, 500e6, address(this));
        settlement = new InvoiceSettlement(address(fxrp), address(usd0), address(adapter));
        adapter.setAuthorizedSettlement(address(settlement));
        fxrp.mint(address(this), 50e6);
        usd0.mint(address(adapter), 500e6);
        fxrp.approve(address(settlement), type(uint256).max);
    }

    function testSettlesExactBoundOutputOnce() public {
        bytes32 settlementId = settlement.termsHash(invoiceHash, beneficiary, 50e6, 30e6, type(uint256).max);
        vm.recordLogs();
        settlement.settle(settlementId, invoiceHash, beneficiary, 50e6, 30e6, type(uint256).max, 25e6);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        require(usd0.balanceOf(beneficiary) == 50e6, "exact payout");
        require(fxrp.balanceOf(address(adapter)) == 25e6, "adapter input");
        require(settlement.used(settlementId), "single-use marker");
        require(_adapterEventId(logs) == settlementId, "adapter event binding");
        (bool ok,) = address(settlement)
            .call(
                abi.encodeCall(
                    settlement.settle, (settlementId, invoiceHash, beneficiary, 50e6, 30e6, type(uint256).max, 25e6)
                )
            );
        require(!ok, "replay accepted");
    }

    function _adapterEventId(Vm.Log[] memory logs) private view returns (bytes32) {
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(adapter) && logs[i].topics[0] == USED_TOPIC) {
                return logs[i].topics[1];
            }
        }
        return bytes32(0);
    }

    function testRejectsChangedBeneficiaryExpiredTermsAndInputAboveCap() public {
        bytes32 bound = settlement.termsHash(invoiceHash, beneficiary, 50e6, 30e6, type(uint256).max);
        (bool changed,) = address(settlement)
            .call(
                abi.encodeCall(
                    settlement.settle, (bound, invoiceHash, address(0xCAFE), 50e6, 30e6, type(uint256).max, 25e6)
                )
            );
        bytes32 expired = settlement.termsHash(invoiceHash, beneficiary, 50e6, 30e6, 0);
        (bool stale,) = address(settlement)
            .call(abi.encodeCall(settlement.settle, (expired, invoiceHash, beneficiary, 50e6, 30e6, 0, 25e6)));
        (bool over,) = address(settlement)
            .call(
                abi.encodeCall(
                    settlement.settle, (bound, invoiceHash, beneficiary, 50e6, 30e6, type(uint256).max, 31e6)
                )
            );
        require(!changed && !stale && !over, "unsafe settlement accepted");
    }

    function testAdapterIsLabelledAuthorizedCappedAndSingleConfigured() public {
        require(
            keccak256(bytes(adapter.label())) == keccak256(bytes("TEST LIQUIDITY - NOT A REAL COSTON2 MARKET")), "label"
        );
        ExternalCaller caller = new ExternalCaller();
        (bool unauthorized,) = address(caller).call(abi.encodeCall(caller.useAdapter, adapter));
        (bool reset,) = address(caller).call(abi.encodeCall(caller.setSettlement, (adapter, address(caller))));
        bytes32 overCapId = settlement.termsHash(invoiceHash, beneficiary, 101e6, 30e6, type(uint256).max);
        (bool overCap,) = address(settlement)
            .call(
                abi.encodeCall(
                    settlement.settle, (overCapId, invoiceHash, beneficiary, 101e6, 30e6, type(uint256).max, 25e6)
                )
            );
        require(!unauthorized && !reset && !overCap, "adapter guard failed");
    }

    function testDeploymentBootstrapBindsAndAuthorizesProductContracts() public {
        InvoiceSettlementDeployment deployment =
            new InvoiceSettlementDeployment(address(fxrp), address(usd0), 100e6, 500e6);
        TestLiquidityAdapter deployedAdapter = deployment.adapter();
        InvoiceSettlement deployedSettlement = deployment.settlement();

        require(deployedAdapter.fxrp() == address(fxrp), "FXRP binding");
        require(deployedAdapter.usd0() == address(usd0), "USD0 binding");
        require(deployedAdapter.administrator() == address(deployment), "admin binding");
        require(deployedAdapter.authorizedSettlement() == address(deployedSettlement), "authorization");
        require(address(deployedSettlement.adapter()) == address(deployedAdapter), "adapter binding");
        require(address(deployedSettlement.fxrp()) == address(fxrp), "settlement FXRP");
        require(address(deployedSettlement.usd0()) == address(usd0), "settlement USD0");
    }
}
