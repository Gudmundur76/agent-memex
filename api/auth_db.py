"""
Auth layer: API keys, per-key rate limits, audit log.
"""
import sqlite3, time, hashlib, os, secrets, json
from typing import Optional

DB_PATH = "/data/memex.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_auth_tables():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key_hash TEXT PRIMARY KEY,
            key_prefix TEXT NOT NULL,
            label TEXT NOT NULL,
            scopes TEXT NOT NULL DEFAULT 'check,submit,verdict,query,stats',
            rate_limit_per_hour INTEGER NOT NULL DEFAULT 60,
            submit_limit_per_hour INTEGER NOT NULL DEFAULT 20,
            created_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS rate_buckets (
            key_hash TEXT NOT NULL,
            bucket TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            window_start INTEGER NOT NULL,
            PRIMARY KEY (key_hash, bucket)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            key_prefix TEXT NOT NULL,
            tool TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            ip TEXT DEFAULT '',
            result TEXT DEFAULT 'ok'
        );
    """)
    conn.commit()
    conn.close()

def create_api_key(label: str, scopes: str = "check,submit,verdict,query,stats",
                   rate_limit: int = 60, submit_limit: int = 20) -> str:
    raw = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:8]
    conn = get_conn()
    conn.execute(
        "INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,0)",
        (key_hash, prefix, label, scopes, rate_limit, submit_limit, int(time.time()))
    )
    conn.commit()
    conn.close()
    return raw  # return full key once, never stored

def verify_api_key(raw_key: str) -> Optional[dict]:
    """Returns key record or None if invalid/revoked."""
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash=? AND revoked=0", (key_hash,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)

def check_rate_limit(key_hash: str, bucket: str, limit: int) -> bool:
    """Returns True if allowed, False if rate limited."""
    now = int(time.time())
    window = now - (now % 3600)  # hourly window
    conn = get_conn()
    row = conn.execute(
        "SELECT count, window_start FROM rate_buckets WHERE key_hash=? AND bucket=?",
        (key_hash, bucket)
    ).fetchone()
    if not row or row["window_start"] < window:
        conn.execute(
            "INSERT OR REPLACE INTO rate_buckets VALUES (?,?,1,?)",
            (key_hash, bucket, window)
        )
        conn.commit()
        conn.close()
        return True
    if row["count"] >= limit:
        conn.close()
        return False
    conn.execute(
        "UPDATE rate_buckets SET count=count+1 WHERE key_hash=? AND bucket=?",
        (key_hash, bucket)
    )
    conn.commit()
    conn.close()
    return True

def audit(key_prefix: str, tool: str, payload: str, ip: str = "", result: str = "ok"):
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]
    conn = get_conn()
    conn.execute(
        "INSERT INTO audit_log (ts, key_prefix, tool, payload_hash, ip, result) VALUES (?,?,?,?,?,?)",
        (int(time.time()), key_prefix, tool, payload_hash, ip, result)
    )
    conn.commit()
    conn.close()
