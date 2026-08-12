from decimal import Decimal

import pytest

import app.xrp.quotes as quotes
from app.xrp.quotes import QuoteError, QuoteParameters, build_quote


def parameters(**overrides: object) -> QuoteParameters:
    values: dict[str, object] = {
        "xrp_usd_price": Decimal("2.00"),
        "price_decimals": 6,
        "price_timestamp": 1_000,
        "lot_size_uba": 10_000_000,
        "minimum_redeem_uba": 5_000_000,
        "minimum_fee_uba": 100_000,
        "fee_bips": 25,
        "memo_executor_fee_uba": 0,
        "core_vault": "rCoreVault",
        "source_block": "0x123",
    }
    values.update(overrides)
    return QuoteParameters(**values)


def test_quote_rounds_up_caps_input_and_adds_live_fees() -> None:
    quote = build_quote(
        invoice_id="xrp_inv_1",
        invoice_amount_usd=Decimal("20.00"),
        beneficiary="0x" + "11" * 20,
        parameters=parameters(),
        now=1_030,
        ttl_seconds=120,
        slippage_bips=100,
    )

    assert quote.target_fxrp_uba == 10_000_000
    assert quote.maximum_fxrp_uba == 10_100_000
    assert quote.net_mint_uba == 10_100_000
    assert quote.minting_fee_uba == 100_000
    assert quote.payment_amount_drops == 10_200_000
    assert quote.expires_at == 1_150
    assert quote.settlement_deadline_at == 1_930
    assert quote.core_vault == "rCoreVault"
    assert quote.minimum_redeem_uba == 5_000_000


def test_quote_respects_live_lot_size_for_small_invoices() -> None:
    quote = build_quote(
        invoice_id="xrp_inv_1",
        invoice_amount_usd=Decimal("5.00"),
        beneficiary="0x" + "22" * 20,
        parameters=parameters(),
        now=1_030,
    )

    assert quote.target_fxrp_uba == 2_500_000
    assert quote.maximum_fxrp_uba == 10_000_000
    assert quote.net_mint_uba == 10_000_000
    assert quote.maximum_fxrp_uba >= quote.net_mint_uba
    assert quote.payment_amount_drops == 10_100_000


def test_quote_accepts_five_second_future_clock_skew() -> None:
    quote = build_quote(
        invoice_id="xrp_inv_1",
        invoice_amount_usd=Decimal("20"),
        beneficiary="0x" + "33" * 20,
        parameters=parameters(price_timestamp=1_035),
        now=1_030,
        max_price_age_seconds=120,
    )

    assert quote.price_timestamp == 1_035


def test_quote_rejects_stale_or_excessively_future_price_data() -> None:
    for timestamp in (800, 1_036):
        with pytest.raises(QuoteError, match="timestamp"):
            build_quote(
                invoice_id="xrp_inv_1",
                invoice_amount_usd=Decimal("20"),
                beneficiary="0x" + "33" * 20,
                parameters=parameters(price_timestamp=timestamp),
                now=1_030,
                max_price_age_seconds=120,
            )


def test_quote_hash_binds_invoice_beneficiary_expiry_and_cap() -> None:
    first = build_quote(
        invoice_id="xrp_inv_1",
        invoice_amount_usd=Decimal("20"),
        beneficiary="0x" + "44" * 20,
        parameters=parameters(),
        now=1_030,
    )
    repeated = build_quote(
        invoice_id="xrp_inv_1",
        invoice_amount_usd=Decimal("20"),
        beneficiary="0x" + "44" * 20,
        parameters=parameters(),
        now=1_030,
    )
    changed = build_quote(
        invoice_id="xrp_inv_1",
        invoice_amount_usd=Decimal("20"),
        beneficiary="0x" + "55" * 20,
        parameters=parameters(),
        now=1_030,
    )
    changed_amount_same_cap = build_quote(
        invoice_id="xrp_inv_1",
        invoice_amount_usd=Decimal("19.999999999"),
        beneficiary="0x" + "44" * 20,
        parameters=parameters(),
        now=1_030,
    )

    assert first.quote_hash == repeated.quote_hash
    assert first.quote_hash != changed.quote_hash
    assert first.maximum_fxrp_uba == changed_amount_same_cap.maximum_fxrp_uba
    assert first.quote_hash != changed_amount_same_cap.quote_hash


def test_coston2_provider_reads_ftso_and_asset_manager_at_runtime() -> None:
    provider_type = getattr(quotes, "Coston2QuoteProvider", None)
    assert provider_type is not None

    def word(value: int) -> str:
        return value.to_bytes(32, "big").hex()

    def response(request: dict[str, object], result: str) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    asset_manager = "0x" + "11" * 20
    ftso = "0x" + "22" * 20

    def transport(_url: str, request: dict[str, object], _timeout: int) -> dict[str, object]:
        method = request["method"]
        if method == "eth_chainId":
            return response(request, "0x72")
        if method == "eth_blockNumber":
            return response(request, "0xabc")
        assert request["params"][1] == "0xabc"  # type: ignore[index]
        call = request["params"][0]  # type: ignore[index]
        data = call["data"]  # type: ignore[index]
        target = call["to"].lower()  # type: ignore[index,union-attr]
        if target.endswith("f6019"):
            address = ftso if "4674736f5632" in data else asset_manager
            return response(request, "0x" + "00" * 12 + address[2:])
        if target == ftso:
            return response(request, "0x" + word(2_000_000) + word(6) + word(1_000))
        selectors = {
            quotes.keccak256(name.encode("ascii"))[:4].hex(): value
            for name, value in {
                "getDirectMintingMinimumFeeUBA()": word(100_000),
                "getDirectMintingFeeBIPS()": word(25),
                "assetMintingGranularityUBA()": word(1),
                "minimumRedeemAmountUBA()": word(5_000_000),
            }.items()
        }
        selector = data[2:10]
        if selector in selectors:
            return response(request, "0x" + selectors[selector])
        if selector == quotes.keccak256(b"getSettings()")[:4].hex():
            fields = [0] * 20
            fields[11] = 6
            fields[19] = 10_000_000
            return response(request, "0x" + word(32) + "".join(word(value) for value in fields))
        if selector == quotes.keccak256(b"directMintingPaymentAddress()")[:4].hex():
            value = b"rCoreVault"
            return response(request, "0x" + word(32) + word(len(value)) + value.hex() + "00" * 22)
        raise AssertionError(f"unexpected RPC call {request}")

    parameters = provider_type(transport=transport).read()

    assert parameters.xrp_usd_price == Decimal("2.000000")
    assert parameters.lot_size_uba == 10_000_000
    assert parameters.minimum_redeem_uba == 5_000_000
    assert parameters.minimum_fee_uba == 100_000
    assert parameters.fee_bips == 25
    assert parameters.core_vault == "rCoreVault"
    assert parameters.source_block == "0xabc"
