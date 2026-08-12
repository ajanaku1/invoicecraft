from __future__ import annotations

import os
import json
import hashlib
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest
import scripts.phase0_live as phase0_cli
from app.xrp.rpc import RpcEvidenceError

from app.xrp import live
from app.xrp.live import LiveConfig, LiveError, StateStore, build_checkpoint_user_op, compute_gross_payment, make_sign_request, verify_sign_request

PENDING_ARTIFACT = (Path(__file__).parent / "fixtures" / "protocol-spike-pending.json").read_text(encoding="utf-8")
assert hashlib.sha256(PENDING_ARTIFACT.encode("utf-8")).hexdigest() == phase0_cli.PENDING_PLACEHOLDER_SHA256


def test_gross_payment_uses_smart_account_fee_only() -> None:
    gross = compute_gross_payment(lot_size_uba=10_000_000, fee_bips=25, minimum_fee_uba=100_000, memo_executor_fee_uba=0)

    assert gross == 10_100_000
    assert gross != 10_200_000
    assert gross - max(gross * 25 // 10_000, 100_000) >= 10_000_000


def test_publish_evidence_validates_final_candidate_before_atomic_pending_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.phase0_live as cli

    candidate = {"schema_version": 1, "status": "completed"}
    class Store:
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": candidate}
    destination = tmp_path / "protocol-spike.json"
    destination.write_text(PENDING_ARTIFACT, encoding="utf-8")
    seen: list[object] = []
    monkeypatch.setattr(cli, "validate_evidence", lambda value, timeout: seen.append((value, timeout)))

    result = cli.publish_evidence(Store(), destination, 1)  # type: ignore[arg-type]

    assert seen == [(candidate, 1)]
    assert json.loads(destination.read_text(encoding="utf-8")) == candidate
    assert result["schema_version"] == 1


def test_publish_evidence_refuses_unsafe_states_and_preserves_existing_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.phase0_live as cli

    candidate = {"schema_version": 1, "status": "completed"}
    class Store:
        def __init__(self, state: dict[str, object]): self.state = state
        def read(self) -> dict[str, object]: return self.state
    destination = tmp_path / "protocol-spike.json"
    original = '{"schema_version":1,"status":"completed","other":true}\n'
    destination.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: (_ for _ in ()).throw(RpcEvidenceError("bad")))
    with pytest.raises(RpcEvidenceError): cli.publish_evidence(Store({"stage": "finalize", "candidate_evidence": candidate}), destination, 1)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == original
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    with pytest.raises(LiveError): cli.publish_evidence(Store({"stage": "prepare-execute", "candidate_evidence": candidate}), destination, 1)  # type: ignore[arg-type]
    with pytest.raises(LiveError): cli.publish_evidence(Store({"stage": "finalize", "candidate_evidence": candidate}), destination, 1)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == original


def test_publish_evidence_is_idempotent_and_refuses_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.phase0_live as cli

    candidate = {"schema_version": 1, "status": "completed"}
    class Store:
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": candidate}
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    destination = tmp_path / "protocol-spike.json"; destination.write_text(json.dumps(candidate), encoding="utf-8")
    before = destination.read_bytes(); cli.publish_evidence(Store(), destination, 1)  # type: ignore[arg-type]
    assert destination.read_bytes() == before
    link = tmp_path / "link.json"; link.symlink_to(destination)
    with pytest.raises(LiveError, match="symlink"): cli.publish_evidence(Store(), link, 1)  # type: ignore[arg-type]


def test_publish_evidence_atomic_mode_parent_symlink_and_cli_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.phase0_live as cli

    candidate = {"schema_version": 1, "status": "completed"}
    class Store:
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": candidate}
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    destination = tmp_path / "protocol-spike.json"; destination.write_text(PENDING_ARTIFACT, encoding="utf-8")
    state_before = json.dumps(Store().read(), sort_keys=True)
    cli.publish_evidence(Store(), destination, 1)  # type: ignore[arg-type]
    assert json.loads(destination.read_text(encoding="utf-8")) == candidate and destination.stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".protocol-spike-*")) and json.dumps(Store().read(), sort_keys=True) == state_before
    real = tmp_path / "real"; real.mkdir(); parent = tmp_path / "parent"; parent.symlink_to(real, target_is_directory=True)
    with pytest.raises(LiveError, match="symlink"): cli.publish_evidence(Store(), parent / "x.json", 1)  # type: ignore[arg-type]
    seen: list[Path] = []; monkeypatch.setattr(cli, "arguments", lambda: __import__("argparse").Namespace(command="publish-evidence", timeout_seconds=1))
    monkeypatch.setattr(cli, "publish_evidence", lambda store, path, timeout: seen.append(path) or {"path": str(path), "schema_version": 1})
    monkeypatch.setattr(cli, "load_config", lambda *_args: (_ for _ in ()).throw(AssertionError("must not load env")))
    assert cli.main() == 0 and seen == [cli.ROOT / "evidence" / "protocol-spike.json"]
    assert "schema_version" in capsys.readouterr().out


def test_publish_evidence_refuses_missing_destination_and_secret_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.phase0_live as cli
    class Store:
        def __init__(self, candidate: object): self.candidate = candidate
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": self.candidate}
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    missing = tmp_path / "missing.json"
    with pytest.raises(LiveError): cli.publish_evidence(Store({"schema_version": 1, "status": "completed"}), missing, 1)  # type: ignore[arg-type]
    assert not missing.exists()
    target = tmp_path / "protocol-spike.json"; original = PENDING_ARTIFACT; target.write_text(original, encoding="utf-8")
    with pytest.raises(LiveError, match="secret"): cli.publish_evidence(Store({"schema_version": 1, "status": "completed", "nested": {"api_key": "x"}}), target, 1)  # type: ignore[arg-type]
    assert target.read_text(encoding="utf-8") == original


def test_publish_evidence_refuses_noncanonical_pending_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.phase0_live as cli
    class Store:
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": {"schema_version": 1, "status": "completed"}}
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    for mutate in (lambda value: {**value, "extra": None}, lambda value: {key: item for key, item in value.items() if key != "protocol"}, lambda value: {**value, "authorization_note": "wrong"}):
        destination = tmp_path / str(len(list(tmp_path.iterdir())))
        original = json.dumps(mutate(json.loads(PENDING_ARTIFACT)), sort_keys=True)
        destination.write_text(original, encoding="utf-8")
        with pytest.raises(LiveError, match="pending placeholder"): cli.publish_evidence(Store(), destination, 1)  # type: ignore[arg-type]
        assert destination.read_text(encoding="utf-8") == original


def test_publish_evidence_replace_failure_preserves_bytes_and_cleans_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.phase0_live as cli
    class Store:
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": {"schema_version": 1, "status": "completed"}}
    destination = tmp_path / "protocol-spike.json"; original = PENDING_ARTIFACT; destination.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    monkeypatch.setattr(cli.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError): cli.publish_evidence(Store(), destination, 1)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == original and not list(tmp_path.glob(".protocol-spike-*"))


@pytest.mark.parametrize("failure", ["fsync", "fchmod"])
def test_publish_evidence_pre_replace_failures_preserve_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import scripts.phase0_live as cli
    class Store:
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": {"schema_version": 1, "status": "completed"}}
    destination = tmp_path / "protocol-spike.json"; original = PENDING_ARTIFACT; destination.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    monkeypatch.setattr(cli.os, failure, lambda *_args: (_ for _ in ()).throw(OSError(failure)))
    monkeypatch.setattr(cli.os, "replace", lambda *_args: (_ for _ in ()).throw(AssertionError("replace must not run")))
    with pytest.raises(OSError, match=failure): cli.publish_evidence(Store(), destination, 1)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == original and not list(tmp_path.glob(".protocol-spike-*"))


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_publish_evidence_write_and_flush_failures_preserve_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import scripts.phase0_live as cli
    class Store:
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": {"schema_version": 1, "status": "completed"}}
    destination = tmp_path / "protocol-spike.json"; original = PENDING_ARTIFACT; destination.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    real_fdopen = cli.os.fdopen
    class Output:
        def __init__(self, output: object): self.output = output
        def __enter__(self) -> object: self.output.__enter__(); return self
        def __exit__(self, *args: object) -> object: return self.output.__exit__(*args)
        def write(self, value: str) -> object:
            if failure == "write": raise OSError("write")
            return self.output.write(value)
        def flush(self) -> object:
            if failure == "flush": raise OSError("flush")
            return self.output.flush()
        def fileno(self) -> int: return self.output.fileno()
    monkeypatch.setattr(cli.os, "fdopen", lambda *args, **kwargs: Output(real_fdopen(*args, **kwargs)))
    monkeypatch.setattr(cli.os, "replace", lambda *_args: (_ for _ in ()).throw(AssertionError("replace must not run")))
    with pytest.raises(OSError, match=failure): cli.publish_evidence(Store(), destination, 1)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == original and not list(tmp_path.glob(".protocol-spike-*"))


def test_publish_evidence_pinned_placeholder_survives_module_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import scripts.phase0_live as cli
    cli = importlib.reload(cli)
    class Store:
        def read(self) -> dict[str, object]: return {"stage": "finalize", "candidate_evidence": {"schema_version": 1, "status": "completed"}}
    monkeypatch.setattr(cli, "validate_evidence", lambda *_args: None)
    destination = tmp_path / "protocol-spike.json"; tampered = PENDING_ARTIFACT + " " ; destination.write_text(tampered, encoding="utf-8")
    with pytest.raises(LiveError, match="pending placeholder"): cli.publish_evidence(Store(), destination, 1)  # type: ignore[arg-type]
    assert destination.read_text(encoding="utf-8") == tampered
    destination.write_text(json.dumps({"schema_version": 1, "status": "completed"}), encoding="utf-8")
    assert cli.publish_evidence(Store(), destination, 1)["schema_version"] == 1


def test_canonical_fsa_memo_decoder_accepts_only_uppercase_bare_42_byte_data() -> None:
    from app.xrp.instructions import build_custom_instruction

    instruction = build_custom_instruction(0, 0x0102030405060708, b"payload")

    assert live._canonical_fsa_memo_bytes(instruction.memo_data_hex)[2:10] == bytes.fromhex("0102030405060708")
    for value in ("0x" + instruction.memo_data_hex, instruction.memo_data_hex.lower(), instruction.memo_data_hex[:-2], instruction.memo_data_hex + "00", "F" * 83):
        with pytest.raises(LiveError, match="canonical FSA memo"):
            live._canonical_fsa_memo_bytes(value)


def test_candidate_evidence_uses_canonical_bare_memo_for_fee_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.xrp.instructions import build_custom_instruction

    memo = build_custom_instruction(0, 0x0102030405060708, b"payload").memo_data_hex
    state = {"xrpl_transaction_hash": "AB" * 32, "execute_sign_request": {"to": "0x" + "11" * 20}, "memo_data_hex": memo}
    seen: list[bytes] = []

    class Client:
        def request(self, method: str, _params: list[object]) -> object:
            return {"meta": {"delivered_amount": "1"}} if method == "tx" else {"timestamp": "0x0"}

    monkeypatch.setattr(live, "_fees_at", lambda *_args: seen.append(_args[-1]) or {})
    monkeypatch.setattr(live, "_protocol_section", lambda _state: {})
    monkeypatch.setattr(live, "_xrpl_section", lambda *_args: {})
    monkeypatch.setattr(live, "_flare_section", lambda *_args: {})

    live._candidate_evidence(state, Client(), Client(), {"hash": "0x" + "22" * 32}, {"blockNumber": "0x1"})

    assert seen == [bytes.fromhex("0102030405060708")]
    state["memo_data_hex"] = "G" * 84
    with pytest.raises(LiveError, match="canonical FSA memo"):
        live._candidate_evidence(state, Client(), Client(), {"hash": "0x" + "22" * 32}, {"blockNumber": "0x1"})


def test_xrpl_validation_serializes_canonical_bare_transaction_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.xrp import rpc

    calls: list[tuple[str, list[object]]] = []

    class Client:
        def request(self, method: str, params: list[object] | None = None) -> object:
            calls.append((method, params or []))
            return {"info": {"network_id": rpc.XRPL_TESTNET_NETWORK_ID}} if method == "server_info" else {"hash": "AB" * 32, "validated": True, "ledger_index": 1, "date": 0}

    monkeypatch.setattr(rpc, "_validate_xrpl_payment", lambda *_args: 1)
    monkeypatch.setattr(rpc, "_require_bound_timestamp", lambda *_args: None)
    monkeypatch.setattr(rpc, "_require_url", lambda *_args: None)
    evidence = {"transaction_hash": "AB" * 32, "validated_ledger_index": 1, "source_url": "https://xrpl.org"}

    rpc._validate_xrpl(Client(), evidence, b"\0" * 42)

    assert calls[1] == ("tx", [{"transaction": "AB" * 32, "binary": False}])
    with pytest.raises(rpc.RpcEvidenceError):
        rpc._validate_xrpl(Client(), {**evidence, "transaction_hash": "0x" + "AB" * 32}, b"\0" * 42)


def test_checkpoint_user_op_commits_exact_payload() -> None:
    payload, instruction = build_checkpoint_user_op("0x" + "11" * 20, 7)

    assert instruction.wallet_id == 0
    assert instruction.executor_fee_uba == 0
    assert len(instruction.memo_bytes) == 42
    assert payload == instruction.packed_user_operation


def test_checkpoint_user_op_matches_checked_in_cast_abi_vectors() -> None:
    """Regression oracle from Foundry cast 1.2.3, not this encoder's output."""
    account = "0x" + "11" * 20
    expected_execute = (
        "2b2ee783" + "00" * 31 + "20" + "00" * 31 + "01" + "00" * 31 + "20"
        + "00" * 12 + "ee6d54382aa623f4d16e856193f5f8384e487002" + "00" * 32 + "00" * 31 + "60" + "00" * 31 + "04"
        + "80abd133" + "00" * 28
    )
    expected_packed = (
        "00" * 31 + "20" + "00" * 12 + "11" * 20 + "00" * 31 + "07"
        + "00" * 30 + "0120" + "00" * 30 + "0140" + "00" * 32 + "00" * 32 + "00" * 32
        + "00" * 30 + "0280" + "00" * 30 + "02a0" + "00" * 32 + "00" * 30 + "0104"
        + expected_execute + "00" * 28 + "00" * 32 + "00" * 32
    )

    assert live._encode_execute_user_op(account, live.CHECKPOINT, live.CHECKPOINT_SELECTOR).hex() == expected_execute
    payload, _ = build_checkpoint_user_op(account, 7)
    assert payload.hex() == expected_packed


def test_registry_and_personal_account_calldata_match_checked_in_cast_oracles() -> None:
    expected_registry = "0x82760fca0000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000001041737365744d616e616765724658525000000000000000000000000000000000"
    expected_personal = "0xd09318d40000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000002272486239434a4157794234726a39315652576e3936446b756b473462776474795468000000000000000000000000000000000000000000000000000000000000"
    registry = live._selector("getContractAddressByName(string)") + live._abi_string("AssetManagerFXRP")[2:]
    personal = live._selector("getPersonalAccount(string)") + live._abi_string("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh")[2:]

    assert registry == expected_registry
    assert personal == expected_personal
    for calldata in (registry, personal):
        assert live._eth_calldata(calldata) == calldata
    with pytest.raises(LiveError, match="calldata"):
        live._eth_calldata("0x12340x56")


def test_state_store_refuses_symlink_and_writes_owner_only(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    state_file = directory / "run-state.json"
    state_file.symlink_to(directory / "other")

    with pytest.raises(LiveError, match="symlink"):
        StateStore(directory).write({"version": 1, "stage": "inspect"})

    state_file.unlink()
    StateStore(directory).write({"version": 1, "stage": "inspect"})

    assert os.stat(state_file).st_mode & 0o777 == 0o600


def test_state_store_refuses_stage_regression(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.write({"version": 1, "stage": "prepare-fdc"})

    with pytest.raises(LiveError, match="regress"):
        store.write({"version": 1, "stage": "poll-xaman"})


def test_browser_transaction_rechecks_all_bound_fields() -> None:
    signer = "0x" + "22" * 20
    request = make_sign_request(signer, "0x" + "33" * 20, 0, "0x1234", "fdc-request")
    valid = {"from": signer, "to": request["to"], "value": "0x0", "input": "0x1234"}

    verify_sign_request(request, valid, signer)
    valid["input"] = "0x5678"
    with pytest.raises(LiveError, match="calldata"):
        verify_sign_request(request, valid, signer)


@pytest.mark.parametrize(
    "purpose", ["deploy-product-contracts", "fund-test-liquidity"]
)
def test_product_sign_requests_reuse_the_fail_closed_public_schema(
    purpose: str,
) -> None:
    signer = "0x" + "22" * 20
    request = make_sign_request(
        signer, "0x" + "33" * 20, 0, "0x1234", purpose
    )

    assert request["purpose"] == purpose
    assert request["chain_id"] == "0x72"


def test_xrpl_classic_address_uses_ripple_base58check() -> None:
    assert live._xrpl_classic_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh") == "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
    with pytest.raises(LiveError, match="checksum"):
        live._xrpl_classic_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyT1")


def test_config_rejects_nonofficial_verifier_and_da_hosts(tmp_path: Path) -> None:
    environment = tmp_path / ".env"
    environment.write_text("\n".join([
        "XAMAN_API_KEY=k", "XAMAN_API_SECRET=s", "XRPL_TESTNET_ADDRESS=rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        "COSTON2_SIGNER_ADDRESS=0x2222222222222222222222222222222222222222", "VERIFIER_URL_TESTNET=https://evil.example",
        "VERIFIER_API_KEY_TESTNET=v", "COSTON2_DA_LAYER_URL=https://ctn2-data-availability.flare.network",
    ]), encoding="utf-8")
    with pytest.raises(LiveError, match="official"):
        live.load_config(environment)


def test_cli_hides_rpc_error_details_and_tracebacks(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(phase0_cli, "arguments", lambda: __import__("argparse").Namespace(command="inspect", timeout_seconds=1))
    monkeypatch.setattr(phase0_cli, "load_config", lambda path: (_ for _ in ()).throw(RpcEvidenceError("api-key-secret")))

    assert phase0_cli.main() == 1
    captured = capsys.readouterr()
    assert "traceback" not in captured.err.lower()
    assert "api-key-secret" not in captured.err


def test_fdc_response_requires_outer_offset_transaction_and_dynamic_bounds() -> None:
    tx_hash = "AA" * 32
    signer = "0x" + "22" * 20
    response = _xrp_response(tx_hash, signer)
    state = {"xrpl_transaction_hash": tx_hash, "signer": signer, "fdc_round_id": "1"}

    assert live._response_tuple_body(response, state) == response[32:]
    assert live._merkle_array(["0x" + "44" * 32]) == bytes.fromhex("00" * 31 + "01" + "44" * 32)
    malformed = bytearray(response)
    malformed[32 + 192:32 + 224] = (0).to_bytes(32, "big")
    with pytest.raises(LiveError, match="offset"):
        live._response_tuple_body(bytes(malformed), state)
    state["fdc_round_id"] = "2"
    with pytest.raises(LiveError, match="voting round"):
        live._response_tuple_body(response, state)


def test_fdc_request_matches_checked_in_cast_abi_vector_and_rejects_tampering() -> None:
    signer, transaction = "0x" + "22" * 20, "AA" * 32
    expected = "5852505061796d656e740000000000000000000000000000000000000000000074657374585250000000000000000000000000000000000000000000000000001111111111111111111111111111111111111111111111111111111111111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0000000000000000000000002222222222222222222222222222222222222222"
    request = _fdc_request(transaction, signer)

    assert request.hex() == expected
    live._validate_fdc_request(request, transaction, signer)
    for offset, replacement, message in [
        (0, b"WrongType".ljust(32, b"\0"), "XRPPayment"),
        (32, b"wrongXRP".ljust(32, b"\0"), "testXRP"),
        (96, b"\xbb" * 32, "XRPL transaction"),
        (128, b"\x01" + request[129:160], "proof owner"),
    ]:
        tampered = bytearray(request); tampered[offset:offset + 32] = replacement
        with pytest.raises(LiveError, match=message):
            live._validate_fdc_request(bytes(tampered), transaction, signer)
    with pytest.raises(LiveError, match="five words"):
        live._validate_fdc_request(request[:-1], transaction, signer)


def test_redirect_handler_refuses_external_redirects() -> None:
    with pytest.raises(HTTPError, match="redirect refused"):
        live._NoRedirect().redirect_request(Request("https://xumm.app/payload"))


def test_xaman_http_requests_have_explicit_agent_and_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []
    class Response:
        def __enter__(self) -> "Response": return self
        def __exit__(self, *args: object) -> None: return None
        def geturl(self) -> str: return "https://xumm.app/api/v1/platform/payload"
        def read(self) -> bytes: return b'{"uuid":"11111111-1111-1111-1111-111111111111"}'
    class Opener:
        def open(self, request: Request, timeout: int) -> Response:
            requests.append(request); return Response()
    monkeypatch.setattr(live, "build_opener", lambda *handlers: Opener())

    live._post_json("https://xumm.app/api/v1/platform/payload", {"txjson": {}}, "key-value", "secret-value")
    live._get_json("https://xumm.app/api/v1/platform/payload/11111111-1111-1111-1111-111111111111", live._xaman_headers_values("key-value", "secret-value"), "xumm.app")

    for request in requests:
        assert request.get_header("User-agent") == live.XAMAN_USER_AGENT
        assert request.get_header("X-api-key") == "key-value"
        assert request.get_header("X-api-secret") == "secret-value"


def test_static_signer_requires_reverification_and_uses_text_content() -> None:
    page = (Path(__file__).parents[1] / "scripts" / "coston2_signer.html").read_text(encoding="utf-8")

    assert "Object.keys(value).length!==fields.length" in page
    assert "await verifyProvider(context,false)" in page
    assert "selectedProvider.provider.on('accountsChanged'" in page
    assert "selectedProvider.provider.on('chainChanged'" in page
    assert "innerHTML" not in page
    assert "navigator.clipboard.writeText(copiedHash)" in page
    assert "rememberSubmission(context.key,'pending')" in page
    assert "Submission status unknown; do not retry; verify wallet or explorer." in page
    assert "error.code===4001" in page
    assert "localStorage.getItem(key)" in page
    assert '<code id="data">' in page
    assert "contextEpoch" in page
    assert "context.provider.request" in page


def test_signer_connect_click_explains_missing_prerequisites_without_provider_call() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',calls=[];
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}},context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
const provider={request:request=>{calls.push(request.method);throw Error('Connect prerequisites must not call a provider')},on:()=>{}};window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});(async()=>{if(elements.connect.disabled)throw Error('Connect stayed disabled after wallet discovery');await elements.connect.onclick();if(elements.result.textContent!=='Load sign-request JSON first.'||calls.length)throw Error('missing request feedback was not explicit or made a provider call: '+JSON.stringify({result:elements.result.textContent,calls}));elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});if(elements.connect.disabled)throw Error('Connect became disabled while waiting for wallet selection');await elements.connect.onclick();if(elements.result.textContent!=='Choose a wallet first.'||calls.length)throw Error('missing wallet feedback was not explicit or made a provider call: '+JSON.stringify({result:elements.result.textContent,calls}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_connect_opens_selected_wallet_before_chain_verification() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',calls=[];
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}},context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
let releaseAccounts;const provider={request:request=>{calls.push(request.method);if(request.method==='eth_requestAccounts')return new Promise(resolve=>{releaseAccounts=()=>resolve([signer])});if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='web3_sha3')return Promise.resolve(hash);throw Error('unexpected '+request.method)},on:()=>{}};window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});(async()=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();const opening=elements.connect.onclick();if(elements.result.textContent!=='Opening Wallet A…'||calls.join(',')!=='eth_requestAccounts'||!elements.connect.disabled||!releaseAccounts)throw Error('wallet opening feedback or order was wrong: '+JSON.stringify({result:elements.result.textContent,calls,disabled:elements.connect.disabled}));await elements.connect.onclick();if(calls.join(',')!=='eth_requestAccounts')throw Error('duplicate connection invoked provider twice');releaseAccounts();await opening;if(calls.join(',')!=='eth_requestAccounts,eth_chainId,web3_sha3'||elements.result.textContent!=='Wallet, Coston2 chain, and calldata hash verified.'||elements.confirm.disabled||elements.connect.disabled)throw Error('wallet connection did not finish safely: '+JSON.stringify({result:elements.result.textContent,calls,disabled:elements.connect.disabled}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_requests_coston2_only_for_explicit_connection() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',network={chainId:'0x72',chainName:'Flare Testnet Coston2',nativeCurrency:{name:'Coston2 Flare',symbol:'C2FLR',decimals:18},rpcUrls:['https://coston2-api.flare.network/ext/C/rpc'],blockExplorerUrls:['https://coston2-explorer.flare.network']};
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
async function scenario(kind){const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),calls=[];let chain=kind==='send'?'0x72':'0x1',switches=0,added;const window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}},context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);const handlers={};const emit=value=>(handlers.chainChanged??[]).forEach(fn=>fn(value)),provider={on:(name,fn)=>(handlers[name]??=[]).push(fn),request:async request=>{calls.push(request.method);if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='eth_chainId')return chain;if(request.method==='web3_sha3')return hash;if(request.method==='wallet_switchEthereumChain'){if(request.params[0].chainId!=='0x72')throw Error('wrong switch request');switches+=1;if(kind==='add'&&switches===1)throw Object.assign(Error('unknown chain'),{code:'4902'});if(kind==='reject')throw Object.assign(Error('rejected'),{code:4001});if(kind==='wrong')return null;chain='0x72';emit(kind==='unsolicited'?'0x1':'0x72');return null}if(request.method==='wallet_addEthereumChain'){added=request.params[0];return null}throw Error('unexpected '+request.method)}};window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();if(kind==='switch'){if(calls.join(',')!=='eth_requestAccounts,eth_chainId,wallet_switchEthereumChain,eth_chainId,web3_sha3'||elements.result.textContent!=='Wallet, Coston2 chain, and calldata hash verified.'||elements.confirm.disabled)throw Error('switch success did not verify in order: '+JSON.stringify({calls,result:elements.result.textContent}))}if(kind==='add'){if(JSON.stringify(added)!==JSON.stringify(network)||calls.join(',')!=='eth_requestAccounts,eth_chainId,wallet_switchEthereumChain,wallet_addEthereumChain,eth_chainId,wallet_switchEthereumChain,eth_chainId,web3_sha3'||elements.confirm.disabled)throw Error('4902 add did not use exact config or verify after switch: '+JSON.stringify({added,calls}))}if(kind==='reject'){if(elements.result.textContent!=='Coston2 approval was rejected.'||!elements.confirm.disabled||!elements.send.disabled||calls.includes('web3_sha3'))throw Error('rejected approval was not explicit and fail-closed: '+JSON.stringify({calls,result:elements.result.textContent}))}if(kind==='wrong'){if(elements.result.textContent!=='Wallet must use Coston2 (0x72)'||!elements.confirm.disabled||calls.includes('web3_sha3'))throw Error('wrong post-switch chain was accepted: '+JSON.stringify({calls,result:elements.result.textContent}))}if(kind==='unsolicited'){if(elements.result.textContent!=='Stale context; reconnect and verify again.'||!elements.confirm.disabled||calls.includes('web3_sha3'))throw Error('unsolicited chain change did not invalidate connection: '+JSON.stringify({calls,result:elements.result.textContent}))}if(kind==='send'){if(elements.confirm.disabled)throw Error('baseline Coston2 connection failed');chain='0x1';elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick();if(calls.filter(method=>method==='wallet_switchEthereumChain'||method==='wallet_addEthereumChain').length||elements.result.textContent!=='Wallet must use Coston2 (0x72)'||!elements.send.disabled)throw Error('send attempted network approval: '+JSON.stringify({calls,result:elements.result.textContent}))}}
(async()=>{for(const kind of ['switch','add','reject','wrong','unsolicited','send'])await scenario(kind)})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_falls_back_to_official_sha3_only_for_unsupported_wallet_methods() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470';
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
async function scenario(kind){const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),calls=[],fetches=[];let release;class AbortController{constructor(){this.signal={}}abort(){}}const response=value=>({ok:true,json:async()=>value}),fetch=(url,options)=>{fetches.push({url,options});if(kind==='http')return Promise.resolve({ok:false,status:500,json:async()=>({})});if(kind==='json')return Promise.resolve({ok:true,json:async()=>{throw Error('bad json')}});if(kind==='id')return Promise.resolve(response({jsonrpc:'2.0',id:2,result:hash}));if(kind==='error')return Promise.resolve(response({jsonrpc:'2.0',id:1,error:{code:-1}}));if(kind==='result')return Promise.resolve(response({jsonrpc:'2.0',id:1,result:'0x1234'}));if(kind==='timeout')return Promise.reject(Object.assign(Error('aborted'),{name:'AbortError'}));if(kind==='stale')return new Promise(resolve=>{release=()=>resolve(response({jsonrpc:'2.0',id:1,result:hash}))});return Promise.resolve(response({jsonrpc:'2.0',id:1,result:kind==='rpc-wrong'?'0x'+'aa'.repeat(32):hash}))},window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},AbortController,fetch,setTimeout,clearTimeout,navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);const provider={request:async request=>{calls.push(request.method);if(request.method==='eth_chainId')return '0x72';if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='web3_sha3'){if(kind==='wrong')return '0x'+'bb'.repeat(32);if(kind==='invalid')return 'invalid';if(kind==='unrelated')throw Object.assign(Error('wallet offline'),{code:123});if(kind==='code')throw Object.assign(Error('not implemented'),{code:-32601});if(kind==='4200')throw Object.assign(Error('unsupported'),{code:'4200'});throw Error("method [web3_sha3] doesn't has corresponding handler")}throw Error('unexpected '+request.method)},on:()=>{}};window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});const load=async data=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data,calldata_hash:hash})}];await elements.file.onchange({target:elements.file})};await load('0x');elements.wallet.value='a';elements.wallet.onchange();const connecting=elements.connect.onclick();if(kind==='stale'){await new Promise(resolve=>setImmediate(resolve));await load('0x1234');release();await connecting;if(elements.result.textContent!=='Request loaded. Choose a wallet, then connect to verify.'||!elements.confirm.disabled)throw Error('stale fetch mutated newer request: '+elements.result.textContent);return}await connecting;const supported=['message','code','4200'];if(supported.includes(kind)){const request=fetches[0]?.options;if(fetches.length!==1||fetches[0].url!=='https://coston2-api.flare.network/ext/C/rpc'||request.method!=='POST'||request.credentials!=='omit'||JSON.stringify(JSON.parse(request.body))!==JSON.stringify({jsonrpc:'2.0',id:1,method:'web3_sha3',params:['0x']})||elements.confirm.disabled)throw Error('unsupported fallback did not strictly verify: '+JSON.stringify({kind,fetches,result:elements.result.textContent}))}else if(kind==='wrong'||kind==='invalid'||kind==='unrelated'){if(fetches.length||!elements.confirm.disabled)throw Error('wallet hash/error improperly fell back: '+JSON.stringify({kind,fetches,result:elements.result.textContent}))}else if(!elements.confirm.disabled||!elements.result.textContent.includes('Official Coston2 calldata hash verification failed'))throw Error('official RPC failure was not fail-closed: '+JSON.stringify({kind,result:elements.result.textContent}))}
(async()=>{for(const kind of ['message','code','4200','wrong','invalid','unrelated','http','json','id','error','result','rpc-wrong','timeout','stale'])await scenario(kind)})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_uses_sha3_fallback_before_presend_dispatch() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'.slice(0,66),tx='0x'+'aa'.repeat(32);class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}class AbortController{constructor(){this.signal={}}abort(){}}const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},calls=[],fetches=[],storage=new Map(),window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};let unsupported=false;const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},AbortController,fetch:async(url,options)=>{fetches.push({url,options});return {ok:true,json:async()=>({jsonrpc:'2.0',id:1,result:hash})}},setTimeout,clearTimeout,navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);const provider={request:async request=>{calls.push(request.method);if(request.method==='eth_chainId')return '0x72';if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='web3_sha3'){if(unsupported)throw Object.assign(Error('unsupported web3_sha3 handler'),{code:4200});return hash}if(request.method==='eth_sendTransaction')return tx;throw Error('unexpected '+request.method)},on:()=>{}};window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});(async()=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();unsupported=true;elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick();if(fetches.length!==1||calls.join(',')!=='eth_requestAccounts,eth_chainId,web3_sha3,eth_chainId,eth_accounts,web3_sha3,eth_sendTransaction'||!elements.result.textContent.includes(tx))throw Error('presend fallback was not verified before dispatch: '+JSON.stringify({calls,fetches,result:elements.result.textContent}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_rejects_ambiguous_sha3_fallback_conditions() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470';class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}async function scenario(kind){const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},fetches=[];class AbortController{constructor(){this.signal={}}abort(){}}const window={addEventListener:(n,f)=>(listeners[n]??=[]).push(f),dispatchEvent:e=>(listeners[e.type]??[]).forEach(f=>f(e)),localStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}},context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},AbortController,setTimeout,clearTimeout,fetch:async()=>{fetches.push(1);const body=kind==='missing'?{id:1,result:hash}:kind==='wrong'?{jsonrpc:'1.0',id:1,result:hash}:{jsonrpc:'2.0',id:1,error:null,result:hash};return {ok:true,json:async()=>body}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);const provider={request:async r=>{if(r.method==='eth_chainId')return '0x72';if(r.method==='eth_requestAccounts')return [signer];if(r.method==='web3_sha3')throw Error(kind==='timeout'?'web3_sha3 handler timed out':"method [web3_sha3] doesn't has corresponding handler")},on:()=>{}};window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'A',rdns:'a'},provider}});elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();if(kind==='timeout'){if(fetches.length||elements.confirm.disabled===false)throw Error('timeout incorrectly fell back')}else if(fetches.length!==1||!elements.confirm.disabled||!elements.result.textContent.includes('Official Coston2 calldata hash verification failed'))throw Error('bad envelope accepted '+kind)}(async()=>{for(const k of ['timeout','missing','wrong','ambiguous'])await scenario(k)})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_renders_exact_raw_calldata() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8');
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),storage=new Map(),window={addEventListener:()=>{},dispatchEvent:()=>{},localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
(async()=>{const data='0x1234';elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer:'0x2222222222222222222222222222222222222222',to:'0x3333333333333333333333333333333333333333',value:'0x0',data,calldata_hash:'0x'+'11'.repeat(32)})}];await elements.file.onchange({target:elements.file});if(elements.data.textContent!==data)throw Error('raw calldata did not render exactly: '+elements.data.textContent)})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_rejects_uppercase_quantity_before_duplicate_submission() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',tx='0x'+'aa'.repeat(32),calls=[];
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
const provider={request:async request=>{calls.push(request.method);if(request.method==='eth_chainId')return '0x72';if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='web3_sha3')return hash;if(request.method==='eth_sendTransaction')return tx;throw Error('unexpected '+request.method)},on:()=>{}};window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});
const load=async value=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value,data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file})};const submit=async()=>{elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick()};
(async()=>{await load('0x3e8');await submit();await load('0x3E8');if(elements.connect.disabled)throw Error('Connect did not remain available after invalid request');if(calls.filter(method=>method==='eth_sendTransaction').length!==1||storage.size!==1||!elements.result.textContent.includes('Invalid public sign request'))throw Error('uppercase quantity bypassed guard identity: '+JSON.stringify({calls,storage:[...storage],result:elements.result.textContent}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_preserves_active_send_when_provider_announces_late() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',tx='0x'+'aa'.repeat(32),calls={a:[],b:[]};
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
let resolveA;const provider=id=>({request:request=>{calls[id].push(request.method);if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return Promise.resolve([signer]);if(request.method==='web3_sha3')return Promise.resolve(hash);if(request.method==='eth_sendTransaction'&&id==='a')return new Promise(resolve=>{resolveA=()=>resolve(tx)});if(request.method==='eth_sendTransaction')throw Error('B must not send');throw Error('unexpected '+request.method)},on:()=>{}}),a=provider('a'),b=provider('b'),announce=(uuid,name,provider)=>window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid,name,rdns:uuid+'.example'},provider}}),wait=()=>new Promise(resolve=>setImmediate(resolve));
announce('a','Wallet A',a);(async()=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();elements.confirm.checked=true;elements.confirm.onchange();const sendA=elements.send.onclick();await wait();if(!resolveA)throw Error('A send was not deferred');announce('b','Wallet B',b);if(elements.wallet.value!=='a'||elements.wallet.options.length!==3||!elements.send.disabled)throw Error('late announcement reset active selection');resolveA();await sendA;if(!elements.result.textContent.includes(tx)||elements.copy.disabled||calls.b.length||storage.size!==1)throw Error('late announcement concealed A hash: '+JSON.stringify({calls,storage:[...storage],result:elements.result.textContent}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_blocks_stale_provider_before_send() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8');
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',calls={a:[],b:[]};
const window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
let pause=false,releaseHash;const provider=id=>({request:request=>{calls[id].push(request.method);if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return Promise.resolve([signer]);if(request.method==='web3_sha3'&&pause&&id==='a')return new Promise(resolve=>{releaseHash=()=>resolve(hash)});if(request.method==='web3_sha3')return Promise.resolve(hash);if(request.method==='eth_sendTransaction')return Promise.resolve('0x'+'aa'.repeat(32));throw Error('unexpected '+request.method)},on:()=>{}}),a=provider('a'),b=provider('b'),announce=(uuid,name,provider)=>window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid,name,rdns:uuid+'.example'},provider}}),wait=()=>new Promise(resolve=>setImmediate(resolve));
announce('a','Wallet A',a);announce('b','Wallet B',b);
const load=async()=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file})};const choose=async id=>{elements.wallet.value=id;elements.wallet.onchange();await elements.connect.onclick()};
(async()=>{await load();await choose('a');elements.confirm.checked=true;elements.confirm.onchange();pause=true;const staleSend=elements.send.onclick();await wait();if(!releaseHash)throw Error('A pre-send hash was not deferred');await choose('b');releaseHash();await staleSend;if(calls.a.includes('eth_sendTransaction')||calls.b.includes('eth_sendTransaction')||storage.size||elements.result.textContent!=='Wallet, Coston2 chain, and calldata hash verified.'||elements.confirm.disabled||!elements.send.disabled)throw Error('stale provider send was not blocked: '+JSON.stringify({calls,storage:[...storage]}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_blocks_stale_request_before_send() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8');
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),signer='0x2222222222222222222222222222222222222222',oldHash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',newHash='0x'+'11'.repeat(32),calls=[];
const window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
let pause=false,releaseHash;const provider={request:request=>{calls.push(request.method);if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return Promise.resolve([signer]);if(request.method==='web3_sha3'&&pause&&request.params[0]==='0x')return new Promise(resolve=>{releaseHash=()=>resolve(oldHash)});if(request.method==='web3_sha3')return Promise.resolve(request.params[0]==='0x1234'?newHash:oldHash);if(request.method==='eth_sendTransaction')return Promise.resolve('0x'+'aa'.repeat(32));throw Error('unexpected '+request.method)},on:()=>{}},wait=()=>new Promise(resolve=>setImmediate(resolve));
window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});const load=async(data,calldata_hash)=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data,calldata_hash})}];await elements.file.onchange({target:elements.file})};const connect=async()=>{elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick()};
(async()=>{await load('0x',oldHash);await connect();elements.confirm.checked=true;elements.confirm.onchange();pause=true;const staleSend=elements.send.onclick();await wait();if(!releaseHash)throw Error('old request hash was not deferred');await load('0x1234',newHash);await connect();releaseHash();await staleSend;if(calls.includes('eth_sendTransaction')||storage.size||elements.result.textContent!=='Wallet, Coston2 chain, and calldata hash verified.'||elements.confirm.disabled||!elements.send.disabled)throw Error('stale request send was not blocked: '+JSON.stringify({calls,storage:[...storage]}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_keeps_newer_submission_ui_after_stale_4001() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8');
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),signer='0x2222222222222222222222222222222222222222',hashA='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',hashB='0x'+'11'.repeat(32),txB='0x'+'bb'.repeat(32),calls={a:[],b:[]};
const window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
let rejectA;const provider=id=>({request:request=>{calls[id].push(request.method);if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return Promise.resolve([signer]);if(request.method==='web3_sha3')return Promise.resolve(request.params[0]==='0x'?hashA:hashB);if(request.method==='eth_sendTransaction'&&id==='a')return new Promise((resolve,reject)=>{rejectA=()=>reject(Object.assign(Error('rejected'),{code:4001}))});if(request.method==='eth_sendTransaction')return Promise.resolve(txB);throw Error('unexpected '+request.method)},on:()=>{}}),a=provider('a'),b=provider('b'),announce=(uuid,name,provider)=>window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid,name,rdns:uuid+'.example'},provider}}),wait=()=>new Promise(resolve=>setImmediate(resolve));
announce('a','Wallet A',a);announce('b','Wallet B',b);const key=hash=>'phase0-submission:0x72:'+signer+':0x3333333333333333333333333333333333333333:0x0:'+hash;const load=async(data,hash)=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data,calldata_hash:hash})}];await elements.file.onchange({target:elements.file})};const choose=async id=>{elements.wallet.value=id;elements.wallet.onchange();await elements.connect.onclick()};const confirm=()=>{elements.confirm.checked=true;elements.confirm.onchange()};
(async()=>{await load('0x',hashA);await choose('a');confirm();const sendA=elements.send.onclick();await wait();if(!rejectA||storage.get(key(hashA))!=='pending')throw Error('A was not dispatched with a pending guard');await load('0x1234',hashB);await choose('b');confirm();await elements.send.onclick();const bResult=elements.result.textContent,bCalls=calls.b.filter(method=>method==='eth_sendTransaction').length;if(storage.get(key(hashB))!=='submitted:'+txB||!bResult.includes(txB)||!elements.send.disabled)throw Error('B was not submitted');rejectA();await sendA;if(storage.has(key(hashA))||storage.get(key(hashB))!=='submitted:'+txB||elements.result.textContent!==bResult||!elements.send.disabled)throw Error('stale A 4001 reset B state: '+JSON.stringify({storage:[...storage],result:elements.result.textContent}));await elements.send.onclick();if(calls.b.filter(method=>method==='eth_sendTransaction').length!==bCalls||storage.get(key(hashB))!=='submitted:'+txB)throw Error('B resent despite submitted guard')})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_scopes_stale_non4001_and_success_to_captured_guard() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hashA='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',hashB='0x'+'11'.repeat(32),txA='0x'+'aa'.repeat(32),txB='0x'+'bb'.repeat(32);
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
async function scenario(mode){const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),calls={a:[],b:[]},window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>{if(mode==='guard-failure'&&key.endsWith(hashA)&&value.startsWith('submitted:'))throw Error('storage full');storage.set(key,value)},removeItem:key=>storage.delete(key)}},context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);let settleA;const provider=id=>({request:request=>{calls[id].push(request.method);if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return Promise.resolve([signer]);if(request.method==='web3_sha3')return Promise.resolve(request.params[0]==='0x'?hashA:hashB);if(request.method==='eth_sendTransaction'&&id==='a')return new Promise((resolve,reject)=>{settleA=()=>mode==='non4001'?reject(Error('ambiguous')):resolve(txA)});if(request.method==='eth_sendTransaction')return Promise.resolve(txB);throw Error('unexpected '+request.method)},on:()=>{}}),announce=(uuid,name,provider)=>window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid,name,rdns:uuid+'.example'},provider}});announce('a','Wallet A',provider('a'));announce('b','Wallet B',provider('b'));const key=hash=>'phase0-submission:0x72:'+signer+':0x3333333333333333333333333333333333333333:0x0:'+hash,load=async(data,hash)=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data,calldata_hash:hash})}];await elements.file.onchange({target:elements.file})},choose=async id=>{elements.wallet.value=id;elements.wallet.onchange();await elements.connect.onclick()},confirm=()=>{elements.confirm.checked=true;elements.confirm.onchange()},wait=()=>new Promise(resolve=>setImmediate(resolve));await load('0x',hashA);await choose('a');confirm();const sendA=elements.send.onclick();await wait();if(!settleA||storage.get(key(hashA))!=='pending')throw Error('A was not dispatched');await load('0x1234',hashB);await choose('b');confirm();await elements.send.onclick();const bResult=elements.result.textContent,bCalls=calls.b.filter(method=>method==='eth_sendTransaction').length;settleA();await sendA;const aExpected=mode==='success'?'submitted:'+txA:'pending';if(storage.get(key(hashA))!==aExpected||storage.get(key(hashB))!=='submitted:'+txB||elements.result.textContent!==bResult||!elements.send.disabled)throw Error('stale '+mode+' mutated B: '+JSON.stringify({storage:[...storage],result:elements.result.textContent}));await elements.send.onclick();if(calls.b.filter(method=>method==='eth_sendTransaction').length!==bCalls)throw Error('B resent after stale '+mode)}
(async()=>{await scenario('non4001');await scenario('success');await scenario('guard-failure')})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_ignores_stale_connect_and_presend_completions() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hashA='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',hashB='0x'+'11'.repeat(32),txB='0x'+'bb'.repeat(32);
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
async function scenario(kind){const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),calls={a:[],b:[]},window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}},context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);let arm=false,settle;const provider=id=>({request:request=>{calls[id].push(request.method);if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return Promise.resolve([signer]);if(request.method==='web3_sha3'&&id==='a'&&arm)return new Promise((resolve,reject)=>{settle=()=>kind.endsWith('resolve')?resolve(hashA):reject(Error('delayed verification error'))});if(request.method==='web3_sha3')return Promise.resolve(id==='a'?hashA:hashB);if(request.method==='eth_sendTransaction')return Promise.resolve(txB);throw Error('unexpected '+request.method)},on:()=>{}}),announce=(uuid,provider)=>window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid,name:uuid,rdns:uuid+'.example'},provider}});announce('a',provider('a'));announce('b',provider('b'));const key=hash=>'phase0-submission:0x72:'+signer+':0x3333333333333333333333333333333333333333:0x0:'+hash,load=async(data,hash)=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data,calldata_hash:hash})}];await elements.file.onchange({target:elements.file})},choose=async id=>{elements.wallet.value=id;elements.wallet.onchange();await elements.connect.onclick()},confirm=()=>{elements.confirm.checked=true;elements.confirm.onchange()},wait=()=>new Promise(resolve=>setImmediate(resolve));await load('0x',hashA);elements.wallet.value='a';elements.wallet.onchange();let stale;if(kind.startsWith('connect')){arm=true;stale=elements.connect.onclick()}else{await elements.connect.onclick();confirm();arm=true;stale=elements.send.onclick()}await wait();if(!settle)throw Error('A verification was not deferred');await load('0x1234',hashB);await choose('b');confirm();await elements.send.onclick();const result=elements.result.textContent,bCalls=calls.b.filter(method=>method==='eth_sendTransaction').length;settle();await stale;if(storage.get(key(hashB))!=='submitted:'+txB||elements.result.textContent!==result||!elements.send.disabled||calls.a.includes('eth_sendTransaction'))throw Error('stale '+kind+' mutated B: '+JSON.stringify({calls,storage:[...storage],result:elements.result.textContent}));await elements.send.onclick();if(calls.b.filter(method=>method==='eth_sendTransaction').length!==bCalls)throw Error('B resent after stale '+kind)}
(async()=>{for(const kind of ['connect-reject','connect-resolve','presend-reject','presend-resolve'])await scenario(kind)})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_ignores_stale_clipboard_completion() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hashA='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',hashB='0x'+'11'.repeat(32),txA='0x'+'aa'.repeat(32);
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
async function scenario(mode){const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};let settle;const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:()=>new Promise((resolve,reject)=>{settle=()=>mode==='resolve'?resolve():reject(Error('clipboard failed'))})}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);const provider={request:request=>{if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return Promise.resolve([signer]);if(request.method==='web3_sha3')return Promise.resolve(request.params[0]==='0x'?hashA:hashB);if(request.method==='eth_sendTransaction')return Promise.resolve(txA);throw Error('unexpected '+request.method)},on:()=>{}};window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});const load=async(data,hash)=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data,calldata_hash:hash})}];await elements.file.onchange({target:elements.file})},connect=async()=>{elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick()},wait=()=>new Promise(resolve=>setImmediate(resolve));await load('0x',hashA);await connect();elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick();const copy=elements.copy.onclick();await wait();if(!settle)throw Error('clipboard was not deferred');await load('0x1234',hashB);await connect();const bResult=elements.result.textContent;settle();await copy;if(elements.result.textContent!==bResult||elements.confirm.disabled)throw Error('stale clipboard '+mode+' mutated B: '+elements.result.textContent)}
(async()=>{await scenario('resolve');await scenario('reject')})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_keeps_current_submission_visible_when_copy_fails() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',tx='0x'+'aa'.repeat(32),key='phase0-submission:0x72:0x2222222222222222222222222222222222222222:0x3333333333333333333333333333333333333333:0x0:0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470';
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{throw Error('clipboard unavailable')}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
const provider={request:async request=>{if(request.method==='eth_chainId')return '0x72';if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='web3_sha3')return hash;if(request.method==='eth_sendTransaction')return tx;throw Error('unexpected '+request.method)},on:()=>{}};
window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});(async()=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick();await elements.copy.onclick();if(elements.result.textContent!=='Could not copy; transaction remains submitted: '+tx||storage.get(key)!=='submitted:'+tx||elements.send.disabled!==true||elements.confirm.disabled||elements.copy.disabled)throw Error('copy failure concealed submission: '+JSON.stringify({result:elements.result.textContent,storage:[...storage]}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_surfaces_hash_when_submitted_guard_write_fails() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',tx='0x'+'aa'.repeat(32),key='phase0-submission:0x72:0x2222222222222222222222222222222222222222:0x3333333333333333333333333333333333333333:0x0:0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470';
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),copies=[];let writes=0,copyFails=true;
const window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>{writes+=1;if(writes===2)throw Error('storage full');storage.set(key,value)},removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async value=>{copies.push(value);if(copyFails)throw Error('clipboard unavailable')}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
const provider={request:async request=>{if(request.method==='eth_chainId')return '0x72';if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='web3_sha3')return hash;if(request.method==='eth_sendTransaction')return tx;throw Error('unexpected '+request.method)},on:()=>{}};
window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});(async()=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick();if(storage.get(key)!=='pending'||!elements.result.textContent.includes(tx)||!elements.result.textContent.includes('guard update failed')||elements.copy.disabled||!elements.send.disabled||elements.confirm.disabled)throw Error('known hash was hidden after guard persistence failure: '+JSON.stringify({storage:[...storage],result:elements.result.textContent}));await elements.copy.onclick();if(elements.result.textContent!=='Could not copy; transaction remains submitted: '+tx||storage.get(key)!=='pending'||elements.copy.disabled)throw Error('copy failure did not preserve surfaced hash');copyFails=false;await elements.copy.onclick();if(elements.result.textContent!=='Transaction hash copied: '+tx||copies.join(',')!==tx+','+tx||storage.get(key)!=='pending')throw Error('copy retry did not use surfaced hash')})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_recovers_stale_submitted_hash_after_reload() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',to='0x3333333333333333333333333333333333333333',hashA='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',hashB='0x'+'bb'.repeat(32),txA='0x'+'aa'.repeat(32),txB='0x'+'cc'.repeat(32),storage=new Map(),copies=[];
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
function boot(){const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}},context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async value=>copies.push(value)}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);return {elements,window}}
const key=hash=>'phase0-submission:0x72:'+signer+':'+to+':0x0:'+hash,load=async(page,data,hash)=>{page.elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to,value:'0x0',data,calldata_hash:hash})}];await page.elements.file.onchange({target:page.elements.file})},choose=async(page,id)=>{page.elements.wallet.value=id;page.elements.wallet.onchange();await page.elements.connect.onclick()},confirm=page=>{page.elements.confirm.checked=true;page.elements.confirm.onchange()},wait=()=>new Promise(resolve=>setImmediate(resolve));
const first=boot();let resolveA;const provider=id=>({request:request=>{if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return Promise.resolve([signer]);if(request.method==='web3_sha3')return Promise.resolve(request.params[0]==='0x'?hashA:hashB);if(request.method==='eth_sendTransaction'&&id==='a')return new Promise(resolve=>{resolveA=()=>resolve(txA)});if(request.method==='eth_sendTransaction')return Promise.resolve(txB);throw Error('unexpected '+request.method)},on:()=>{}}),announce=(id,provider)=>first.window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:id,name:id,rdns:id+'.example'},provider}});announce('a',provider('a'));announce('b',provider('b'));
(async()=>{await load(first,'0x',hashA);await choose(first,'a');confirm(first);const sendA=first.elements.send.onclick();await wait();if(!resolveA||storage.get(key(hashA))!=='pending')throw Error('A did not receive a pending guard');await load(first,'0x1234',hashB);await choose(first,'b');confirm(first);await first.elements.send.onclick();const bResult=first.elements.result.textContent;resolveA();await sendA;if(storage.get(key(hashA))!=='submitted:'+txA||first.elements.result.textContent!==bResult)throw Error('stale A result mutated B or was not guarded: '+JSON.stringify({storage:[...storage],result:first.elements.result.textContent}));const reloaded=boot();await load(reloaded,'0x',hashA);if(reloaded.elements.result.textContent!=='Recovered submitted public transaction hash: '+txA||reloaded.elements.copy.disabled||!reloaded.elements.send.disabled)throw Error('submitted guard did not recover its hash: '+JSON.stringify({result:reloaded.elements.result.textContent,copy:reloaded.elements.copy.disabled}));await reloaded.elements.copy.onclick();if(copies.join(',')!==txA||reloaded.elements.result.textContent!=='Transaction hash copied: '+txA)throw Error('recovered hash was not copyable without a selected wallet')})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_fails_closed_when_rejection_guard_clear_fails() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8'),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',key='phase0-submission:0x72:'+signer+':0x3333333333333333333333333333333333333333:0x0:'+hash;
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:()=>{throw Error('storage unavailable')}}},context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
const provider={request:async request=>{if(request.method==='eth_chainId')return '0x72';if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='web3_sha3')return hash;if(request.method==='eth_sendTransaction')throw Object.assign(Error('rejected'),{code:4001});throw Error('unexpected '+request.method)},on:()=>{}};
window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});(async()=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick();if(storage.get(key)!=='pending'||!elements.send.disabled||!elements.confirm.disabled||!elements.copy.disabled||!elements.result.textContent.includes('Pending submission guard could not be cleared')||elements.result.textContent.includes('Wallet rejected'))throw Error('rejected send was not fail-closed after guard-clear failure: '+JSON.stringify({storage:[...storage],result:elements.result.textContent,send:elements.send.disabled,confirm:elements.confirm.disabled}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_refuses_guard_added_during_presend_verification() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8');
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',key='phase0-submission:0x72:0x2222222222222222222222222222222222222222:0x3333333333333333333333333333333333333333:0x0:'+hash,calls=[];
const window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
let addGuard=false;const provider={request:request=>{calls.push(request.method);if(request.method==='eth_chainId')return Promise.resolve('0x72');if(request.method==='eth_requestAccounts')return Promise.resolve([signer]);if(request.method==='eth_accounts'){if(addGuard)storage.set(key,'pending');return Promise.resolve([signer])}if(request.method==='web3_sha3')return Promise.resolve(hash);if(request.method==='eth_sendTransaction')return Promise.resolve('0x'+'aa'.repeat(32));throw Error('unexpected '+request.method)},on:()=>{}};
window.dispatchEvent({type:'eip6963:announceProvider',detail:{info:{uuid:'a',name:'Wallet A',rdns:'a.example'},provider}});(async()=>{elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='a';elements.wallet.onchange();await elements.connect.onclick();elements.confirm.checked=true;elements.confirm.onchange();addGuard=true;await elements.send.onclick();if(calls.includes('eth_sendTransaction')||storage.get(key)!=='pending')throw Error('existing guard was overwritten or dispatched: '+JSON.stringify({calls,storage:[...storage]}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_routes_eip6963_wallet_through_verification_and_submission() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8');
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map();
const window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};
vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
const signer='0x2222222222222222222222222222222222222222',to='0x3333333333333333333333333333333333333333',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',tx='0x'+'aa'.repeat(32),calls=[[],[]];
const provider=index=>{const handlers={};return{request:async request=>{calls[index].push(request.method);if(request.method==='eth_chainId')return '0x72';if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='web3_sha3')return hash;if(request.method==='eth_sendTransaction')return tx;throw Error('unexpected '+request.method)},on:(name,fn)=>(handlers[name]??=[]).push(fn),emit:name=>(handlers[name]??[]).forEach(fn=>fn())}};
const first=provider(0),second=provider(1),announce=detail=>window.dispatchEvent({type:'eip6963:announceProvider',detail});
announce({info:{uuid:'wallet-1',name:'First Wallet',rdns:'first.example'},provider:first});announce({info:{uuid:'wallet-2',name:'Second Wallet',rdns:'second.example'},provider:second});announce({info:{uuid:'wallet-2',name:'Duplicate UUID',rdns:'duplicate.example'},provider:provider(1)});announce({info:{uuid:'wallet-3',name:'Duplicate provider',rdns:'duplicate.example'},provider:second});announce({info:{uuid:'',name:'Malformed',rdns:'bad.example'},provider:provider(1)});announce({info:{uuid:'malformed',name:'Malformed',rdns:'bad.example'},provider:{}});
(async()=>{if('ethereum' in window)throw Error('EIP-6963-only scenario unexpectedly has window.ethereum');if(elements.wallet.options.length!==3||elements.wallet.options[1].textContent!=='First Wallet'||elements.wallet.options[2].textContent!=='Second Wallet')throw Error('EIP-6963 choices were not safely deduplicated');elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to,value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});if(!elements.signer.textContent||elements.signer.textContent!==signer||!elements.purpose.textContent||elements.data.textContent!=='0x')throw Error('valid sign request did not render');elements.wallet.value='wallet-2';elements.wallet.onchange();if(elements.connect.disabled)throw Error('selected wallet did not enable connection');await elements.connect.onclick();if(calls[0].length||calls[1].join(',')!=='eth_requestAccounts,eth_chainId,web3_sha3'||elements.result.textContent!=='Wallet, Coston2 chain, and calldata hash verified.'||elements.confirm.disabled)throw Error('selected EIP-6963 wallet was not fully verified: '+JSON.stringify(calls));elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick();if(calls[0].length||calls[1].join(',')!=='eth_requestAccounts,eth_chainId,web3_sha3,eth_chainId,eth_accounts,web3_sha3,eth_sendTransaction')throw Error('submission RPC calls escaped selected wallet: '+JSON.stringify(calls));if(!elements.result.textContent.includes(tx)||elements.copy.disabled||[...storage.values()].join(',')!=='submitted:'+tx.toLowerCase())throw Error('submission result or storage was not recorded');elements.wallet.value='wallet-1';elements.wallet.onchange();if(!elements.confirm.disabled||!elements.send.disabled||elements.confirm.checked||!elements.result.textContent.includes('Selected wallet'))throw Error('wallet selection did not reset verification');second.emit('accountsChanged');if(!elements.confirm.disabled||!elements.send.disabled)throw Error('wallet account change did not reset verification')})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_signer_routes_explicit_legacy_wallet_selection() -> None:
    page = Path(__file__).parents[1] / "scripts" / "coston2_signer.html"
    harness = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8');
class Element{constructor(id){this.id=id;this.disabled=false;this.checked=false;this.textContent='';this.value='';this.options=[];this.files=[]}appendChild(node){this.options.push(node)}addEventListener(name,fn){this['on'+name]=fn}}
const ids=['file','wallet','connect','purpose','signer','chain','target','value','data','hash','confirm','send','result','copy'],elements=Object.fromEntries(ids.map(id=>[id,new Element(id)])),listeners={},storage=new Map(),signer='0x2222222222222222222222222222222222222222',hash='0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470',tx='0x'+'aa'.repeat(32),calls=[[],[]];
const provider=index=>({request:async request=>{calls[index].push(request.method);if(request.method==='eth_chainId')return '0x72';if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [signer];if(request.method==='web3_sha3')return hash;if(request.method==='eth_sendTransaction')return tx;throw Error('unexpected '+request.method)}}),legacyOne=provider(0),legacyTwo=provider(1);
const window={addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),dispatchEvent:event=>(listeners[event.type]??[]).forEach(fn=>fn(event)),localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},ethereum:{providers:[legacyOne,legacyTwo]}};
const context={window,document:{getElementById:id=>elements[id]??null,createElement:()=>new Element('option')},Event:class{constructor(type){this.type=type}},navigator:{clipboard:{writeText:async()=>{}}},console,localStorage:window.localStorage};vm.createContext(context);vm.runInContext(html.match(/<script>([\s\S]*)<\/script>/)[1],context);
(async()=>{if(elements.wallet.options.length!==3||elements.wallet.options[1].textContent!=='Legacy wallet 1'||elements.wallet.options[2].textContent!=='Legacy wallet 2')throw Error('legacy wallets were not rendered as explicit choices');elements.file.files=[{text:async()=>JSON.stringify({version:'1',purpose:'fdc-request',chain_id:'0x72',signer,to:'0x3333333333333333333333333333333333333333',value:'0x0',data:'0x',calldata_hash:hash})}];await elements.file.onchange({target:elements.file});elements.wallet.value='legacy-1';elements.wallet.onchange();await elements.connect.onclick();elements.confirm.checked=true;elements.confirm.onchange();await elements.send.onclick();if(calls[0].length||calls[1].join(',')!=='eth_requestAccounts,eth_chainId,web3_sha3,eth_chainId,eth_accounts,web3_sha3,eth_sendTransaction'||storage.size!==1||[...storage.values()][0]!=='submitted:'+tx||!elements.result.textContent.includes(tx))throw Error('legacy provider routing was not deterministic: '+JSON.stringify({calls,storage:[...storage]}))})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(["node", "-e", harness, str(page)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_prepare_fdc_binds_verifier_bytes_fee_and_browser_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, fdc_hub, fee_config = "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "44" * 20
    request_bytes = "0x" + _fdc_request("AA" * 32, signer).hex()
    state = _state("poll-xaman", signer, {"fdc_hub": fdc_hub})
    state["xrpl_transaction_hash"] = "AA" * 32
    store = StateStore(tmp_path)
    store.write(state)
    rpc = _FakeRpc({
        "server_info": {"info": {"network_id": 1}}, "eth_chainId": "0x72",
        "eth_call": lambda params: _abi_result(fee_config) if params[0]["data"] == live._selector("fdcRequestFeeConfigurations()") else _word_hex(9),
    })
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_post_public_json", lambda url, body, headers: {"abiEncodedRequest": request_bytes})
    monkeypatch.setattr(live, "_require_live_contracts", lambda client, saved, keys: {"fdc_hub": fdc_hub})

    sign_request = live.prepare_fdc(_config(signer), store)

    assert sign_request["to"] == fdc_hub
    assert sign_request["value"] == "0x9"
    assert sign_request["data"].startswith(live._selector("requestAttestation(bytes)"))
    assert store.read()["fdc_request_bytes"] == request_bytes
    request_file = tmp_path / "fdc-sign-request.json"
    assert request_file.exists() and os.stat(request_file).st_mode & 0o777 == 0o600
    assert set(__import__("json").loads(request_file.read_text())) == {"version", "purpose", "chain_id", "signer", "to", "value", "data", "calldata_hash"}


def test_poll_xaman_requires_signed_final_payment_bound_to_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = "0x" + "22" * 20
    state = _state("create-xaman", signer, {})
    state.update({"xaman_uuid": "11111111-1111-1111-1111-111111111111", "xaman_next": {}, "xaman_intent_digest": "0x" + "11" * 32})
    store = StateStore(tmp_path)
    store.write(state)
    tx_hash = "AA" * 32
    xrpl_result = {"hash": tx_hash.lower(), "validated": True, "TransactionType": "Payment", "Flags": 0, "Account": "rSource", "Destination": "rVault", "Amount": "100", "ledger_index": 9, "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": "100"}, "Memos": [{"Memo": {"MemoData": state["memo_data_hex"]}}]}
    rpc = _FakeRpc({"server_info": {"info": {"network_id": 1, "validated_ledger": {"seq": 11}}}, "eth_chainId": "0x72", "tx": xrpl_result})
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_get_json", lambda url, headers, host: {"meta": {"resolved": True, "signed": True}, "response": {"txid": tx_hash, "dispatched_nodetype": "TESTNET"}})

    polled = live.poll_xaman(_config(signer), store)

    assert polled["stage"] == "poll-xaman"
    assert polled["xrpl_transaction_hash"] == tx_hash
    assert polled["xrpl_confirmations"] == 3


def test_poll_xaman_rejects_xrpl_hash_different_from_xaman_txid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, tx_hash = "0x" + "22" * 20, "AA" * 32
    state = _state("create-xaman", signer, {})
    state.update({"xaman_uuid": "11111111-1111-1111-1111-111111111111", "xaman_next": {}, "xaman_intent_digest": "0x" + "11" * 32})
    store = StateStore(tmp_path); store.write(state)
    transaction = {"hash": "BB" * 32, "validated": True, "TransactionType": "Payment", "Flags": 0, "Account": "rSource", "Destination": "rVault", "Amount": "100", "ledger_index": 9, "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": "100"}, "Memos": [{"Memo": {"MemoData": state["memo_data_hex"]}}]}
    rpc = _FakeRpc({"server_info": {"info": {"network_id": 1, "validated_ledger": {"seq": 11}}}, "eth_chainId": "0x72", "tx": transaction})
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_get_json", lambda url, headers, host: {"meta": {"resolved": True, "signed": True}, "response": {"txid": tx_hash, "dispatched_nodetype": "TESTNET"}})
    with pytest.raises(LiveError, match="hash does not match"):
        live.poll_xaman(_config(signer), store)


def test_poll_xaman_rejects_only_two_confirmations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = "0x" + "22" * 20
    state = _state("create-xaman", signer, {})
    state.update({"xaman_uuid": "11111111-1111-1111-1111-111111111111", "xaman_next": {}, "xaman_intent_digest": "0x" + "11" * 32})
    store = StateStore(tmp_path)
    store.write(state)
    transaction = {"hash": "aa" * 32, "validated": True, "TransactionType": "Payment", "Flags": 0, "Account": "rSource", "Destination": "rVault", "Amount": "100", "ledger_index": 9, "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": "100"}, "Memos": [{"Memo": {"MemoData": state["memo_data_hex"]}}]}
    rpc = _FakeRpc({"server_info": {"info": {"network_id": 1, "validated_ledger": {"seq": 10}}}, "eth_chainId": "0x72", "tx": transaction})
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_get_json", lambda url, headers, host: {"meta": {"resolved": True, "signed": True}, "response": {"txid": "AA" * 32, "dispatched_nodetype": "TESTNET"}})

    with pytest.raises(LiveError, match="fewer than three"):
        live.poll_xaman(_config(signer), store)


@pytest.mark.parametrize("meta,response", [
    ({"resolved": False, "signed": True}, {"dispatched_nodetype": "TESTNET"}),
    ({"resolved": True, "signed": False}, {"dispatched_nodetype": "TESTNET"}),
    ({"resolved": True, "signed": True}, {}),
    ({"resolved": True, "signed": True}, {"dispatched_nodetype": "MAINNET"}),
])
def test_poll_xaman_rejects_unresolved_unsigned_or_non_testnet_payloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meta: dict[str, bool], response: dict[str, str]) -> None:
    signer = "0x" + "22" * 20
    state = _state("create-xaman", signer, {})
    state.update({"xaman_uuid": "11111111-1111-1111-1111-111111111111", "xaman_next": {}, "xaman_intent_digest": "0x" + "11" * 32})
    store = StateStore(tmp_path); store.write(state)
    response["txid"] = "AA" * 32
    monkeypatch.setattr(live, "_get_json", lambda url, headers, host: {"meta": meta, "response": response})

    with pytest.raises(LiveError, match="Xaman payload"):
        live.poll_xaman(_config(signer), store)


def test_record_fdc_rejects_substituted_browser_calldata_and_binds_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, fdc_hub = "0x" + "22" * 20, "0x" + "33" * 20
    request = make_sign_request(signer, fdc_hub, 9, "0x1234", "fdc-request")
    manager = "0x" + "55" * 20
    state = _state("prepare-fdc", signer, {"fdc_hub": fdc_hub, "flare_systems_manager": manager})
    state["fdc_sign_request"] = request
    store = StateStore(tmp_path)
    store.write(state)
    tx_hash = "0x" + "66" * 32
    block_hash = "0x" + "77" * 32
    transaction = {"hash": tx_hash, "from": signer, "to": fdc_hub, "value": "0x9", "input": "0x1234", "blockNumber": "0x10", "blockHash": block_hash}
    rpc = _FakeRpc({
        "eth_getTransactionByHash": transaction,
        "eth_chainId": "0x72", "eth_getTransactionReceipt": {"status": "0x1", "transactionHash": tx_hash, "blockNumber": "0x10", "blockHash": block_hash},
        "eth_getBlockByNumber": {"timestamp": "0x64", "hash": block_hash},
        "eth_call": lambda params: _word_hex(10 if params[0]["data"] == live._selector("firstVotingRoundStartTs()") else 9),
    })
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_require_live_contracts", lambda client, saved, keys: {"fdc_hub": fdc_hub, "flare_systems_manager": manager})

    recorded = live.record_fdc(_config(signer), store, tx_hash)

    assert recorded["fdc_round_id"] == "10"
    assert recorded["fdc_transaction_hash"] == tx_hash


def test_record_fdc_rejects_wrong_chain_and_transaction_block_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, target, tx_hash = "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "66" * 32
    state = _state("prepare-fdc", signer, {"flare_systems_manager": "0x" + "55" * 20})
    state["fdc_sign_request"] = make_sign_request(signer, target, 0, "0x1234", "fdc-request")
    store = StateStore(tmp_path); store.write(state)
    rpc = _FakeRpc({"eth_chainId": "0x71"})
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    with pytest.raises(LiveError, match="Coston2"):
        live.record_fdc(_config(signer), store, tx_hash)


def test_live_registry_mismatch_refuses_stale_target(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state("poll-xaman", "0x" + "22" * 20, {"fdc_hub": "0x" + "33" * 20})
    monkeypatch.setattr(live, "_registry", lambda client, name: "0x" + "44" * 20)
    with pytest.raises(LiveError, match="registry"):
        live._require_live_contracts(_FakeRpc({}), state, ("fdc_hub",))


def test_live_registry_mismatch_refuses_stale_flare_systems_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state("prepare-fdc", "0x" + "22" * 20, {"flare_systems_manager": "0x" + "33" * 20})
    monkeypatch.setattr(live, "_registry", lambda client, name: "0x" + "44" * 20)
    with pytest.raises(LiveError, match="registry"):
        live._require_live_contracts(_FakeRpc({}), state, ("flare_systems_manager",))


def test_record_fdc_rejects_fetched_block_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, hub, manager, tx_hash = "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "44" * 20, "0x" + "66" * 32
    state = _state("prepare-fdc", signer, {"fdc_hub": hub, "flare_systems_manager": manager})
    state["fdc_sign_request"] = make_sign_request(signer, hub, 0, "0x1234", "fdc-request")
    store = StateStore(tmp_path); store.write(state)
    receipt_hash = "0x" + "77" * 32
    transaction = {"hash": tx_hash, "from": signer, "to": hub, "value": "0x0", "input": "0x1234", "blockNumber": "0x1", "blockHash": receipt_hash}
    receipt = {"status": "0x1", "transactionHash": tx_hash, "blockNumber": "0x1", "blockHash": receipt_hash}
    rpc = _FakeRpc({"eth_chainId": "0x72", "eth_getTransactionByHash": transaction, "eth_getTransactionReceipt": receipt, "eth_getBlockByNumber": {"hash": "0x" + "88" * 32, "timestamp": "0x64"}})
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_require_live_contracts", lambda client, saved, keys: {"fdc_hub": hub, "flare_systems_manager": manager})
    with pytest.raises(LiveError, match="block hash"):
        live.record_fdc(_config(signer), store, tx_hash)


def test_inspect_targets_master_account_nonce_and_uses_live_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    signer, controller, asset, personal = "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "44" * 20, "0x" + "55" * 20
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(live, "_validate_networks", lambda xrpl, flare: None)
    monkeypatch.setattr(live, "_resolve_contracts", lambda flare: {"master_account_controller": controller, "asset_manager": asset})
    monkeypatch.setattr(live, "_eth_address_call", lambda client, target, data: personal)
    def quantity(client: object, target: str, data: str) -> int:
        calls.append((target, data)); return 7
    monkeypatch.setattr(live, "_eth_quantity_call", quantity)
    monkeypatch.setattr(live, "_live_settings", lambda client, target: {"lot_size_uba": 10_000_000, "fee_bips": 25, "minimum_fee_uba": 100_000, "core_vault": "rVault"})

    state = live._inspect_state(_config(signer), _FakeRpc({}), _FakeRpc({}))

    assert (controller, live._selector("getNonce(address)") + live._abi_address(personal).hex()) in calls
    assert state["gross_drops"] == 10_100_000


def test_create_xaman_posts_exact_unsigned_payment_and_blocks_changed_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = "0x" + "22" * 20
    baseline = _state("inspect", signer, {})
    baseline["gross_drops"] = 10_100_000
    store = StateStore(tmp_path); store.write(baseline)
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (_FakeRpc({}), _FakeRpc({})))
    monkeypatch.setattr(live, "_inspect_state", lambda config, xrpl, flare: dict(baseline))
    monkeypatch.setattr(live, "_post_json", lambda url, body, key, secret: posted.append(dict(body)) or {"uuid": "11111111-1111-1111-1111-111111111111", "next": {}})
    monkeypatch.setattr(live, "_get_json", lambda url, headers, host: {"payload": {"request_json": posted[0]["txjson"]}, "meta": {"force_network": "TESTNET"}, "next": {"always": "https://evil.example"}})

    created = live.create_xaman(_config(signer), store)

    txjson = posted[0]["txjson"]
    assert isinstance(txjson, dict) and "DestinationTag" not in txjson
    assert posted[0]["options"] == {"force_network": "TESTNET"}
    assert created["next"] == {"always": "https://xumm.app/sign/11111111-1111-1111-1111-111111111111"}
    assert len(txjson["Memos"][0]["Memo"]["MemoData"]) == 84
    assert txjson["Memos"][0]["Memo"]["MemoData"].upper() == txjson["Memos"][0]["Memo"]["MemoData"]
    changed = dict(baseline); changed["nonce"] = "8"
    other = StateStore(tmp_path / "changed"); other.write(baseline)
    monkeypatch.setattr(live, "_inspect_state", lambda config, xrpl, flare: changed)
    with pytest.raises(LiveError, match="commitment changed"):
        live.create_xaman(_config(signer), other)


def test_xaman_uuid_reconciliation_never_posts_a_second_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = "0x" + "22" * 20
    baseline = _state("inspect", signer, {}); store = StateStore(tmp_path); store.write(baseline)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (_FakeRpc({}), _FakeRpc({})))
    monkeypatch.setattr(live, "_inspect_state", lambda config, xrpl, flare: dict(baseline))
    def interrupted(url: str, body: dict[str, object], key: str, secret: str) -> dict[str, object]:
        sent.append(body); raise LiveError("network interrupted")
    monkeypatch.setattr(live, "_post_json", interrupted)
    with pytest.raises(LiveError, match="interrupted"):
        live.create_xaman(_config(signer), store)
    monkeypatch.setattr(live, "_get_json", lambda url, headers, host: {"payload": {"request_json": sent[0]["txjson"]}, "meta": {}, "next": {}})
    with pytest.raises(LiveError, match="malformed"):
        live.create_xaman(_config(signer), store, xaman_uuid="11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(live, "_get_json", lambda url, headers, host: {"payload": {"request_json": sent[0]["txjson"]}, "meta": {"force_network": "MAINNET"}, "next": {}})
    with pytest.raises(LiveError, match="Testnet"):
        live.create_xaman(_config(signer), store, xaman_uuid="11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(live, "_get_json", lambda url, headers, host: {"payload": {"request_json": sent[0]["txjson"]}, "meta": {"force_network": "TESTNET"}, "next": {}})

    result = live.create_xaman(_config(signer), store, xaman_uuid="11111111-1111-1111-1111-111111111111")

    assert result["uuid"] == "11111111-1111-1111-1111-111111111111" and len(sent) == 1


def test_posted_xaman_uuid_stays_pending_until_get_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, uuid = "0x" + "22" * 20, "11111111-1111-1111-1111-111111111111"
    baseline = _state("inspect", signer, {}); store = StateStore(tmp_path); store.write(baseline)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (_FakeRpc({}), _FakeRpc({})))
    monkeypatch.setattr(live, "_inspect_state", lambda config, xrpl, flare: dict(baseline))
    monkeypatch.setattr(live, "_post_json", lambda url, body, key, secret: sent.append(dict(body)) or {"uuid": uuid, "next": {"always": "https://evil.example"}})
    monkeypatch.setattr(live, "_get_json", lambda *args: (_ for _ in ()).throw(LiveError("GET failed")))
    with pytest.raises(LiveError, match="GET failed"):
        live.create_xaman(_config(signer), store)
    pending = store.read()
    assert pending["stage"] == "xaman-pending-reconcile" and "xaman_next" not in pending
    monkeypatch.setattr(live, "_get_json", lambda *args: {"payload": {"request_json": sent[0]["txjson"]}, "meta": {"force_network": "TESTNET"}})

    reconciled = live.create_xaman(_config(signer), store)

    assert len(sent) == 1 and reconciled["next"] == {"always": "https://xumm.app/sign/11111111-1111-1111-1111-111111111111"}


def test_malformed_xaman_uuid_is_refused_before_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = "0x" + "22" * 20
    state = _state("inspect", signer, {}); store = StateStore(tmp_path); store.write(state)
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (_FakeRpc({}), _FakeRpc({})))
    monkeypatch.setattr(live, "_inspect_state", lambda config, xrpl, flare: dict(state))
    monkeypatch.setattr(live, "_post_json", lambda *args: {"uuid": "not-a-uuid"})
    with pytest.raises(LiveError, match="UUID"):
        live.create_xaman(_config(signer), store)


def test_xaman_reconciliation_rejects_missing_nested_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, uuid = "0x" + "22" * 20, "11111111-1111-1111-1111-111111111111"
    state = _state("inspect", signer, {}); store = StateStore(tmp_path); store.write(state)
    payment, digest = live._xaman_payment(_config(signer), state)
    monkeypatch.setattr(live, "_get_json", lambda *args: {"meta": {"force_network": "TESTNET"}})
    with pytest.raises(LiveError, match="malformed"):
        live._reconcile_xaman(_config(signer), store, state, payment, digest, uuid)


def test_finalize_requires_coston2_and_exact_mined_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, target, tx_hash = "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "66" * 32
    state = _state("prepare-execute", signer, {})
    state["execute_sign_request"] = make_sign_request(signer, target, 0, "0x1234", "execute-direct-mint")
    store = StateStore(tmp_path); store.write(state)
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (_FakeRpc({}), _FakeRpc({"eth_chainId": "0x71"})))
    with pytest.raises(LiveError, match="Coston2"):
        live.finalize(_config(signer), store, tx_hash)
    transaction = {"hash": "0x" + "77" * 32, "from": signer, "to": target, "value": "0x0", "input": "0x1234", "blockNumber": "0x1"}
    receipt = {"transactionHash": tx_hash, "blockNumber": "0x1", "status": "0x1"}
    rpc = _FakeRpc({"eth_chainId": "0x72", "eth_getTransactionByHash": transaction, "eth_getTransactionReceipt": receipt})
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    with pytest.raises(LiveError, match="transaction hash"):
        live.finalize(_config(signer), store, tx_hash)


def test_finalize_sends_candidate_to_independent_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer, target, tx_hash = "0x" + "22" * 20, "0x" + "33" * 20, "0x" + "66" * 32
    state = _state("prepare-execute", signer, {})
    state["execute_sign_request"] = make_sign_request(signer, target, 0, "0x1234", "execute-direct-mint")
    store = StateStore(tmp_path); store.write(state)
    transaction = {"hash": tx_hash, "from": signer, "to": target, "value": "0x0", "input": "0x1234", "blockNumber": "0x1"}
    receipt = {"transactionHash": tx_hash, "blockNumber": "0x1", "status": "0x1"}
    rpc = _FakeRpc({"eth_chainId": "0x72", "eth_getTransactionByHash": transaction, "eth_getTransactionReceipt": receipt})
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_candidate_evidence", lambda *args: {"schema_version": 1, "status": "completed"})
    import app.xrp.rpc as rpc_module
    monkeypatch.setattr(rpc_module, "validate_evidence", lambda evidence, timeout: seen.append(evidence))

    result = live.finalize(_config(signer), store, tx_hash)

    assert result == {"schema_version": 1, "status": "completed"}
    assert seen == [result]


def test_mined_transaction_requires_requested_hash_and_matching_blocks() -> None:
    tx_hash = "0x" + "11" * 32
    transaction = {"hash": tx_hash, "blockNumber": "0x1", "blockHash": "0x" + "22" * 32}
    receipt = {"transactionHash": tx_hash, "blockNumber": "0x1", "blockHash": "0x" + "22" * 32}
    live._validate_mined_transaction(transaction, receipt, tx_hash, "test")
    receipt["blockHash"] = "0x" + "33" * 32
    with pytest.raises(LiveError, match="block hashes"):
        live._validate_mined_transaction(transaction, receipt, tx_hash, "test")


def test_prepare_execute_rejects_unfinalized_round_then_builds_proof_bound_calldata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = "0x" + "22" * 20
    contracts = {"fdc_verification": "0x" + "33" * 20, "relay": "0x" + "44" * 20, "asset_manager": "0x" + "55" * 20}
    state = _state("record-fdc", signer, contracts)
    state.update({"fdc_round_id": "7", "fdc_request_bytes": "0x" + "aa" * 32, "xrpl_transaction_hash": "AA" * 32})
    store = StateStore(tmp_path)
    store.write(state)
    rpc = _FakeRpc({"eth_chainId": "0x72", "eth_call": lambda params: _word_hex(1)})
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_post_public_json", lambda url, body, headers: {"response_hex": "0x" + _xrp_response("AA" * 32, signer, 7).hex(), "proof": ["0x" + "77" * 32]})
    monkeypatch.setattr(live, "_require_live_contracts", lambda client, saved, keys: contracts)

    sign_request = live.prepare_execute(_config(signer), store)

    assert sign_request["to"] == contracts["asset_manager"]
    assert sign_request["value"] == "0x0"
    assert sign_request["data"].startswith(live.EXECUTE_DIRECT_MINTING_WITH_DATA_SELECTOR)
    assert store.read()["stage"] == "prepare-execute"
    execute_file = tmp_path / "execute-sign-request.json"
    assert execute_file.exists() and set(__import__("json").loads(execute_file.read_text())) == {"version", "purpose", "chain_id", "signer", "to", "value", "data", "calldata_hash"}


def test_prepare_recovery_execute_builds_direct_mint_with_empty_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    signer = "0x" + "22" * 20
    contracts = {"fdc_verification": "0x" + "33" * 20, "relay": "0x" + "44" * 20, "asset_manager": "0x" + "55" * 20}
    state = _state("record-fdc", signer, contracts)
    state.update({
        "fdc_round_id": "7",
        "fdc_request_bytes": "0x" + "aa" * 32,
        "xrpl_transaction_hash": "AA" * 32,
        "packed_user_operation_hex": "",
        "recovery_target_transaction_hash": "BB" * 32,
    })
    store = StateStore(tmp_path)
    store.write(state)
    rpc = _FakeRpc({"eth_chainId": "0x72", "eth_call": lambda params: _word_hex(1)})
    monkeypatch.setattr(live, "_trusted_clients", lambda timeout: (rpc, rpc))
    monkeypatch.setattr(live, "_post_public_json", lambda url, body, headers: {"response_hex": "0x" + _xrp_response("AA" * 32, signer, 7).hex(), "proof": ["0x" + "77" * 32]})
    monkeypatch.setattr(live, "_require_live_contracts", lambda client, saved, keys: contracts)

    sign_request = live.prepare_recovery_execute(_config(signer), store)

    assert sign_request["to"] == contracts["asset_manager"]
    assert sign_request["value"] == "0x0"
    assert sign_request["data"].startswith(live.EXECUTE_DIRECT_MINTING_WITH_DATA_SELECTOR)
    assert sign_request["data"].endswith("0" * 64)
    assert store.read()["stage"] == "prepare-execute"


class _FakeRpc:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def request(self, method: str, params: list[object] | None = None) -> object:
        result = self.values[method]
        return result(params) if callable(result) else result


def _state(stage: str, signer: str, contracts: dict[str, str]) -> dict[str, object]:
    payload, instruction = build_checkpoint_user_op("0x" + "11" * 20, 7)
    return {"version": 1, "stage": stage, "xrpl_address": "rSource", "signer": signer, "personal_account": "0x" + "11" * 20, "nonce": "7", "packed_user_operation_hex": payload.hex(), "memo_data_hex": instruction.memo_data_hex, "gross_drops": 100, "settings": {"core_vault": "rVault"}, "contracts": contracts}


def _config(signer: str) -> LiveConfig:
    return LiveConfig("key", "secret", "rSource", signer, "https://verifier.example", "verifier-key", "https://da.example")


def _word_hex(value: int) -> str:
    return "0x" + live._word(value).hex()


def _abi_result(address: str) -> str:
    return "0x" + live._abi_address(address).hex()


def _fdc_request(transaction_hash: str, signer: str) -> bytes:
    return b"XRPPayment".ljust(32, b"\0") + b"testXRP".ljust(32, b"\0") + b"\x11" * 32 + bytes.fromhex(transaction_hash) + live._abi_address(signer)


def _xrp_response(tx_hash: str, signer: str, round_id: int = 1) -> bytes:
    source = b"rSource"
    memo = bytes.fromhex("fe00")
    response_body_head = [
        live._word(1), live._word(2), live._word(15 * 32), live._word(0), live._word(0),
        live._word(0), live._word(0), live._word(0), live._word(0), live._word(0), live._word(1),
        live._word(15 * 32 + len(live._abi_bytes(source))), live._word(0), live._word(0), live._word(0),
    ]
    response_body = b"".join(response_body_head) + live._abi_bytes(source) + live._abi_bytes(memo)
    response_head = [
        b"XRPPayment".ljust(32, b"\0"), b"testXRP".ljust(32, b"\0"), live._word(round_id), live._word(2),
        bytes.fromhex(tx_hash), live._abi_address(signer), live._word(7 * 32),
    ]
    return live._word(32) + b"".join(response_head) + response_body
