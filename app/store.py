from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time

# Shared persistence for payment challenges and the daily invoice counter.
# Backed by SQLite so state survives process restarts and is consistent across
# workers on a single host (fixes the previous per-process in-memory store,
# which dropped challenges and reset invoice numbers on every restart / worker).
_DB_PATH = os.getenv("DB_PATH", os.path.join(tempfile.gettempdir(), "invoicecraft.db"))
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS challenges "
        "(id TEXT PRIMARY KEY, created REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS counters (day TEXT PRIMARY KEY, n INTEGER NOT NULL)"
    )
    return conn


def add_challenge(challenge_id: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO challenges (id, created, used) VALUES (?, ?, 0)",
                (challenge_id, time.time()),
            )
            conn.commit()
        finally:
            conn.close()


def challenge_valid(challenge_id: str, ttl: int) -> bool:
    if not challenge_id:
        return False
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT created, used FROM challenges WHERE id = ?", (challenge_id,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return False
    created, used = row
    if used:
        return False
    return (time.time() - created) <= ttl


def consume_challenge(challenge_id: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE challenges SET used = 1 WHERE id = ?", (challenge_id,)
            )
            conn.commit()
        finally:
            conn.close()


def cleanup_challenges(ttl: int) -> None:
    cutoff = time.time() - ttl
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM challenges WHERE created < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


def next_invoice_seq(day: str) -> int:
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT n FROM counters WHERE day = ?", (day,)
            ).fetchone()
            n = (row[0] if row else 0) + 1
            conn.execute(
                "INSERT OR REPLACE INTO counters (day, n) VALUES (?, ?)", (day, n)
            )
            conn.commit()
            return n
        finally:
            conn.close()
