#!/usr/bin/env python3
"""Manual, browser-signed Phase 0 Testnet runner; never ingests private keys."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
PENDING_PLACEHOLDER_SHA256 = "8a4644549ce7d404c87a342579a767301286c4f0ee889761cee9596906dfde0e"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.xrp.live import (
    LiveConfig, LiveError, StateStore, create_xaman, finalize, inspect, load_config, poll_xaman,
    prepare_execute, prepare_fdc, record_fdc,
)
from app.xrp.rpc import RpcEvidenceError
from app.xrp.rpc import validate_evidence


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "inspect": "Read pinned Testnet values and build a public commitment.",
        "create-xaman": "Create one Xaman approval payload after fresh commitment checks.",
        "poll-xaman": "Validate signed Xaman output and XRPL finality (read-only).",
        "prepare-fdc": "Prepare a public FDC browser sign request (read-only).",
        "record-fdc": "Verify a browser-submitted FDC transaction and proof (read-only).",
        "prepare-execute": "Prepare a public direct-mint browser sign request (read-only).",
        "finalize": "Verify the execute transaction and emit candidate evidence (read-only).",
        "publish-evidence": "Validate finalized local evidence and atomically promote it.",
    }.items():
        subparser = commands.add_parser(name, help=help_text)
        if name == "create-xaman":
            subparser.add_argument("--xaman-uuid", help="Reconcile an already-created Xaman payload without POSTing another.")
        if name in {"record-fdc", "finalize"}:
            subparser.add_argument("transaction_hash", help="Public Coston2 transaction hash")
    parser.add_argument("--timeout-seconds", type=int, default=15)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    if args.timeout_seconds <= 0:
        return 1
    try:
        store = StateStore(ROOT / "evidence" / "phase0-live")
        if args.command == "publish-evidence":
            print(json.dumps(publish_evidence(store, ROOT / "evidence" / "protocol-spike.json", args.timeout_seconds), sort_keys=True))
            return 0
        config = load_config(ROOT / ".env")
        print(json.dumps(_run_command(args, config, store), sort_keys=True))
        return 0
    except (LiveError, RpcEvidenceError, OSError, json.JSONDecodeError) as error:
        print(f"phase0-live: {type(error).__name__}", file=sys.stderr)
        return 1


def _run_command(args: argparse.Namespace, config: LiveConfig, store: StateStore) -> dict[str, object]:
    timeout = args.timeout_seconds
    if args.command == "inspect":
        return _summary(inspect(config, store, timeout))
    if args.command == "create-xaman":
        return create_xaman(config, store, timeout, args.xaman_uuid)
    if args.command == "poll-xaman":
        return _summary(poll_xaman(config, store, timeout))
    if args.command == "prepare-fdc":
        prepare_fdc(config, store, timeout)
        return _request_output(store, "fdc-sign-request.json")
    if args.command == "record-fdc":
        return _summary(record_fdc(config, store, args.transaction_hash, timeout))
    if args.command == "prepare-execute":
        prepare_execute(config, store, timeout)
        return _request_output(store, "execute-sign-request.json")
    if args.command == "finalize":
        return finalize(config, store, args.transaction_hash, timeout)
    raise LiveError("unknown command")


def publish_evidence(store: StateStore, destination: Path, timeout_seconds: int) -> dict[str, object]:
    """Locally promote only independently revalidated finalized evidence."""
    state = store.read()
    if state.get("stage") != "finalize" or not isinstance(state.get("candidate_evidence"), Mapping):
        raise LiveError("publish-evidence requires finalized candidate evidence")
    candidate = dict(state["candidate_evidence"])
    _reject_secret_candidate(candidate)
    validate_evidence(candidate, timeout_seconds)
    if not destination.exists() or destination.is_symlink() or destination.parent.is_symlink():
        raise LiveError("evidence destination symlink is refused")
    encoded = json.dumps(candidate, sort_keys=True, indent=2) + "\n"
    if destination.exists():
        existing_bytes = destination.read_bytes()
        existing = json.loads(existing_bytes)
        if existing == candidate:
            return {"path": str(destination), "schema_version": candidate.get("schema_version")}
        if hashlib.sha256(existing_bytes).hexdigest() != PENDING_PLACEHOLDER_SHA256:
            raise LiveError("evidence destination is not the pending placeholder")
    descriptor, temporary = tempfile.mkstemp(prefix=".protocol-spike-", dir=destination.parent)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(encoded); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, destination); os.chmod(destination, 0o644)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {"path": str(destination), "schema_version": candidate.get("schema_version")}


def _reject_secret_candidate(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if any(marker in str(key).lower() for marker in ("secret", "api_key", "private", "authorization", "password")):
                raise LiveError("candidate evidence contains secret-like key")
            _reject_secret_candidate(nested)
    elif isinstance(value, list):
        for nested in value: _reject_secret_candidate(nested)


def _summary(state: dict[str, object]) -> dict[str, object]:
    return {key: state[key] for key in ("stage", "xrpl_address", "signer", "personal_account", "nonce", "memo_data_hex", "gross_drops", "contracts", "settings")}


def _request_output(store: StateStore, name: str) -> dict[str, str]:
    path = store.directory / name
    return {"sign_request_path": str(path), "schema": "phase0-sign-request-v1"}


if __name__ == "__main__":
    raise SystemExit(main())
