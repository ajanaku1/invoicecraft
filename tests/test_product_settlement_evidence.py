from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.xrp.instructions import keccak256
from scripts.verify_product_settlement import ProductEvidenceError, validate_product_settlement


SETTLEMENT = "0x" + "11" * 20
ADAPTER = "0x" + "22" * 20
USD0 = "0xc1a5b41512496b80903d1f32d6dea3a73212e71f"
FXRP = "0x0b6a3645c240605887a5532109323a3e12273dc7"
BENEFICIARY = "0x" + "55" * 20
TX_HASH = "0x" + "66" * 32
SETTLEMENT_ID = "0x" + "77" * 32
INVOICE_HASH = "0x" + "88" * 32
XRPL_HASH = "99" * 32


def word(value: int) -> str:
    return value.to_bytes(32, "big").hex()


def address_topic(value: str) -> str:
    return "0x" + "00" * 12 + value[2:].lower()


def event_topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii")).hex()


def evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "paid",
        "network": "coston2",
        "canonical_hash": INVOICE_HASH,
        "payout": {"beneficiary": BENEFICIARY, "currency": "USD₮0", "amount_uba": 75_000_000, "token": USD0},
        "xrpl": {"transaction_hash": XRPL_HASH, "explorer_url": f"https://testnet.xrpl.org/transactions/{XRPL_HASH}"},
        "flare": {
            "transaction_hash": TX_HASH,
            "block_number": 123,
            "settlement_contract": SETTLEMENT,
            "settlement_id": SETTLEMENT_ID,
            "fxrp_input_uba": 80_000_000,
            "explorer_url": f"https://coston2-explorer.flare.network/tx/{TX_HASH}",
        },
        "liquidity": {"label": "Test liquidity — not a real Coston2 market", "adapter": ADAPTER, "event_index": 1},
    }


def settlement_event() -> dict[str, object]:
    return {
        "address": SETTLEMENT,
        "logIndex": "0x0",
        "topics": [
            event_topic("InvoiceSettled(bytes32,bytes32,address,uint256,uint256)"),
            SETTLEMENT_ID,
            INVOICE_HASH,
            address_topic(BENEFICIARY),
        ],
        "data": "0x" + word(80_000_000) + word(75_000_000),
    }


def adapter_event() -> dict[str, object]:
    return {
        "address": ADAPTER,
        "logIndex": "0x1",
        "topics": [
            event_topic("TestLiquidityUsed(bytes32,address,uint256,uint256)"),
            SETTLEMENT_ID,
            address_topic(BENEFICIARY),
        ],
        "data": "0x" + word(80_000_000) + word(75_000_000),
    }


def transfer_event() -> dict[str, object]:
    return {
        "address": USD0,
        "logIndex": "0x2",
        "topics": [
            event_topic("Transfer(address,address,uint256)"),
            address_topic(ADAPTER),
            address_topic(BENEFICIARY),
        ],
        "data": "0x" + word(75_000_000),
    }


def event_logs() -> list[dict[str, object]]:
    return [settlement_event(), adapter_event(), transfer_event()]


def abi_address(value: str) -> str:
    return address_topic(value)


def abi_string(value: str) -> str:
    encoded = value.encode("utf-8")
    padding = (32 - len(encoded) % 32) % 32
    return "0x" + word(32) + word(len(encoded)) + (encoded + b"\0" * padding).hex()


class Transport:
    def __init__(self, logs: list[dict[str, object]] | None = None) -> None:
        self.logs = logs or event_logs()

    def __call__(self, _url: str, request: dict[str, object], _timeout: int) -> Mapping[str, object]:
        method = request["method"]
        params = request["params"]
        if method == "eth_chainId":
            result: object = "0x72"
        elif method == "eth_getCode":
            result = "0x6000"
        elif method == "eth_getTransactionReceipt":
            result = {"transactionHash": TX_HASH, "status": "0x1", "blockNumber": "0x7b", "logs": self.logs}
        elif method == "eth_call":
            result = self.eth_call(params)
        else:
            raise AssertionError(method)
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    def eth_call(self, params: object) -> str:
        call = params[0]
        target, data = call["to"].lower(), call["data"]
        selector = data[:10]
        getter = self._address_getter(target, selector)
        if getter is not None:
            return getter
        metadata = self._token_metadata(target, selector)
        return metadata or abi_string("TEST LIQUIDITY - NOT A REAL COSTON2 MARKET")

    def _address_getter(self, target: str, selector: str) -> str | None:
        getters = {
            (SETTLEMENT, "fxrp()"): FXRP,
            (SETTLEMENT, "usd0()"): USD0,
            (SETTLEMENT, "adapter()"): ADAPTER,
            (ADAPTER, "fxrp()"): FXRP,
            (ADAPTER, "usd0()"): USD0,
            (ADAPTER, "authorizedSettlement()"): SETTLEMENT,
        }
        for (address, signature), value in getters.items():
            if target == address and selector == "0x" + keccak256(signature.encode("ascii"))[:4].hex():
                return abi_address(value)
        return None

    def _token_metadata(self, target: str, selector: str) -> str | None:
        metadata = {
            USD0: ("USDT0 test", "USD₮0"),
            FXRP: ("FXRP", "FTestXRP"),
        }
        if target not in metadata:
            return None
        name, symbol = metadata[target]
        values = {"name()": abi_string(name), "symbol()": abi_string(symbol), "decimals()": "0x" + word(6)}
        for signature, value in values.items():
            expected = "0x" + keccak256(signature.encode("ascii"))[:4].hex()
            if selector == expected:
                return value
        return None


class MetadataTransport(Transport):
    def __init__(self, signature: str, result: str, target: str = USD0) -> None:
        super().__init__()
        self.signature = signature
        self.result = result
        self.target = target

    def eth_call(self, params: object) -> str:
        call = params[0]
        selector = "0x" + keccak256(self.signature.encode("ascii"))[:4].hex()
        if call["to"].lower() == self.target and call["data"][:10] == selector:
            return self.result
        return super().eth_call(params)


def test_product_receipt_validates_contract_bindings_and_exact_transfer() -> None:
    validate_product_settlement(evidence(), 3, Transport())


@pytest.mark.parametrize(
    ("signature", "result"),
    [
        ("name()", abi_string("Impostor token")),
        ("symbol()", abi_string("USDC")),
        ("decimals()", "0x" + word(18)),
    ],
)
def test_product_receipt_rejects_noncanonical_usdt0_metadata(
    signature: str, result: str
) -> None:
    with pytest.raises(ProductEvidenceError, match="USD₮0"):
        validate_product_settlement(evidence(), 3, MetadataTransport(signature, result))


def test_product_receipt_rejects_noncanonical_fxrp_metadata() -> None:
    with pytest.raises(ProductEvidenceError, match="FXRP"):
        validate_product_settlement(
            evidence(),
            3,
            MetadataTransport("symbol()", abi_string("FAKE"), target=FXRP),
        )


@pytest.mark.parametrize("tamper", ["amount", "beneficiary", "adapter_event", "transfer", "explorer"])
def test_product_receipt_rejects_changed_live_evidence(tamper: str) -> None:
    value = evidence()
    logs = event_logs()
    if tamper == "amount":
        value["payout"]["amount_uba"] = 74_000_000
    elif tamper == "beneficiary":
        value["payout"]["beneficiary"] = "0x" + "aa" * 20
    elif tamper == "adapter_event":
        logs[1]["address"] = "0x" + "aa" * 20
    elif tamper == "transfer":
        logs.pop()
    else:
        value["flare"]["explorer_url"] = "https://evil.example/tx/" + TX_HASH

    with pytest.raises(ProductEvidenceError):
        validate_product_settlement(value, 3, Transport(logs))
