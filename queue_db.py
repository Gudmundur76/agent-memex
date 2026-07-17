"""
Submission queue: memex_submit lands here; processor promotes to mol store.
"""
import sqlite3, time, uuid, json
from typing import Optional

DB_PATH = "/data/memex.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_queue_tables():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS submission_queue (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            claim_text TEXT NOT NULL,
            domain TEXT NOT NULL DEFAULT 'general',
            submitted_by TEXT NOT NULL DEFAULT 'unknown',
            submitted_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt INTEGER,
            verdict TEXT,
            mol_id TEXT,
            error TEXT
        );
    """)
    conn.commit()
    conn.close()

def enqueue(claim_text: str, domain: str, submitted_by: str,
            idempotency_key: Optional[str] = None) -> dict:
    """Add claim to queue. Idempotent if idempotency_key provided."""
    conn = get_conn()
    if idempotency_key:
        existing = conn.execute(
            "SELECT id, status, verdict, mol_id FROM submission_queue WHERE idempotency_key=?",
            (idempotency_key,)
        ).fetchone()
        if existing:
            conn.close()
            return {"id": existing["id"], "status": existing["status"],
                    "verdict": existing["verdict"], "mol_id": existing["mol_id"],
                    "duplicate": True}
    qid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO submission_queue VALUES (?,?,?,?,?,?,'pending',0,NULL,NULL,NULL,NULL)",
        (qid, idempotency_key, claim_text, domain, submitted_by, int(time.time()))
    )
    conn.commit()
    conn.close()
    return {"id": qid, "status": "pending", "duplicate": False}

def get_status(qid: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, status, verdict, mol_id, submitted_at, last_attempt, error "
        "FROM submission_queue WHERE id=?", (qid,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def get_pending(limit: int = 10) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM submission_queue WHERE status='pending' AND attempts < 3 "
        "ORDER BY submitted_at ASC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_processing(qid: str):
    conn = get_conn()
    conn.execute(
        "UPDATE submission_queue SET status='processing', attempts=attempts+1, last_attempt=? WHERE id=?",
        (int(time.time()), qid)
    )
    conn.commit()
    conn.close()

def mark_done(qid: str, verdict: str, mol_id: Optional[str] = None):
    conn = get_conn()
    conn.execute(
        "UPDATE submission_queue SET status='done', verdict=?, mol_id=? WHERE id=?",
        (verdict, mol_id, qid)
    )
    conn.commit()
    conn.close()

def mark_failed(qid: str, error: str):
    conn = get_conn()
    conn.execute(
        "UPDATE submission_queue SET status='failed', error=? WHERE id=?",
        (error, qid)
    )
    conn.commit()
    conn.close()
