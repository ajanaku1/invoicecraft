#!/usr/bin/env python3
"""Fail when scoped project files contain private keys or credential material."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


class SecretScanError(ValueError):
    """Raised when a file contains likely private credential material."""


_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[opusr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})\b")
_ASSIGNMENT = re.compile(
    r"(?im)\b(?:[a-z0-9]+_)*(?:api[_-]?(?:secret|key)|private[_-]?key|passphrase|access[_-]?token)\b"
    r"\s*[=:]\s*[\"']?([A-Za-z0-9_./+=:-]{20,})"
)
_MNEMONIC = re.compile(
    r"(?im)\b(?:mnemonic|seed(?:_phrase)?)\b\s*[=:]\s*[\"']"
    r"([a-z]+(?:\s+[a-z]+){11,23})[\"']"
)
_PLACEHOLDERS = ("placeholder", "example", "dummy", "fake", "redacted", "your-", "test-")


def _files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if not path.exists():
            raise SecretScanError(f"scan path is missing: {path}")
        if path.name in {".env", ".env.local"}:
            raise SecretScanError("environment secret file is in scan scope")
        candidates = path.rglob("*") if path.is_dir() else [path]
        files.extend(candidate for candidate in candidates if candidate.is_file() and not candidate.is_symlink())
    return sorted(set(files))


def _text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SecretScanError(f"cannot read {path}") from error
    if b"\0" in raw or len(raw) > 2_000_000:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _assignment_is_secret(value: str) -> bool:
    lowered = value.lower()
    return not any(marker in lowered for marker in _PLACEHOLDERS)


def _scan_text(path: Path, text: str) -> None:
    if _PRIVATE_KEY.search(text) or _TOKEN.search(text) or _MNEMONIC.search(text):
        raise SecretScanError(f"private credential material found in {path}")
    for match in _ASSIGNMENT.finditer(text):
        if _assignment_is_secret(match.group(1)):
            raise SecretScanError(f"secret-like assignment found in {path}")


def scan_paths(paths: list[Path]) -> None:
    for path in _files(paths):
        text = _text(path)
        if text is not None:
            _scan_text(path, text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    arguments = parser.parse_args()
    try:
        scan_paths(arguments.paths)
    except SecretScanError:
        return 1
    print("NO_SECRETS_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
