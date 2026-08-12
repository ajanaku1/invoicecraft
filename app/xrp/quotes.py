"""Live-parameter quote contracts for XRP invoices."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from .instructions import keccak256
from .rpc import (
    COSTON2_CHAIN_ID,
    FLARE_CONTRACT_REGISTRY,
    TRUSTED_COSTON2_RPC_URL,
    JsonRpcClient,
    Transport,
)

XRP_USD_FEED_ID = "0x015852502f55534400000000000000000000000000"
MAX_FUTURE_PRICE_SKEW_SECONDS = 5


class QuoteError(ValueError):
    """Raised when live quote inputs cannot produce a safe bounded quote."""


@dataclass(frozen=True)
class QuoteParameters:
    xrp_usd_price: Decimal
    price_decimals: int
    price_timestamp: int
    lot_size_uba: int
    minimum_redeem_uba: int
    minimum_fee_uba: int
    fee_bips: int
    memo_executor_fee_uba: int
    core_vault: str
    source_block: str


@dataclass(frozen=True)
class XrpQuote:
    quote_hash: str
    xrp_usd_price: str
    price_decimals: int
    price_timestamp: int
    target_fxrp_uba: int
    maximum_fxrp_uba: int
    net_mint_uba: int
    minting_fee_uba: int
    memo_executor_fee_uba: int
    payment_amount_drops: int
    lot_size_uba: int
    minimum_redeem_uba: int
    core_vault: str
    source_block: str
    expires_at: int
    settlement_deadline_at: int

    def to_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


class QuoteProvider(Protocol):
    def read(self) -> QuoteParameters: ...


class Coston2QuoteProvider:
    def __init__(self, timeout_seconds: int = 15, transport: Transport | None = None) -> None:
        self.client = JsonRpcClient(TRUSTED_COSTON2_RPC_URL, timeout_seconds, transport)

    def read(self) -> QuoteParameters:
        from .live import LiveError
        from .rpc import RpcEvidenceError

        try:
            return self._read()
        except QuoteError:
            raise
        except (LiveError, RpcEvidenceError, KeyError, TypeError, ValueError) as error:
            raise QuoteError("live Coston2 quote parameters are unavailable") from error

    def _read(self) -> QuoteParameters:
        from .live import _eth_call_at, _selector

        chain_id = self.client.request("eth_chainId")
        if not isinstance(chain_id, str) or int(chain_id, 16) != COSTON2_CHAIN_ID:
            raise QuoteError("quote provider is not connected to Coston2")
        block = self.client.request("eth_blockNumber")
        if not isinstance(block, str) or not block.startswith("0x"):
            raise QuoteError("Coston2 quote block is malformed")
        asset_manager = _registry_at(self.client, "AssetManagerFXRP", block)
        ftso = _registry_at(self.client, "FtsoV2", block)
        calldata = _selector("getFeedById(bytes21)") + XRP_USD_FEED_ID[2:] + "00" * 11
        price, decimals, timestamp = _decode_feed(
            _eth_call_at(self.client, ftso, calldata, block)
        )
        settings = _settings_at(self.client, asset_manager, block)
        return _parameters(settings, price, decimals, timestamp, block)


def build_quote(
    invoice_id: str, invoice_amount_usd: Decimal, beneficiary: str,
    parameters: QuoteParameters, now: int, ttl_seconds: int = 120,
    settlement_ttl_seconds: int = 900, slippage_bips: int = 100,
    max_price_age_seconds: int = 120,
) -> XrpQuote:
    _validate_inputs(invoice_id, invoice_amount_usd, beneficiary, parameters, now)
    _validate_quote_windows(
        now, parameters.price_timestamp, max_price_age_seconds,
        ttl_seconds, settlement_ttl_seconds,
    )
    amounts = _quote_amounts(
        invoice_amount_usd, parameters, slippage_bips
    )
    target, maximum, net_mint, fee, payment = amounts
    windows = (now + ttl_seconds, now + settlement_ttl_seconds)
    quote_hash = _quote_hash(
        invoice_id,
        invoice_amount_usd,
        beneficiary,
        maximum,
        payment,
        windows[0],
        windows[1],
        parameters,
    )
    return _quote_result(quote_hash, parameters, amounts, windows)


def _validate_quote_windows(
    now: int,
    price_timestamp: int,
    max_price_age: int,
    payer_ttl: int,
    settlement_ttl: int,
) -> None:
    price_age = now - price_timestamp
    if not -MAX_FUTURE_PRICE_SKEW_SECONDS <= price_age <= max_price_age:
        raise QuoteError("FTSO price timestamp is outside the accepted window")
    windows = (max_price_age, payer_ttl, settlement_ttl)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in windows):
        raise QuoteError("quote windows must be positive integers")
    if settlement_ttl <= payer_ttl:
        raise QuoteError("settlement execution window must outlast the payer quote")


def _quote_result(
    quote_hash: str,
    parameters: QuoteParameters,
    amounts: tuple[int, int, int, int, int],
    windows: tuple[int, int],
) -> XrpQuote:
    target, maximum, net_mint, fee, payment = amounts
    expires_at, settlement_deadline_at = windows
    return XrpQuote(
        quote_hash=quote_hash,
        xrp_usd_price=str(parameters.xrp_usd_price),
        price_decimals=parameters.price_decimals,
        price_timestamp=parameters.price_timestamp,
        target_fxrp_uba=target,
        maximum_fxrp_uba=maximum,
        net_mint_uba=net_mint,
        minting_fee_uba=fee,
        memo_executor_fee_uba=parameters.memo_executor_fee_uba,
        payment_amount_drops=payment,
        lot_size_uba=parameters.lot_size_uba,
        minimum_redeem_uba=parameters.minimum_redeem_uba,
        core_vault=parameters.core_vault,
        source_block=parameters.source_block,
        expires_at=expires_at,
        settlement_deadline_at=settlement_deadline_at,
    )


def _quote_amounts(
    amount_usd: Decimal, parameters: QuoteParameters, slippage_bips: int
) -> tuple[int, int, int, int, int]:
    target = _ceil_units(amount_usd / parameters.xrp_usd_price)
    maximum = max(
        parameters.lot_size_uba,
        _ceil_ratio(target, 10_000 + slippage_bips, 10_000),
    )
    net_mint = maximum
    fee = max(net_mint * parameters.fee_bips // 10_000, parameters.minimum_fee_uba)
    payment = net_mint + fee + parameters.memo_executor_fee_uba
    return target, maximum, net_mint, fee, payment


def _ceil_units(value: Decimal) -> int:
    return int((value * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


def _ceil_ratio(value: int, numerator: int, denominator: int) -> int:
    return (value * numerator + denominator - 1) // denominator


def _quote_hash(
    invoice_id: str,
    invoice_amount_usd: Decimal,
    beneficiary: str,
    maximum: int,
    payment: int,
    expires_at: int,
    settlement_deadline_at: int,
    parameters: QuoteParameters,
) -> str:
    binding = {
        "beneficiary": beneficiary.lower(),
        "expires_at": expires_at,
        "settlement_deadline_at": settlement_deadline_at,
        "invoice_id": invoice_id,
        "invoice_amount_usd": format(invoice_amount_usd, "f"),
        "maximum_fxrp_uba": maximum,
        "network": "coston2",
        "payment_amount_drops": payment,
        "source_block": parameters.source_block,
    }
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "0x" + keccak256(encoded).hex()


def _validate_inputs(
    invoice_id: str,
    amount: Decimal,
    beneficiary: str,
    parameters: QuoteParameters,
    now: int,
) -> None:
    if not invoice_id or amount <= 0 or not beneficiary:
        raise QuoteError("invoice quote inputs are invalid")
    integers = (
        now,
        parameters.price_decimals,
        parameters.price_timestamp,
        parameters.lot_size_uba,
        parameters.minimum_redeem_uba,
        parameters.minimum_fee_uba,
        parameters.fee_bips,
        parameters.memo_executor_fee_uba,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in integers):
        raise QuoteError("live quote parameters are invalid")
    if parameters.xrp_usd_price <= 0 or parameters.lot_size_uba <= 0:
        raise QuoteError("live quote parameters are invalid")


def _parameters(
    settings: dict[str, object],
    price: int,
    decimals: int,
    timestamp: int,
    block: str,
) -> QuoteParameters:
    return QuoteParameters(
        xrp_usd_price=Decimal(price).scaleb(-decimals),
        price_decimals=decimals,
        price_timestamp=timestamp,
        lot_size_uba=int(settings["lot_size_uba"]),
        minimum_redeem_uba=int(settings["minimum_redeem_uba"]),
        minimum_fee_uba=int(settings["minimum_fee_uba"]),
        fee_bips=int(settings["fee_bips"]),
        memo_executor_fee_uba=0,
        core_vault=str(settings["core_vault"]),
        source_block=block,
    )


def _decode_feed(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise QuoteError("FTSO feed response is malformed")
    try:
        raw = bytes.fromhex(value[2:])
    except ValueError as error:
        raise QuoteError("FTSO feed response is malformed") from error
    if len(raw) != 96:
        raise QuoteError("FTSO feed response is malformed")
    price = int.from_bytes(raw[:32], "big")
    decimals = int.from_bytes(raw[32:64], "big", signed=True)
    timestamp = int.from_bytes(raw[64:], "big")
    if price <= 0 or not 0 <= decimals <= 18 or timestamp <= 0:
        raise QuoteError("FTSO feed response is malformed")
    return price, decimals, timestamp


def _registry_at(client: JsonRpcClient, name: str, block: str) -> str:
    from .live import _abi_string, _eth_call_at, _selector

    data = _selector("getContractAddressByName(string)") + _abi_string(name)[2:]
    raw = _decode_bytes(_eth_call_at(client, FLARE_CONTRACT_REGISTRY, data, block))
    if len(raw) != 32 or raw[:12] != b"\0" * 12:
        raise QuoteError("contract registry address is malformed")
    return "0x" + raw[12:].hex()


def _settings_at(client: JsonRpcClient, target: str, block: str) -> dict[str, object]:
    from .live import _eth_call_at, _eth_quantity_at, _selector

    settings = _decode_bytes(_eth_call_at(client, target, _selector("getSettings()"), block))
    if len(settings) < 32 + 20 * 32 or int.from_bytes(settings[:32], "big") != 32:
        raise QuoteError("AssetManager settings are malformed")
    decimals = int.from_bytes(settings[32 + 11 * 32 : 64 + 11 * 32], "big")
    lot_amg = int.from_bytes(settings[32 + 19 * 32 : 64 + 19 * 32], "big")
    granularity = _eth_quantity_at(client, target, "assetMintingGranularityUBA()", block)
    return {
        "minimum_fee_uba": _eth_quantity_at(client, target, "getDirectMintingMinimumFeeUBA()", block),
        "fee_bips": _eth_quantity_at(client, target, "getDirectMintingFeeBIPS()", block),
        "minimum_redeem_uba": _eth_quantity_at(client, target, "minimumRedeemAmountUBA()", block),
        "lot_size_uba": lot_amg * granularity,
        "asset_decimals": decimals,
        "core_vault": _string_at(client, target, "directMintingPaymentAddress()", block),
    }


def _string_at(client: JsonRpcClient, target: str, signature: str, block: str) -> str:
    from .live import _eth_call_at, _selector

    raw = _decode_bytes(_eth_call_at(client, target, _selector(signature), block))
    if len(raw) < 64 or int.from_bytes(raw[:32], "big") != 32:
        raise QuoteError("contract string is malformed")
    length = int.from_bytes(raw[32:64], "big")
    if 64 + length > len(raw):
        raise QuoteError("contract string is malformed")
    try:
        return raw[64 : 64 + length].decode("ascii")
    except UnicodeDecodeError as error:
        raise QuoteError("contract string is malformed") from error


def _decode_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise QuoteError("contract response is malformed")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as error:
        raise QuoteError("contract response is malformed") from error
