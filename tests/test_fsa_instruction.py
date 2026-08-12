from __future__ import annotations

import pytest

import app.xrp.rpc as rpc
import app.xrp.instructions as instructions

from app.xrp.instructions import (
    CustomInstruction,
    InstructionError,
    build_contract_user_operation,
    build_custom_instruction,
    build_unsigned_payment,
    inspect_custom_instruction,
    keccak256,
)
from app.xrp.live import CHECKPOINT, CHECKPOINT_SELECTOR, build_checkpoint_user_op
from app.xrp.rpc import (
    EXECUTE_DIRECT_MINTING_WITH_DATA_SELECTOR,
    FLARE_CONTRACT_REGISTRY,
    JsonRpcClient,
    RpcEvidenceError,
    validate_evidence,
)


PACKED_USER_OPERATION = b"abc"
KNOWN_KECCAK_ABC = "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
KNOWN_KECCAK_MULTI_BLOCK = "96ea54061def936c4be90b518992fdc6f12f535068a256229aca54267b4d084d"

ASSET_MANAGER = "0x1111111111111111111111111111111111111111"
PERSONAL_ACCOUNT = "0x2222222222222222222222222222222222222222"
MASTER_ACCOUNT_CONTROLLER = "0x3333333333333333333333333333333333333333"
XRPL_HASH = "aa" * 32
FLARE_HASH = "0x" + "bb" * 32
BLOCK_HASH = "0x" + "cc" * 32
EXECUTION_BLOCK = "0x42"
XRPL_AMOUNT_DROPS = "20024041"


def test_builds_spec_exact_hash_commitment_memo() -> None:
    instruction = build_custom_instruction(7, 0x0102030405060708, PACKED_USER_OPERATION)

    assert instruction.packed_user_operation == PACKED_USER_OPERATION
    assert instruction.user_op_hash.hex() == KNOWN_KECCAK_ABC
    assert instruction.memo_bytes == bytes.fromhex(
        "fe070102030405060708" + KNOWN_KECCAK_ABC
    )
    assert instruction.memo_data_hex == instruction.memo_bytes.hex().upper()
    assert len(instruction.memo_bytes) == 42


def test_keccak_matches_empty_and_multi_block_ethereum_vectors() -> None:
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(PACKED_USER_OPERATION).hex() == KNOWN_KECCAK_ABC
    assert keccak256(b"a" * 200).hex() == KNOWN_KECCAK_MULTI_BLOCK


def test_generic_contract_userop_matches_the_proven_phase0_encoder() -> None:
    expected, _instruction = build_checkpoint_user_op(PERSONAL_ACCOUNT, 7)
    actual = build_contract_user_operation(
        PERSONAL_ACCOUNT, 7, CHECKPOINT, bytes.fromhex(CHECKPOINT_SELECTOR[2:])
    )

    assert actual == expected


def test_contract_userop_encodes_approve_then_settle_as_two_calls() -> None:
    approve_target = "0x" + "44" * 20
    settlement_target = "0x" + "55" * 20
    approve = bytes.fromhex("095ea7b3") + b"a" * 64
    settle = bytes.fromhex("99fbab88") + b"b" * 224

    payload = instructions.build_contract_user_operations(
        PERSONAL_ACCOUNT,
        7,
        ((approve_target, approve), (settlement_target, settle)),
    )

    approve_word = bytes.fromhex("00" * 12 + "44" * 20)
    settlement_word = bytes.fromhex("00" * 12 + "55" * 20)
    assert (2).to_bytes(32, "big") in payload
    assert payload.index(approve_word) < payload.index(settlement_word)
    assert payload.index(approve) < payload.index(settle)


# Values independently generated with OpenSSL's KECCAK-256 digest, not SHA3-256.
@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (134, "e5de5653994e2fa6729d329b65b5f332dee942a7ea54515e173824c232a4ff91"),
        (135, "34367dc248bbd832f4e3e69dfaac2f92638bd0bbd18f2912ba4ef454919cf446"),
        (136, "a6c4d403279fe3e0af03729caada8374b5ca54d8065329a3ebcaeb4b60aa386e"),
    ],
)
def test_keccak_matches_rate_boundary_vectors(length: int, expected: str) -> None:
    assert keccak256(b"a" * length).hex() == expected


@pytest.mark.parametrize("wallet_id", [-1, 256])
def test_rejects_wallet_id_outside_uint8(wallet_id: int) -> None:
    with pytest.raises(InstructionError, match="wallet"):
        build_custom_instruction(wallet_id, 0, PACKED_USER_OPERATION)


@pytest.mark.parametrize("fee", [-1, 1 << 64])
def test_rejects_executor_fee_outside_uint64(fee: int) -> None:
    with pytest.raises(InstructionError, match="fee"):
        build_custom_instruction(0, fee, PACKED_USER_OPERATION)


def test_rejects_empty_user_operation() -> None:
    with pytest.raises(InstructionError, match="non-empty"):
        build_custom_instruction(0, 0, b"")


@pytest.mark.parametrize(
    ("memo", "payload", "message"),
    [
        (b"\xfe" * 41, PACKED_USER_OPERATION, "42 bytes"),
        (b"\xff" + b"\x00" * 41, PACKED_USER_OPERATION, "opcode"),
        (
            bytes.fromhex("fe070102030405060708" + "00" * 32),
            PACKED_USER_OPERATION,
            "hash",
        ),
    ],
)
def test_inspection_rejects_invalid_memo(memo: bytes, payload: bytes, message: str) -> None:
    with pytest.raises(InstructionError, match=message):
        inspect_custom_instruction(memo, payload)


def test_inspection_preserves_exact_supplied_user_operation() -> None:
    original = build_custom_instruction(3, 99, PACKED_USER_OPERATION)
    inspected = inspect_custom_instruction(original.memo_bytes, PACKED_USER_OPERATION)

    assert inspected == original


def test_payment_template_has_exact_memo_and_no_destination_tag() -> None:
    instruction = build_custom_instruction(0, 1, PACKED_USER_OPERATION)

    payment = build_unsigned_payment(
        source_account="rSource",
        core_vault_destination="rLiveCoreVault",
        amount_drops=123456,
        instruction=instruction,
    )

    assert payment == {
        "TransactionType": "Payment",
        "Account": "rSource",
        "Destination": "rLiveCoreVault",
        "Amount": "123456",
        "Memos": [{"Memo": {"MemoData": instruction.memo_data_hex}}],
    }
    assert "DestinationTag" not in payment


@pytest.mark.parametrize("amount", [0, -1, True])
def test_payment_template_rejects_invalid_amount(amount: object) -> None:
    instruction = build_custom_instruction(0, 0, PACKED_USER_OPERATION)
    with pytest.raises(InstructionError, match="drops"):
        build_unsigned_payment("rSource", "rLiveCoreVault", amount, instruction)  # type: ignore[arg-type]


def test_json_rpc_client_rejects_error_id_mismatch_and_timeout() -> None:
    responses = iter(
        [
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "bad"}},
            {"jsonrpc": "2.0", "id": 99, "result": {}},
            TimeoutError("network timed out"),
            TimeoutError("network timed out again"),
        ]
    )
    def transport(*_arguments: object) -> dict[str, object]:
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        return response

    client = JsonRpcClient("https://rpc.example", 3, transport)

    with pytest.raises(RpcEvidenceError, match="error"):
        client.request("first")
    with pytest.raises(RpcEvidenceError, match="id"):
        client.request("second")
    with pytest.raises(RpcEvidenceError, match="unreachable"):
        client.request("third")


def test_json_rpc_client_retries_one_transient_transport_failure() -> None:
    attempts = 0

    def transport(
        _url: str, request: dict[str, object], _timeout: int
    ) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionResetError("RPC peer closed the connection")
        return {"jsonrpc": "2.0", "id": request["id"], "result": "0x72"}

    client = JsonRpcClient(rpc.TRUSTED_COSTON2_RPC_URL, 3, transport)

    assert client.request("eth_chainId") == "0x72"
    assert attempts == 2


def test_trusted_xrpl_accepts_only_observed_legacy_success_envelope() -> None:
    legacy = {"result": {"status": "success", "info": {"network_id": 1}}}
    client = JsonRpcClient(rpc.TRUSTED_XRPL_TESTNET_RPC_URL, 3, lambda *_: legacy)

    assert client.request("server_info") == legacy["result"]

    for response, message in [
        ({"result": {"status": "error"}}, "JSON-RPC"),
        ({"id": 99, "result": {"status": "success"}}, "id"),
        ({"result": "success"}, "JSON-RPC"),
    ]:
        with pytest.raises(RpcEvidenceError, match=message):
            JsonRpcClient(rpc.TRUSTED_XRPL_TESTNET_RPC_URL, 3, lambda *_args, result=response: result).request("server_info")

    with pytest.raises(RpcEvidenceError, match="invalid JSON-RPC"):
        JsonRpcClient(rpc.TRUSTED_COSTON2_RPC_URL, 3, lambda *_: legacy).request("eth_chainId")


def test_http_transport_reuses_one_client_for_sequential_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200
        reason_phrase = "OK"
        headers: dict[str, str] = {}

        def __init__(self, request_id: object) -> None:
            self.request_id = request_id

        def json(self) -> dict[str, object]:
            return {"jsonrpc": "2.0", "id": self.request_id, "result": "0x72"}

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def post(
            self, _url: str, *, json: dict[str, object], timeout: int
        ) -> Response:
            self.calls.append({"request": json, "timeout": timeout})
            return Response(json["id"])

    client = RecordingClient()
    monkeypatch.setattr(rpc, "_HTTP_CLIENT", client, raising=False)
    json_rpc = JsonRpcClient(rpc.TRUSTED_COSTON2_RPC_URL, 3)

    assert json_rpc.request("eth_chainId") == "0x72"
    assert json_rpc.request("eth_chainId") == "0x72"
    assert len(client.calls) == 2


def test_json_rpc_client_rejects_http_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    class RedirectResponse:
        status_code = 302
        reason_phrase = "Found"
        headers: dict[str, str] = {"location": "https://redirect.example"}

    class RedirectingClient:
        def post(self, *_arguments: object, **_keywords: object) -> RedirectResponse:
            return RedirectResponse()

    monkeypatch.setattr(rpc, "_HTTP_CLIENT", RedirectingClient(), raising=False)

    with pytest.raises(RpcEvidenceError, match="redirect"):
        JsonRpcClient(rpc.TRUSTED_COSTON2_RPC_URL, 1).request("eth_chainId")


def test_validator_rejects_pending_or_incomplete_evidence() -> None:
    pending = {"schema_version": 1, "status": "pending_authorization"}
    with pytest.raises(RpcEvidenceError):
        validate_evidence(pending, 1, lambda *_: {})

    completed = {"schema_version": 1, "status": "completed"}
    with pytest.raises(RpcEvidenceError):
        validate_evidence(completed, 1, lambda *_: {})


def test_validator_accepts_a_complete_rpc_bound_evidence_record() -> None:
    evidence, transport = _completed_evidence_and_transport()

    validate_evidence(evidence, 1, transport)


def test_validator_uses_only_trusted_rpc_urls() -> None:
    seen_urls: list[str] = []
    evidence, transport = _completed_evidence_and_transport(seen_urls=seen_urls)

    validate_evidence(evidence, 1, transport)

    assert set(seen_urls) == {rpc.TRUSTED_XRPL_TESTNET_RPC_URL, rpc.TRUSTED_COSTON2_RPC_URL}


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("user_op", "UserOp"),
        ("xrpl_hash", "XRPL"),
        ("asset_manager", "AssetManager"),
        ("selector", "selector"),
        ("receipt_status", "succeed"),
        ("receipt_block", "block"),
        ("live_parameter", "minimum_fee_uba"),
        ("destination_tag", "destination tag"),
        ("partial_payment", "partial"),
        ("non_native_amount", "native XRP"),
        ("delivered_missing", "delivered_amount"),
        ("delivered_object", "delivered_amount"),
        ("delivered_mismatch", "delivered amount"),
        ("xrpl_network", "Testnet"),
        ("untrusted_rpc_url", "trusted RPC"),
        ("event_missing", "UserOperationExecuted"),
        ("event_emitter", "UserOperationExecuted"),
        ("event_account", "UserOperationExecuted"),
        ("event_nonce", "UserOperationExecuted"),
        ("core_vault", "core vault"),
        ("underfunded", "net minted"),
        ("fee_rounding", "minting fee"),
    ],
)
def test_validator_rejects_each_tampered_rpc_fact(tamper: str, message: str) -> None:
    evidence, transport = _completed_evidence_and_transport(tamper)

    with pytest.raises(RpcEvidenceError, match=message):
        validate_evidence(evidence, 1, transport)


def test_validator_accepts_minimum_fee_boundary_payment() -> None:
    evidence, transport = _completed_evidence_and_transport("minimum_fee_boundary")

    validate_evidence(evidence, 1, transport)


def _completed_evidence_and_transport(
    tamper: str | None = None, seen_urls: list[str] | None = None
) -> tuple[dict[str, object], object]:
    payload = _packed_user_operation(PERSONAL_ACCOUNT)
    evidence = _completed_evidence(payload)
    if tamper == "underfunded":
        evidence["xrpl"]["amount_drops"] = "20024013"  # type: ignore[index]
        evidence["xrpl"]["delivered_amount_drops"] = "20024013"  # type: ignore[index]
    if tamper == "fee_rounding":
        evidence["fees"]["minting_fee_uba"] = "24000"  # type: ignore[index]
    if tamper == "minimum_fee_boundary":
        evidence["xrpl"]["amount_drops"] = "20025013"  # type: ignore[index]
        evidence["xrpl"]["delivered_amount_drops"] = "20025013"  # type: ignore[index]
        evidence["fees"]["minimum_fee_uba"] = "25000"  # type: ignore[index]
        evidence["fees"]["minting_fee_uba"] = "25000"  # type: ignore[index]
    if tamper == "untrusted_rpc_url":
        evidence["xrpl"]["rpc_url"] = "https://attacker.example"  # type: ignore[index]
    return evidence, _transport_for(_tampered_calldata(payload, tamper), tamper, seen_urls)


def _completed_evidence(payload: bytes) -> dict[str, object]:
    instruction = build_custom_instruction(0, 13, payload)
    return {
        "schema_version": 1,
        "status": "completed",
        "protocol": _protocol_evidence(payload, instruction),
        "xrpl": _xrpl_evidence(),
        "flare": _flare_evidence(),
        "fees": _fee_evidence(),
    }


def _protocol_evidence(payload: bytes, instruction: CustomInstruction) -> dict[str, str]:
    return {
        "packed_user_operation_hex": payload.hex(),
        "user_op_hash_hex": instruction.user_op_hash.hex(),
        "memo_data_hex": instruction.memo_data_hex,
        "source_url": "https://dev.flare.network/smart-accounts/custom-instruction",
    }


def _xrpl_evidence() -> dict[str, object]:
    return {
        "rpc_url": rpc.TRUSTED_XRPL_TESTNET_RPC_URL, "transaction_hash": XRPL_HASH,
        "source_account": "rSource", "core_vault_destination": "rVault", "amount_drops": XRPL_AMOUNT_DROPS,
        "delivered_amount_drops": XRPL_AMOUNT_DROPS,
        "validated_ledger_index": 1, "timestamp": "2000-01-01T00:00:00Z",
        "source_url": "https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/tx",
    }


def _flare_evidence() -> dict[str, str]:
    return {
        "rpc_url": rpc.TRUSTED_COSTON2_RPC_URL, "chain_id": "0x72", "transaction_hash": FLARE_HASH,
        "personal_account": PERSONAL_ACCOUNT, "call_target": ASSET_MANAGER,
        "block_number": EXECUTION_BLOCK, "block_hash": BLOCK_HASH, "timestamp": "1970-01-01T00:00:00Z",
        "source_url": "https://dev.flare.network/fassets/reference/IAssetManager",
    }


def _fee_evidence() -> dict[str, str]:
    return {
        "minimum_fee_uba": "11", "fee_bips": "12", "memo_executor_fee_uba": "13",
        "standard_direct_mint_executor_fee_uba": "17",
        "asset_minting_granularity_uba": "1", "lot_size_amg": "20000000", "lot_size_uba": "20000000",
        "minting_fee_uba": "24028", "net_minted_uba": "20000000", "asset_decimals": "6", "lot_size_xrp": "20",
        "minimum_redeem_amount_uba": "5000000", "minimum_redeem_amount_xrp": "5",
        "query_block": EXECUTION_BLOCK,
        "source_url": "https://dev.flare.network/fassets/developer-guides/fassets-settings-node",
    }


def _tampered_calldata(payload: bytes, tamper: str | None) -> str:
    calldata = _direct_mint_calldata(payload, XRPL_HASH)
    if tamper == "user_op":
        return _direct_mint_calldata(_packed_user_operation("0x3333333333333333333333333333333333333333"), XRPL_HASH)
    if tamper == "xrpl_hash":
        return _direct_mint_calldata(payload, "dd" * 32)
    if tamper == "selector":
        return "0x00000000" + calldata[10:]
    return calldata


def _transport_for(flare_input: str, tamper: str | None, seen_urls: list[str] | None = None) -> object:
    def transport(url: str, request: dict[str, object], _timeout: int) -> dict[str, object]:
        if seen_urls is not None:
            seen_urls.append(url)
        method = request["method"]
        result = _rpc_result(method, request, flare_input, tamper)
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    return transport


def _rpc_result(method: object, request: dict[str, object], flare_input: str, tamper: str | None) -> object:
    if method == "server_info":
        return {"info": {"network_id": 0 if tamper == "xrpl_network" else 1}}
    if method == "tx":
        amount = XRPL_AMOUNT_DROPS
        if tamper == "underfunded":
            amount = "20024013"
        if tamper == "minimum_fee_boundary":
            amount = "20025013"
        payment = {
            "validated": True, "hash": XRPL_HASH, "TransactionType": "Payment",
            "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": amount}, "Account": "rSource",
            "Destination": "rVault", "Amount": amount, "Flags": 0, "ledger_index": 1, "date": 0,
            "Memos": [{"Memo": {"MemoData": _memo_data_for_fixture()}}],
        }
        if tamper == "destination_tag":
            payment["DestinationTag"] = 7
        if tamper == "partial_payment":
            payment["Flags"] = 0x00020000
        if tamper == "non_native_amount":
            payment["Amount"] = {"currency": "USD", "value": "1", "issuer": "rIssuer"}
        if tamper == "delivered_missing":
            del payment["meta"]["delivered_amount"]
        if tamper == "delivered_object":
            payment["meta"]["delivered_amount"] = {"currency": "USD", "value": "1", "issuer": "rIssuer"}
        if tamper == "delivered_mismatch":
            payment["meta"]["delivered_amount"] = "1"
        return payment
    if method == "eth_chainId":
        return "0x72"
    if method == "eth_getTransactionByHash":
        return {
            "hash": FLARE_HASH,
            "to": ASSET_MANAGER,
            "input": flare_input,
            "blockNumber": EXECUTION_BLOCK,
            "blockHash": BLOCK_HASH,
        }
    if method == "eth_getTransactionReceipt":
        status = "0x0" if tamper == "receipt_status" else "0x1"
        block_number = "0x43" if tamper == "receipt_block" else EXECUTION_BLOCK
        return {
            "transactionHash": FLARE_HASH,
            "status": status,
            "blockNumber": block_number,
            "blockHash": BLOCK_HASH,
            "logs": _user_operation_logs(tamper),
        }
    if method == "eth_getBlockByNumber":
        return {"hash": BLOCK_HASH, "timestamp": "0x0"}
    if method == "eth_call":
        return _eth_call_result(request, tamper)
    raise AssertionError(f"unexpected RPC method: {method}")


def _eth_call_result(request: dict[str, object], tamper: str | None) -> str:
    call = request["params"][0]  # type: ignore[index]
    data = call["data"]  # type: ignore[index]
    if call["to"].lower() == FLARE_CONTRACT_REGISTRY.lower():  # type: ignore[index]
        if data.startswith(_function_selector("getContractAddressByName(string)")):
            name = _decode_abi_string(bytes.fromhex(data[10:]))
            if name == "MasterAccountController":
                return _abi_address(MASTER_ACCOUNT_CONTROLLER)
        address = "0x4444444444444444444444444444444444444444" if tamper == "asset_manager" else ASSET_MANAGER
        return _abi_address(address)
    selectors = {
        _function_selector("getDirectMintingMinimumFeeUBA()"): 11,
        _function_selector("getDirectMintingFeeBIPS()"): 12,
        _function_selector("getDirectMintingExecutorFeeUBA()"): 17,
        _function_selector("minimumRedeemAmountUBA()"): 5000000,
        _function_selector("assetMintingGranularityUBA()"): 1,
    }
    if data == _function_selector("getSettings()"):
        return _settings_result(20000000, 6)
    if data == _function_selector("directMintingPaymentAddress()"):
        return _abi_string("rWrongVault" if tamper == "core_vault" else "rVault")
    value = 10 if tamper == "live_parameter" and data == _function_selector("getDirectMintingMinimumFeeUBA()") else selectors[data]  # type: ignore[index]
    if tamper == "minimum_fee_boundary" and data == _function_selector("getDirectMintingMinimumFeeUBA()"):
        value = 25000
    return _abi_word(value)


def _packed_user_operation(sender: str) -> bytes:
    sender_word = bytes.fromhex("00" * 12 + sender[2:])
    head = [sender_word, _word(0), _word(288), _word(320), _word(0), _word(0), _word(0), _word(352), _word(384)]
    return _word(32) + b"".join(head) + _word(0) * 4


def _direct_mint_calldata(payload: bytes, xrpl_hash: str) -> str:
    response = _word(0) * 4 + bytes.fromhex(xrpl_hash) + _word(0) + _word(224)
    proof = _word(64) + _word(96) + _word(0) + response
    encoded = _word(64) + _word(64 + len(proof)) + proof + _abi_bytes(payload)
    return EXECUTE_DIRECT_MINTING_WITH_DATA_SELECTOR + encoded.hex()


def _settings_result(lot_size_amg: int, decimals: int) -> str:
    words = [_word(0) for _ in range(20)]
    words[11] = _word(decimals)
    words[19] = _word(lot_size_amg)
    return "0x" + (_word(32) + b"".join(words)).hex()


def _memo_data_for_fixture() -> str:
    return build_custom_instruction(0, 13, _packed_user_operation(PERSONAL_ACCOUNT)).memo_data_hex


def _abi_address(address: str) -> str:
    return "0x" + "00" * 12 + address[2:]


def _abi_string(value: str) -> str:
    return "0x" + (_word(32) + _abi_bytes(value.encode("ascii"))).hex()


def _function_selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


def _user_operation_logs(tamper: str | None) -> list[dict[str, object]]:
    if tamper == "event_missing":
        return []
    account = "0x4444444444444444444444444444444444444444" if tamper == "event_account" else PERSONAL_ACCOUNT
    emitter = "0x5555555555555555555555555555555555555555" if tamper == "event_emitter" else MASTER_ACCOUNT_CONTROLLER
    nonce = 1 if tamper == "event_nonce" else 0
    return [{
        "address": emitter,
        "topics": [
            "0x" + keccak256(b"UserOperationExecuted(address,uint256)").hex(),
            "0x" + "00" * 12 + account[2:],
        ],
        "data": _abi_word(nonce),
    }]


def _decode_abi_string(encoded: bytes) -> str:
    start = int.from_bytes(encoded[:32], "big")
    length = int.from_bytes(encoded[start : start + 32], "big")
    return encoded[start + 32 : start + 32 + length].decode("ascii")


def _abi_bytes(value: bytes) -> bytes:
    padding = (-len(value)) % 32
    return _word(len(value)) + value + b"\x00" * padding


def _abi_word(value: int) -> str:
    return "0x" + _word(value).hex()


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")
