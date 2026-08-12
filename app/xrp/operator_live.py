"""Trusted Testnet RPC and Phase 0 adapters for the browser operator queue."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from . import live
from .executor import (
    ExecutorError,
    FsaExecutionResult,
    SettlementUserOperationBuilder,
    XrplPaymentEvidence,
)
from .instructions import keccak256
from .operator import OperatorError, OperatorPreparation, OperatorProgress
from .rpc import (
    TRUSTED_XRPL_TESTNET_RPC_URL,
    JsonRpcClient,
    RpcEvidenceError,
    Transport,
    _packed_user_operation_identity,
)


JsonObject = Mapping[str, object]
COSTON2_FXRP = "0x0b6a3645c240605887a5532109323a3e12273dc7"
COSTON2_USD0 = "0xc1a5b41512496b80903d1f32d6dea3a73212e71f"


@dataclass(frozen=True)
class Coston2OperatorConfig:
    signer: str
    verifier_url: str
    verifier_api_key: str
    da_layer_url: str
    settlement_contract: str
    adapter: str
    fxrp_token: str
    usd0_token: str
    timeout_seconds: int = 15

    def __post_init__(self) -> None:
        for value in (
            self.signer,
            self.settlement_contract,
            self.adapter,
            self.fxrp_token,
            self.usd0_token,
        ):
            _address(value)
        if not self.verifier_api_key:
            raise OperatorError("FDC verifier API key is not configured")
        if self.fxrp_token.lower() != COSTON2_FXRP or self.usd0_token.lower() != COSTON2_USD0:
            raise OperatorError("operator assets are not the canonical Coston2 test tokens")
        if self.timeout_seconds <= 0:
            raise OperatorError("operator timeout must be positive")


class TrustedXrplPaymentReader:
    def __init__(
        self, timeout_seconds: int = 15, transport: Transport | None = None
    ) -> None:
        self.client = JsonRpcClient(
            TRUSTED_XRPL_TESTNET_RPC_URL, timeout_seconds, transport
        )

    def read(self, transaction_hash: str) -> XrplPaymentEvidence:
        normalized = _xrpl_hash(transaction_hash)
        try:
            server = _mapping(self.client.request("server_info"), "server_info")
            info = _mapping(server.get("info"), "server info")
            if info.get("network_id") != 1:
                raise OperatorError("XRPL RPC is not Testnet")
            current = _positive_int(
                _mapping(info.get("validated_ledger"), "validated ledger").get("seq"),
                "validated ledger sequence",
            )
            transaction = _mapping(
                self.client.request(
                    "tx", [{"transaction": normalized, "binary": False}]
                ),
                "XRPL transaction",
            )
            evidence = self._evidence(transaction)
            if evidence.transaction_hash != normalized:
                raise OperatorError("XRPL transaction hash does not match")
            if current - evidence.ledger_index + 1 < 3:
                raise OperatorError(
                    "XRPL payment has fewer than three validated-ledger confirmations"
                )
            return evidence
        except RpcEvidenceError as error:
            raise OperatorError("trusted XRPL evidence is unavailable") from error

    @staticmethod
    def _evidence(transaction: JsonObject) -> XrplPaymentEvidence:
        if transaction.get("TransactionType") != "Payment":
            raise OperatorError("XRPL transaction is not a Payment")
        metadata = _mapping(transaction.get("meta"), "XRPL metadata")
        memos = transaction.get("Memos")
        if not isinstance(memos, list) or len(memos) != 1:
            raise OperatorError("XRPL payment memo is malformed")
        memo = _mapping(_mapping(memos[0], "XRPL memo").get("Memo"), "XRPL memo")
        return XrplPaymentEvidence(
            transaction_hash=_xrpl_hash(_text(transaction.get("hash"), "transaction hash")),
            validated=transaction.get("validated") is True,
            result=_text(metadata.get("TransactionResult"), "transaction result"),
            source_account=_text(transaction.get("Account"), "source account"),
            destination=_text(transaction.get("Destination"), "destination"),
            amount_drops=_decimal(transaction.get("Amount"), "payment amount"),
            delivered_amount_drops=_decimal(
                metadata.get("delivered_amount"), "delivered amount"
            ),
            memo_data_hex=_hex_text(memo.get("MemoData"), "memo data"),
            destination_tag=_optional_int(transaction.get("DestinationTag")),
            flags=_nonnegative_int(transaction.get("Flags", 0), "payment flags"),
            ledger_index=_positive_int(transaction.get("ledger_index"), "ledger index"),
            ledger_timestamp=_positive_int(transaction.get("date"), "ledger timestamp")
            + 946_684_800,
            fdc_round_id=None,
            fdc_proof_hash=None,
        )


class _MemoryStateStore:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = dict(state)

    def read(self) -> dict[str, object]:
        return dict(self.state)

    def write(self, state: Mapping[str, object]) -> None:
        self.state = dict(state)

    def write_sign_request(
        self, _name: str, _request: Mapping[str, str]
    ) -> None:
        return None


class LiveSettlementUserOperationBuilder:
    """Build with the current onchain FSA nonce instead of configured mutable state."""

    def __init__(
        self,
        fxrp_token: str,
        settlement_contract: str,
        timeout_seconds: int = 15,
    ) -> None:
        self.fxrp_token = _address(fxrp_token)
        self.settlement_contract = _address(settlement_contract)
        self.timeout_seconds = timeout_seconds

    def build(self, invoice: dict[str, object], source_account: str) -> bytes:
        try:
            _, flare = live._trusted_clients(self.timeout_seconds)
            live._validate_flare_chain(flare)
            controller = live._registry(flare, "MasterAccountController")
            personal = live._eth_address_call(
                flare,
                controller,
                live._selector("getPersonalAccount(string)")
                + live._abi_string(source_account)[2:],
            )
            nonce_call = live._selector("getNonce(address)") + live._abi_address(
                personal
            ).hex()
            nonce = live._eth_quantity_call(flare, controller, nonce_call)
            return SettlementUserOperationBuilder(
                personal,
                nonce,
                self.fxrp_token,
                self.settlement_contract,
            ).build(invoice)
        except (live.LiveError, RpcEvidenceError) as error:
            raise ExecutorError("current FSA nonce is unavailable") from error


class LiveCoston2OperatorBackend:
    """Adapt proven Phase 0 functions to invoice-scoped public job state."""

    def __init__(self, config: Coston2OperatorConfig) -> None:
        self.config = config
        self.live_config = live.LiveConfig(
            "",
            "",
            "",
            config.signer,
            config.verifier_url,
            config.verifier_api_key,
            config.da_layer_url,
        )

    def start(
        self, invoice: dict[str, object], evidence: XrplPaymentEvidence
    ) -> dict[str, object]:
        packed = _packed_user_operation(invoice)
        personal, nonce = _packed_user_operation_identity(packed)
        xrpl, flare = live._trusted_clients(self.config.timeout_seconds)
        live._validate_networks(xrpl, flare)
        contracts = live._resolve_contracts(flare)
        settings = live._live_settings(flare, contracts["asset_manager"])
        return {
            "version": live.STATE_VERSION,
            "stage": "poll-xaman",
            "xrpl_address": evidence.source_account,
            "signer": self.config.signer,
            "personal_account": personal,
            "nonce": str(nonce),
            "packed_user_operation_hex": packed.hex(),
            "memo_data_hex": evidence.memo_data_hex,
            "gross_drops": evidence.delivered_amount_drops,
            "contracts": contracts,
            "settings": settings,
            "xrpl_transaction_hash": evidence.transaction_hash,
            "xrpl_validated_ledger_index": evidence.ledger_index,
            "product": _product_context(invoice, self.config),
        }

    def prepare_fdc(self, context: dict[str, object]) -> OperatorPreparation:
        store = _MemoryStateStore(context)
        request = live.prepare_fdc(
            self.live_config, store, self.config.timeout_seconds
        )
        return OperatorPreparation(dict(request), store.read())

    def record_fdc(
        self, context: dict[str, object], transaction_hash: str
    ) -> OperatorProgress:
        store = _MemoryStateStore(context)
        state = live.record_fdc(
            self.live_config, store, transaction_hash, self.config.timeout_seconds
        )
        return OperatorProgress(
            store.read(), _positive_decimal_text(state.get("fdc_round_id"), "FDC round")
        )

    def prepare_execute(self, context: dict[str, object]) -> OperatorPreparation:
        store = _MemoryStateStore(context)
        request = live.prepare_execute(
            self.live_config, store, self.config.timeout_seconds
        )
        state = store.read()
        proof_hash = _hash(state.get("fdc_response_hash"), "FDC response hash")
        return OperatorPreparation(dict(request), state, proof_hash)

    def finalize(
        self, context: dict[str, object], transaction_hash: str
    ) -> FsaExecutionResult:
        store = _MemoryStateStore(context)
        live.finalize(
            self.live_config, store, transaction_hash, self.config.timeout_seconds
        )
        _, flare = live._trusted_clients(self.config.timeout_seconds)
        receipt = _mapping(
            flare.request("eth_getTransactionReceipt", [transaction_hash]),
            "Coston2 receipt",
        )
        return _settlement_outcome(
            _mapping(context.get("product"), "product context"),
            transaction_hash,
            receipt,
        )


def _product_context(
    invoice: dict[str, object], config: Coston2OperatorConfig
) -> dict[str, object]:
    quote = _mapping(invoice.get("quote"), "quote")
    return {
        "canonical_hash": _hash(invoice.get("canonical_hash"), "canonical hash"),
        "beneficiary": _address(_text(invoice.get("beneficiary"), "beneficiary")),
        "exact_usd0_uba": _invoice_total_uba(invoice),
        "fxrp_input_uba": _positive_int(quote.get("net_mint_uba"), "FXRP input"),
        "settlement_contract": config.settlement_contract,
        "adapter": config.adapter,
        "fxrp_token": config.fxrp_token,
        "usd0_token": config.usd0_token,
    }


def _settlement_outcome(
    product: JsonObject, transaction_hash: str, receipt: JsonObject
) -> FsaExecutionResult:
    if receipt.get("status") != "0x1":
        raise OperatorError("product settlement transaction did not succeed")
    if str(receipt.get("transactionHash", "")).lower() != transaction_hash.lower():
        raise OperatorError("product settlement receipt hash does not match")
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise OperatorError("product settlement receipt has no logs")
    settlement_log = _matching_settlement_log(logs, product)
    settlement_id = _text(_topics(settlement_log)[1], "settlement ID")
    adapter_log = _matching_adapter_log(logs, product, settlement_id)
    _require_transfer_log(logs, product)
    return FsaExecutionResult(
        status="succeeded",
        flare_transaction_hash=_hash(transaction_hash, "Coston2 transaction hash"),
        flare_block_number=_quantity(receipt.get("blockNumber"), "block number"),
        settlement_contract=_address(
            _text(product.get("settlement_contract"), "settlement contract")
        ),
        adapter=_address(_text(product.get("adapter"), "adapter")),
        usd0_token=_address(_text(product.get("usd0_token"), "USD0 token")),
        beneficiary=_address(_text(product.get("beneficiary"), "beneficiary")),
        usd0_amount=_positive_int(product.get("exact_usd0_uba"), "USD0 output"),
        settlement_id=_hash(settlement_id, "settlement ID"),
        adapter_event_index=_quantity(adapter_log.get("logIndex"), "adapter log index"),
    )


def _matching_settlement_log(
    logs: list[object], product: JsonObject
) -> JsonObject:
    expected_topics = (
        _topic("InvoiceSettled(bytes32,bytes32,address,uint256,uint256)"),
        None,
        _hash(product.get("canonical_hash"), "canonical hash"),
        _address_topic(_text(product.get("beneficiary"), "beneficiary")),
    )
    expected_data = _event_data(product)
    return _find_log(
        logs,
        _text(product.get("settlement_contract"), "settlement contract"),
        expected_topics,
        expected_data,
        "InvoiceSettled",
    )


def _matching_adapter_log(
    logs: list[object], product: JsonObject, settlement_id: str
) -> JsonObject:
    topics = (
        _topic("TestLiquidityUsed(bytes32,address,uint256,uint256)"),
        settlement_id,
        _address_topic(_text(product.get("beneficiary"), "beneficiary")),
    )
    return _find_log(
        logs,
        _text(product.get("adapter"), "adapter"),
        topics,
        _event_data(product),
        "TestLiquidityUsed",
    )


def _require_transfer_log(logs: list[object], product: JsonObject) -> None:
    topics = (
        _topic("Transfer(address,address,uint256)"),
        _address_topic(_text(product.get("adapter"), "adapter")),
        _address_topic(_text(product.get("beneficiary"), "beneficiary")),
    )
    _find_log(
        logs,
        _text(product.get("usd0_token"), "USD0 token"),
        topics,
        "0x" + _word(_positive_int(product.get("exact_usd0_uba"), "USD0 output")),
        "USD0 Transfer",
    )


def _find_log(
    logs: list[object],
    address: str,
    expected_topics: tuple[str | None, ...],
    data: str,
    label: str,
) -> JsonObject:
    for value in logs:
        if not isinstance(value, Mapping):
            continue
        topics = value.get("topics")
        if not isinstance(topics, list) or len(topics) != len(expected_topics):
            continue
        topic_match = all(
            expected is None
            or (isinstance(actual, str) and actual.lower() == expected.lower())
            for actual, expected in zip(topics, expected_topics)
        )
        if (
            str(value.get("address", "")).lower() == address.lower()
            and str(value.get("data", "")).lower() == data.lower()
            and topic_match
        ):
            return value
    raise OperatorError(f"product settlement lacks exact {label} evidence")


def _event_data(product: JsonObject) -> str:
    fxrp = _positive_int(product.get("fxrp_input_uba"), "FXRP input")
    usd0 = _positive_int(product.get("exact_usd0_uba"), "USD0 output")
    return "0x" + _word(fxrp) + _word(usd0)


def _topics(log: JsonObject) -> list[object]:
    value = log.get("topics")
    if not isinstance(value, list):
        raise OperatorError("product settlement topics are malformed")
    return value


def _packed_user_operation(invoice: JsonObject) -> bytes:
    evidence = _mapping(invoice.get("fsa_evidence"), "FSA evidence")
    value = _text(evidence.get("packed_user_operation_hex"), "PackedUserOperation")
    try:
        raw = bytes.fromhex(value.removeprefix("0x"))
    except ValueError as error:
        raise OperatorError("PackedUserOperation is malformed") from error
    if not raw:
        raise OperatorError("PackedUserOperation is malformed")
    return raw


def _invoice_total_uba(invoice: JsonObject) -> int:
    details = _mapping(invoice.get("invoice"), "invoice")
    try:
        units = Decimal(str(details["total"])) * Decimal(1_000_000)
    except (InvalidOperation, KeyError) as error:
        raise OperatorError("invoice total is malformed") from error
    if units <= 0 or units != units.to_integral_value():
        raise OperatorError("invoice total cannot be represented exactly")
    return int(units)


def _mapping(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise OperatorError(f"{label} is malformed")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OperatorError(f"{label} is malformed")
    return value


def _decimal(value: object, label: str) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise OperatorError(f"{label} is malformed")
    return int(value)


def _positive_decimal_text(value: object, label: str) -> int:
    parsed = _decimal(value, label)
    if parsed <= 0:
        raise OperatorError(f"{label} is malformed")
    return parsed


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperatorError(f"{label} is malformed")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperatorError(f"{label} is malformed")
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else _nonnegative_int(value, "destination tag")


def _quantity(value: object, label: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value) is None:
        raise OperatorError(f"{label} is malformed")
    return int(value, 16)


def _xrpl_hash(value: str) -> str:
    raw = value.removeprefix("0x").removeprefix("0X")
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw) is None:
        raise OperatorError("XRPL transaction hash is malformed")
    return raw.upper()


def _hash(value: object, label: str) -> str:
    text = _text(value, label)
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", text) is None:
        raise OperatorError(f"{label} is malformed")
    return text.lower()


def _hex_text(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) % 2 or re.fullmatch(r"[0-9a-fA-F]+", text) is None:
        raise OperatorError(f"{label} is malformed")
    return text.upper()


def _address(value: str) -> str:
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None or int(value[2:], 16) == 0:
        raise OperatorError("Coston2 address is malformed")
    return value.lower()


def _topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii")).hex()


def _address_topic(value: str) -> str:
    return "0x" + "00" * 12 + _address(value)[2:]


def _word(value: int) -> str:
    return value.to_bytes(32, "big").hex()
