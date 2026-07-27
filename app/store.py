from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time

import httpx

# Persistence for payment challenges, used tx hashes, and the daily invoice
# counter. Uses Upstash Redis (via its REST API) when UPSTASH_REDIS_REST_URL +
# UPSTASH_REDIS_REST_TOKEN are set — durable across restarts and workers. Falls
# back to local SQLite otherwise (fine for a single ephemeral instance).
_lock = threading.Lock()


def _challenge_ttl() -> int:
    return int(os.getenv("CHALLENGE_TTL", "900"))


# ── Upstash Redis (REST) backend ───────────────────────────────────────────

def _redis_cfg():
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return url.rstrip("/"), token
    return None


def _redis(*args):
    url, token = _redis_cfg()
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json=[str(a) for a in args],
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("result")


# ── SQLite backend ─────────────────────────────────────────────────────────

def _db_path() -> str:
    return os.getenv("DB_PATH", os.path.join(tempfile.gettempdir(), "invoicecraft.db"))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS challenges "
        "(id TEXT PRIMARY KEY, created REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS counters (day TEXT PRIMARY KEY, n INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS used_txs (tx TEXT PRIMARY KEY, created REAL NOT NULL)"
    )
    return conn


# ── Public API (dispatches to Redis when configured, else SQLite) ───────────

def add_challenge(challenge_id: str) -> None:
    if _redis_cfg():
        _redis("SET", f"ch:{challenge_id}", "open", "EX", _challenge_ttl())
        return
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
    if _redis_cfg():
        return _redis("GET", f"ch:{challenge_id}") == "open"
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
    if _redis_cfg():
        _redis("DEL", f"ch:{challenge_id}")
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE challenges SET used = 1 WHERE id = ?", (challenge_id,))
            conn.commit()
        finally:
            conn.close()


def cleanup_challenges(ttl: int) -> None:
    if _redis_cfg():
        return  # Redis expires keys automatically.
    cutoff = time.time() - ttl
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM challenges WHERE created < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


def next_invoice_seq(day: str) -> int:
    if _redis_cfg():
        return int(_redis("INCR", f"invseq:{day}"))
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT n FROM counters WHERE day = ?", (day,)).fetchone()
            n = (row[0] if row else 0) + 1
            conn.execute(
                "INSERT OR REPLACE INTO counters (day, n) VALUES (?, ?)", (day, n)
            )
            conn.commit()
            return n
        finally:
            conn.close()


def incr_invoices() -> int:
    """Count a successfully generated (paid) invoice; returns the running total."""
    if _redis_cfg():
        return int(_redis("INCR", "stats:invoices"))
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT n FROM counters WHERE day = ?", ("__invoices__",)
            ).fetchone()
            n = (row[0] if row else 0) + 1
            conn.execute(
                "INSERT OR REPLACE INTO counters (day, n) VALUES (?, ?)",
                ("__invoices__", n),
            )
            conn.commit()
            return n
        finally:
            conn.close()


def get_invoice_count() -> int:
    if _redis_cfg():
        v = _redis("GET", "stats:invoices")
        return int(v) if v else 0
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT n FROM counters WHERE day = ?", ("__invoices__",)
            ).fetchone()
        finally:
            conn.close()
    return row[0] if row else 0


def tx_consumed(tx_hash: str) -> bool:
    if not tx_hash:
        return False
    key = tx_hash.lower()
    if _redis_cfg():
        return int(_redis("EXISTS", f"tx:{key}")) == 1
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT 1 FROM used_txs WHERE tx = ?", (key,)).fetchone()
        finally:
            conn.close()
    return row is not None


def consume_tx(tx_hash: str) -> None:
    if not tx_hash:
        return
    key = tx_hash.lower()
    if _redis_cfg():
        _redis("SET", f"tx:{key}", "1")
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO used_txs (tx, created) VALUES (?, ?)",
                (key, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
