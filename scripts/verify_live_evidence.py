#!/usr/bin/env python3
"""Validate completed XRPL/Coston2 evidence through bounded live RPC calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.xrp.rpc import RpcEvidenceError, validate_evidence


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("evidence", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.timeout_seconds <= 0:
        return 1
    try:
        evidence = json.loads(arguments.evidence.read_text(encoding="utf-8"))
        validate_evidence(evidence, arguments.timeout_seconds)
    except (OSError, json.JSONDecodeError, RpcEvidenceError):
        return 1
    print("RPC_EVIDENCE_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
