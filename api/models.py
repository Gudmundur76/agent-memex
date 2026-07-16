"""
Memex — Data models and DB schema

Designed for Phase 1 (SQLite/Postgres) with a clear migration path to
decentralized storage (IPFS content addressing, wallet-based identity).
"""

import uuid
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Memory:
    """A single memory unit stored by an agent."""
    id: str                          # UUID, future: CID (IPFS content address)
    namespace: str                   # agent/user/project identifier
    content: str                     # raw text content
    summary: str                     # auto-generated summary
    tags: list[str]                  # auto-extracted tags
    embedding: list[float]           # semantic embedding vector
    source_agent: str                # which agent wrote this
    source_platform: str             # chatgpt / claude / perplexity / custom
    created_at: int                  # unix timestamp ms (UTC)
    updated_at: int
    importance: float                # 0.0–1.0, used for pruning
    access_count: int                # how many times retrieved
    # web3 fields (phase 2+)
    owner_address: Optional[str] = None   # wallet address
    ipfs_cid: Optional[str] = None        # IPFS content ID
    signature: Optional[str] = None       # owner's signature of content hash


@dataclass
class Namespace:
    """A memory namespace — owned by an agent/user/project."""
    id: str
    name: str
    description: str
    api_key_hash: str                # sha256 of API key (free tier: None)
    is_public: bool                  # can other agents read this?
    created_at: int
    memory_count: int
    owner_address: Optional[str] = None  # wallet address (phase 2+)

*** Add File: /home/ubuntu/memex/api/database.py
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


# ── Namespace operations ──────────────────────────────────────────────────────

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


# ── Memory operations ─────────────────────────────────────────────────────────

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


def get_memories(namespace: str, limit: int = 20, offset: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM memories WHERE namespace=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (namespace, limit, offset)
    ).fetchall()
    conn.execute(
        "UPDATE memories SET access_count = access_count + 1 WHERE namespace=? ORDER BY created_at DESC LIMIT ?",
        (namespace, limit)
    )
    conn.commit()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def search_memories(namespace: str, query_embedding: list[float],
                    limit: int = 10, threshold: float = 0.3) -> list[dict]:
    """
    Cosine similarity search over stored embeddings.
    Phase 2: replace with pgvector or Chroma for scale.
    """
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


def keyword_search(namespace: str, query: str, limit: int = 10) -> list[dict]:
    """Fallback full-text search when no embedding is available."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM memories WHERE namespace=? AND (content LIKE ? OR summary LIKE ? OR tags LIKE ?) ORDER BY importance DESC LIMIT ?",
        (namespace, f"%{query}%", f"%{query}%", f"%{query}%", limit)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["embedding"] = json.loads(d.get("embedding") or "[]")
    return d


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)

*** Add File: /home/ubuntu/memex/api/embeddings.py
"""
Memex — Embedding generation

Uses the LiteLLM proxy on the VPS (which routes to xAI/OpenAI).
Falls back to a simple TF-IDF-style hash embedding if the API is unavailable.
Phase 2: replace with a local embedding model (e.g. nomic-embed-text via Ollama).
"""

import os
import re
import math
import hashlib
from openai import OpenAI

LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
EMBED_MODEL  = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM    = 384  # fallback dimension

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=LITELLM_BASE + "/v1",
            api_key=os.environ.get("LITELLM_API_KEY", "sk-1234"),
        )
    return _client


def embed(text: str) -> list[float]:
    """Generate a semantic embedding for text. Falls back gracefully."""
    try:
        resp = _get_client().embeddings.create(
            model=EMBED_MODEL,
            input=text[:8000],  # truncate to avoid token limits
        )
        return resp.data[0].embedding
    except Exception:
        return _fallback_embed(text)


def _fallback_embed(text: str) -> list[float]:
    """
    Deterministic hash-based embedding for when the API is unavailable.
    Not semantically meaningful but ensures the system keeps working.
    """
    words = re.findall(r'\w+', text.lower())
    vec = [0.0] * EMBED_DIM
    for word in words:
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        idx = h % EMBED_DIM
        vec[idx] += 1.0
    # L2 normalize
    mag = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / mag for x in vec]


def extract_tags(text: str) -> list[str]:
    """Simple keyword extraction — no external API needed."""
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
    stopwords = {
        'this', 'that', 'with', 'from', 'have', 'been', 'will', 'would',
        'could', 'should', 'they', 'them', 'their', 'what', 'when', 'where',
        'which', 'while', 'about', 'into', 'through', 'during', 'before',
        'after', 'above', 'below', 'between', 'each', 'more', 'most',
        'other', 'some', 'such', 'than', 'then', 'there', 'these', 'those',
    }
    freq: dict[str, int] = {}
    for w in words:
        if w not in stopwords:
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:8]]


def summarize(text: str) -> str:
    """Generate a short summary. Falls back to first 200 chars."""
    if len(text) <= 200:
        return text
    try:
        resp = _get_client().chat.completions.create(
            model=os.environ.get("INNER_MODEL", "claude-3-haiku-20240307"),
            messages=[
                {"role": "system", "content": "Summarize the following in one sentence (max 100 words):"},
                {"role": "user", "content": text[:4000]},
            ],
            max_tokens=150,
        )
        return resp.choices[0].message.content or text[:200]
    except Exception:
        return text[:200] + ("..." if len(text) > 200 else "")

*** Add File: /home/ubuntu/memex/api/main.py
"""
Memex — Core FastAPI application

Endpoints:
  POST   /v1/memory              Store a memory
  GET    /v1/memory/{namespace}  List recent memories
  POST   /v1/memory/search       Semantic + keyword search
  DELETE /v1/memory/{id}         Delete a memory
  POST   /v1/namespace           Create/get a namespace
  GET    /v1/namespace/{name}    Get namespace info
  GET    /health                 Health check
  GET    /                       API info + docs link
"""

import os
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

from .database import (
    init_db, create_namespace, get_namespace,
    store_memory, get_memories, search_memories,
    delete_memory, keyword_search
)
from .embeddings import embed, extract_tags, summarize


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Memex — Open Agent Memory Layer",
    description=(
        "Free, open persistent memory for any AI agent. "
        "Works with Claude, ChatGPT, Perplexity, and any custom agent via MCP or REST. "
        "Designed to evolve toward decentralized, wallet-owned memory."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response schemas ────────────────────────────────────────────────

class StoreMemoryRequest(BaseModel):
    namespace: str = Field(..., description="Your agent/user/project identifier")
    content: str   = Field(..., description="The memory content to store")
    source_agent: str = Field("unknown", description="Name of the agent storing this")
    source_platform: str = Field("unknown", description="Platform: chatgpt/claude/perplexity/custom")
    importance: float = Field(0.5, ge=0.0, le=1.0, description="Memory importance 0-1")
    tags: Optional[list[str]] = Field(None, description="Optional tags (auto-extracted if omitted)")


class SearchRequest(BaseModel):
    namespace: str = Field(..., description="Namespace to search in")
    query: str     = Field(..., description="Search query")
    limit: int     = Field(10, ge=1, le=50)
    threshold: float = Field(0.3, ge=0.0, le=1.0, description="Minimum similarity score")


class CreateNamespaceRequest(BaseModel):
    name: str = Field(..., description="Unique namespace name (e.g. 'my-agent' or 'project-x')")
    description: str = Field("", description="What this namespace is for")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "name": "Memex",
        "tagline": "Open persistent memory for any AI agent",
        "version": "0.1.0",
        "docs": "/docs",
        "github": "https://github.com/Gudmundur76/agent-memex",
        "mcp_server": "memex.gummi.lt/mcp",
        "status": "operational",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": int(time.time() * 1000)}


@app.post("/v1/namespace")
async def create_or_get_namespace(req: CreateNamespaceRequest):
    existing = get_namespace(req.name)
    if existing:
        return {"namespace": existing, "created": False}
    ns = create_namespace(req.name, req.description)
    return {"namespace": ns, "created": True}


@app.get("/v1/namespace/{name}")
async def get_namespace_info(name: str):
    ns = get_namespace(name)
    if not ns:
        raise HTTPException(status_code=404, detail=f"Namespace '{name}' not found")
    return ns


@app.post("/v1/memory")
async def store(req: StoreMemoryRequest):
    # Auto-create namespace if it doesn't exist
    if not get_namespace(req.namespace):
        create_namespace(req.namespace)

    # Generate embedding and extract tags
    emb  = embed(req.content)
    tags = req.tags if req.tags is not None else extract_tags(req.content)
    summ = summarize(req.content)

    mem = store_memory(
        namespace=req.namespace,
        content=req.content,
        summary=summ,
        tags=tags,
        embedding=emb,
        source_agent=req.source_agent,
        source_platform=req.source_platform,
        importance=req.importance,
    )
    # Don't return the full embedding in the response
    mem.pop("embedding", None)
    return {"memory": mem, "message": "Memory stored successfully"}


@app.get("/v1/memory/{namespace}")
async def list_memories(namespace: str, limit: int = 20, offset: int = 0):
    ns = get_namespace(namespace)
    if not ns:
        raise HTTPException(status_code=404, detail=f"Namespace '{namespace}' not found")
    mems = get_memories(namespace, limit=limit, offset=offset)
    for m in mems:
        m.pop("embedding", None)
    return {"namespace": namespace, "memories": mems, "count": len(mems)}


@app.post("/v1/memory/search")
async def search(req: SearchRequest):
    ns = get_namespace(req.namespace)
    if not ns:
        # Auto-create and return empty
        create_namespace(req.namespace)
        return {"namespace": req.namespace, "results": [], "count": 0}

    # Try semantic search first, fall back to keyword
    query_emb = embed(req.query)
    results = search_memories(req.namespace, query_emb, limit=req.limit, threshold=req.threshold)

    if not results:
        results = keyword_search(req.namespace, req.query, limit=req.limit)
        for r in results:
            r["score"] = 0.0

    for r in results:
        r.pop("embedding", None)

    return {"namespace": req.namespace, "results": results, "count": len(results)}


@app.delete("/v1/memory/{namespace}/{memory_id}")
async def delete(namespace: str, memory_id: str):
    ok = delete_memory(memory_id, namespace)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True, "id": memory_id}


# ── OpenAI-compatible memory endpoint (for ChatGPT Actions) ──────────────────
@app.post("/v1/chat/memory")
async def chat_memory_store(request: Request):
    """
    Simplified endpoint matching the shape ChatGPT Actions expect.
    POST body: {"namespace": "...", "content": "...", "role": "user|assistant"}
    """
    body = await request.json()
    namespace = body.get("namespace", "default")
    content   = body.get("content", "")
    role      = body.get("role", "unknown")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    if not get_namespace(namespace):
        create_namespace(namespace)
    emb  = embed(content)
    tags = extract_tags(content)
    summ = summarize(content)
    mem = store_memory(namespace, content, summ, tags, emb,
                       source_agent=role, source_platform="chatgpt")
    mem.pop("embedding", None)
    return {"id": mem["id"], "stored": True}

*** Add File: /home/ubuntu/memex/api/__init__.py

*** Add File: /home/ubuntu/memex/requirements.txt
fastapi>=0.111.0
uvicorn[standard]>=0.30.0
pydantic>=2.7.0
openai>=1.35.0
mcp>=1.0.0
httpx>=0.27.0

*** Add File: /home/ubuntu/memex/Dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV MEMEX_DB_PATH=/app/data/memex.db
ENV LITELLM_BASE_URL=http://host.docker.internal:4000
ENV LITELLM_API_KEY=sk-1234
ENV INNER_MODEL=claude-3-haiku-20240307
ENV EMBED_MODEL=text-embedding-3-small
ENV PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --workers 2"]

