"""
Memex — Database layer using SQLite (dev) / PostgreSQL (prod)
Designed to be swapped for a vector DB or IPFS-backed store in phase 2.
"""

import os
import json
import uuid
import time
import sqlite3
import hashlib
from pathlib import Path
from typing import Optional

DB_PATH = os.environ.get("MEMEX_DB_PATH", "/app/data/memex.db")


def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS namespaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            api_key_hash TEXT,
            is_public INTEGER DEFAULT 1,
            created_at INTEGER NOT NULL,
            memory_count INTEGER DEFAULT 0,
            owner_address TEXT
        );

        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            namespace TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            embedding TEXT DEFAULT '[]',
            source_agent TEXT DEFAULT 'unknown',
            source_platform TEXT DEFAULT 'unknown',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            importance REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            owner_address TEXT,
            ipfs_cid TEXT,
            signature TEXT,
            FOREIGN KEY (namespace) REFERENCES namespaces(name)
        );

        CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
    """)
    conn.commit()
    conn.close()


def create_namespace(name: str, description: str = "", api_key: str = None) -> dict:
    conn = get_conn()
    ns_id = str(uuid.uuid4())
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest() if api_key else None
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT OR IGNORE INTO namespaces (id, name, description, api_key_hash, is_public, created_at) VALUES (?,?,?,?,?,?)",
        (ns_id, name, description, api_key_hash, 1, now)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM namespaces WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row)


def get_namespace(name: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM namespaces WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def store_memory(namespace: str, content: str, summary: str = "",
                 tags: list = None, embedding: list = None,
                 source_agent: str = "unknown", source_platform: str = "unknown",
                 importance: float = 0.5) -> dict:
    conn = get_conn()
    mem_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    conn.execute(
        """INSERT INTO memories
           (id, namespace, content, summary, tags, embedding, source_agent,
            source_platform, created_at, updated_at, importance)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (mem_id, namespace, content, summary,
         json.dumps(tags or []), json.dumps(embedding or []),
         source_agent, source_platform, now, now, importance)
    )
    conn.execute("UPDATE namespaces SET memory_count = memory_count + 1 WHERE name=?", (namespace,))
    conn.commit()
    row = conn.execute("SELECT * FROM memories WHERE id=?", (mem_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_memories(namespace: str, limit: int = 20, offset: int = 0) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM memories WHERE namespace=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (namespace, limit, offset)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def search_memories(namespace: str, query_embedding: list,
                    limit: int = 10, threshold: float = 0.3) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM memories WHERE namespace=? ORDER BY importance DESC, created_at DESC LIMIT 200",
        (namespace,)
    ).fetchall()
    conn.close()

    if not query_embedding:
        return [_row_to_dict(r) for r in rows[:limit]]

    scored = []
    for row in rows:
        emb = json.loads(row["embedding"] or "[]")
        if emb:
            score = _cosine_similarity(query_embedding, emb)
            if score >= threshold:
                d = _row_to_dict(row)
                d["score"] = round(score, 4)
                scored.append(d)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def delete_memory(memory_id: str, namespace: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM memories WHERE id=? AND namespace=?", (memory_id, namespace))
    if cur.rowcount:
        conn.execute("UPDATE namespaces SET memory_count = MAX(0, memory_count - 1) WHERE name=?", (namespace,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def keyword_search(namespace: str, query: str, limit: int = 10) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM memories WHERE namespace=? AND (content LIKE ? OR summary LIKE ? OR tags LIKE ?) ORDER BY importance DESC LIMIT ?",
        (namespace, f"%{query}%", f"%{query}%", f"%{query}%", limit)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["embedding"] = json.loads(d.get("embedding") or "[]")
    return d


def _cosine_similarity(a: list, b: list) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)

