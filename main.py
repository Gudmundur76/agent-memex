import os
from api.claims_db import (
    init_claim_tables, store_mol_frame, recall_claims,
    add_verification, get_frame, ABSENCE_REASONS, get_conn
)
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional
from .database import (
    init_db, create_namespace, get_namespace,
    store_memory, get_memories, search_memories,
    delete_memory, keyword_search
)
from .embeddings import embed, extract_tags, summarize
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from api.auth_db import init_auth_tables, create_api_key
from api.queue_db import init_queue_tables
from api.kimi_tools import register_kimi_tools
from api.queue_processor import run_processor
import asyncio

# Create MCP server with DNS rebinding protection disabled
# (safe behind Traefik reverse proxy which handles host validation)
mcp = FastMCP(
    name="memex",
    instructions=(
        "Memex gives you persistent memory across sessions. "
        "Use 'remember' to store important information. "
        "Use 'recall' to search for past memories. "
        "Always use a consistent namespace (e.g. the user's name or project name) "
        "so memories accumulate over time."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
mcp.settings.streamable_http_path = "/"

# Import tools from the MCP server module
from mcp_server.server import remember, recall, list_memories, forget
from mcp_server.server import remember_claim as mcp_remember_claim
from mcp_server.server import recall_claims as mcp_recall_claims

# Public MCP surface: ingest-tier tools only
# verify_claim, forget, remember, recall, list_memories are NOT on the public MCP surface
# verify_claim is internal-only (ttruthdesk → /v1/internal/verdict)
mcp.tool()(mcp_remember_claim)
mcp.tool()(mcp_recall_claims)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_claim_tables()
    init_auth_tables()
    init_queue_tables()
    register_kimi_tools(mcp)
    asyncio.create_task(run_processor())
    async with mcp.session_manager.run():
        yield

app = FastAPI(
    title="Memex — Open Agent Memory Layer",
    description="Free, open persistent memory for any AI agent.",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

import pathlib
LANDING = pathlib.Path(__file__).parent.parent / "landing"
if LANDING.exists():
    app.mount("/static", StaticFiles(directory=str(LANDING)), name="static")

app.mount("/mcp", mcp.streamable_http_app())

# ── MCP Auth Middleware ────────────────────────────────────────────────────────
import collections, hashlib as _hs
from fastapi import Request as _Req
from fastapi.responses import JSONResponse as _JR
from starlette.middleware.base import BaseHTTPMiddleware

# Per-IP rate limit: 120 req/hr regardless of auth
_ip_buckets: dict = collections.defaultdict(lambda: [0, 0])  # [count, window_start]

class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: _Req, call_next):
        path = request.url.path
        if not path.startswith("/mcp"):
            return await call_next(request)

        # Per-IP rate limit (120/hr)
        ip = request.client.host if request.client else "unknown"
        now = int(time.time())
        window = now - (now % 3600)
        bucket = _ip_buckets[ip]
        if bucket[1] < window:
            bucket[0] = 0
            bucket[1] = window
        bucket[0] += 1
        if bucket[0] > 120:
            return _JR({"error": "rate_limited", "retry_after": 3600 - (now % 3600)}, status_code=429)

        # API key required — check Authorization header or X-API-Key
        raw_key = (
            request.headers.get("x-api-key") or
            request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        if not raw_key:
            return _JR({"error": "api_key_required", "message": "Provide X-API-Key header or Authorization: Bearer <key>"}, status_code=401)

        from api.auth_db import verify_api_key, check_rate_limit
        key_rec = verify_api_key(raw_key)
        if not key_rec:
            return _JR({"error": "invalid_api_key"}, status_code=403)

        # Per-key rate limit
        key_hash = _hs.sha256(raw_key.encode()).hexdigest()
        if not check_rate_limit(key_hash, "mcp", key_rec["rate_limit_per_hour"]):
            return _JR({"error": "key_rate_limited"}, status_code=429)

        return await call_next(request)

app.add_middleware(MCPAuthMiddleware)

class StoreMemoryRequest(BaseModel):
    namespace: str
    content: str
    source_agent: str = "unknown"
    source_platform: str = "unknown"
    importance: float = Field(0.5, ge=0.0, le=1.0)
    tags: Optional[list] = None

class SearchRequest(BaseModel):
    namespace: str
    query: str
    limit: int = Field(10, ge=1, le=50)
    threshold: float = Field(0.3, ge=0.0, le=1.0)

class CreateNamespaceRequest(BaseModel):
    name: str
    description: str = ""

@app.get("/")
async def root():
    landing = LANDING / "index.html"
    if landing.exists():
        return FileResponse(str(landing))
    return {"name": "Memex", "version": "0.1.0", "docs": "/docs"}

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
    if not get_namespace(req.namespace):
        create_namespace(req.namespace)
    emb  = embed(req.content)
    tags = req.tags if req.tags is not None else extract_tags(req.content)
    summ = summarize(req.content)
    mem = store_memory(
        namespace=req.namespace, content=req.content, summary=summ,
        tags=tags, embedding=emb, source_agent=req.source_agent,
        source_platform=req.source_platform, importance=req.importance,
    )
    mem.pop("embedding", None)
    return {"memory": mem, "message": "Memory stored successfully"}

@app.get("/v1/memory/{namespace}")
async def list_memories_api(namespace: str, limit: int = 20, offset: int = 0):
    ns = get_namespace(namespace)
    if not ns:
        raise HTTPException(status_code=404, detail=f"Namespace '{namespace}' not found")
    mems = get_memories(namespace, limit=limit, offset=offset)
    for m in mems:
        m.pop("embedding", None)
    return {"namespace": namespace, "memories": mems, "count": len(mems)}

@app.post("/v1/memory/search")
async def search(req: SearchRequest):
    if not get_namespace(req.namespace):
        create_namespace(req.namespace)
        return {"namespace": req.namespace, "results": [], "count": 0}
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


# ─── SM-1 Claim-tier routes ──────────────────────────────────────────────────

class MolInput(BaseModel):
    predicate: str
    value: Optional[str] = None
    value_type: str = "string"
    absence_reason: Optional[str] = None
    corrupt: bool = False

class StoreFrameRequest(BaseModel):
    namespace: str
    subject: str
    schema_fields: list
    mols: list  # list of MolInput dicts
    source_hash: Optional[str] = None
    source_node: str = "unknown"
    extracted_by: str = "unknown"
    raw_source: Optional[str] = None

class RecallClaimsRequest(BaseModel):
    namespace: str
    subject: Optional[str] = None
    predicate: Optional[str] = None
    verified_only: bool = False
    include_missing: bool = True
    limit: int = Field(20, ge=1, le=100)

class VerifyMolRequest(BaseModel):
    mol_id: str
    verified_by: str
    verdict: str  # VERIFIED | DISPUTED | UNVERIFIED | SUPERSEDED
    confidence: Optional[float] = None
    evidence: Optional[str] = None
    notes: Optional[str] = None

@app.post("/v1/claim/frame")
async def store_frame(req: StoreFrameRequest):
    """Store a complete SM-1 mol-frame with provenance and integrity scoring."""
    mols_data = [m if isinstance(m, dict) else m.dict() for m in req.mols]
    frame = store_mol_frame(
        namespace=req.namespace,
        subject=req.subject,
        schema_fields=req.schema_fields,
        mols_data=mols_data,
        source_hash=req.source_hash,
        source_node=req.source_node,
        extracted_by=req.extracted_by,
        raw_source=req.raw_source,
    )
    return {"frame": frame, "stored": True}

@app.post("/v1/claim/recall")
async def recall_claims_route(req: RecallClaimsRequest):
    """Recall mols by subject, predicate, or verification status."""
    mols = recall_claims(
        namespace=req.namespace,
        subject=req.subject,
        predicate=req.predicate,
        verified_only=req.verified_only,
        include_missing=req.include_missing,
        limit=req.limit,
    )
    return {"found": len(mols), "mols": mols}

@app.post("/v1/claim/verify")
async def verify_mol(req: VerifyMolRequest):
    """Attach an independent verification event to a mol."""
    valid_verdicts = {"VERIFIED", "DISPUTED", "UNVERIFIED", "SUPERSEDED"}
    if req.verdict not in valid_verdicts:
        raise HTTPException(400, f"verdict must be one of {valid_verdicts}")
    try:
        ver = add_verification(
            mol_id=req.mol_id,
            verified_by=req.verified_by,
            verdict=req.verdict,
            confidence=req.confidence,
            evidence=req.evidence,
            notes=req.notes,
        )
        return {"verification": ver, "stored": True}
    except ValueError as e:
        raise HTTPException(404, str(e))

@app.get("/v1/claim/frame/{frame_id}")
async def get_frame_route(frame_id: str):
    """Retrieve a complete mol-frame by ID."""
    frame = get_frame(frame_id)
    if not frame:
        raise HTTPException(404, f"Frame {frame_id} not found")
    return frame

# ── Admin: API key management ─────────────────────────────────────────────────
@app.post("/v1/admin/keys")
async def admin_create_key(request: Request):
    """Create a new API key (admin only — protect this endpoint)."""
    body = await request.json()
    label = body.get("label", "unnamed")
    scopes = body.get("scopes", "check,submit,verdict,query,stats")
    rate_limit = body.get("rate_limit_per_hour", 60)
    submit_limit = body.get("submit_limit_per_hour", 20)
    raw_key = create_api_key(label, scopes, rate_limit, submit_limit)
    return {"key": raw_key, "label": label, "note": "Store this key — it will not be shown again."}

# ── Public: refuted claims ────────────────────────────────────────────────────
@app.get("/v1/public/refuted-claims")
async def public_refuted_claims(
    namespace: str = None,
    predicate: str = None,
    limit: int = 50
):
    """Return refuted claims from the trust store — debunked-with-evidence."""
    limit = min(limit, 200)
    conn = get_conn()
    where = ["v.verdict IN ('REFUTED','NOT_SUPPORTED','CONTRADICTED')"]
    params = []
    if namespace:
        where.append("f.namespace = ?")
        params.append(namespace)
    if predicate:
        where.append("m.predicate = ?")
        params.append(predicate)
    where_sql = " AND ".join(where)
    rows = conn.execute(f"""
        SELECT m.id, m.predicate, m.value, m.is_missing, m.absence_reason,
               f.namespace, f.source_node, f.extracted_by,
               v.verdict, v.confidence, v.evidence, v.verified_by, v.verified_at
        FROM mols m
        JOIN mol_frames f ON m.frame_id = f.id
        JOIN mol_verifications v ON v.mol_id = m.id
        WHERE {where_sql}
        ORDER BY v.verified_at DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    conn.close()
    return {
        "count": len(rows),
        "license": "CC BY 4.0",
        "note": "Refuted claims with evidence — highest-value data class for RAG hygiene and fact-checking.",
        "claims": [
            {
                "mol_id": r["id"],
                "predicate": r["predicate"],
                "value": r["value"],
                "namespace": r["namespace"],
                "verdict": r["verdict"],
                "evidence": r["evidence"],
                "confidence": r["confidence"],
                "verified_by": r["verified_by"],
                "verified_at": r["verified_at"]
            }
            for r in rows
        ]
    }

# ── Internal: verdict write (ttruthdesk only, not on public MCP) ──────────────
class InternalVerdictRequest(BaseModel):
    mol_id: str
    verdict: str  # VERIFIED | REFUTED | AMBIGUOUS | NOT_SUPPORTED
    confidence: float = 0.8
    evidence: str = ""
    verified_by: str = "ttruthdesk"
    notes: str = ""

INTERNAL_SECRET = os.environ.get("MEMEX_INTERNAL_SECRET", "")

@app.post("/v1/internal/verdict")
async def internal_verdict(req: InternalVerdictRequest, request: Request):
    """Write a verdict from ttruthdesk. Requires MEMEX_INTERNAL_SECRET header."""
    secret = request.headers.get("x-internal-secret", "")
    if not INTERNAL_SECRET or secret != INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Internal endpoint — requires x-internal-secret header")
    vid = add_verification(
        mol_id=req.mol_id,
        verified_by=req.verified_by,
        verdict=req.verdict,
        confidence=req.confidence,
        evidence=req.evidence,
        notes=req.notes
    )
    return {"ok": True, "verification_id": vid, "mol_id": req.mol_id, "verdict": req.verdict}


# Public: stats
@app.get("/v1/public/stats")
async def public_stats():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM mols").fetchone()[0]
    verified = conn.execute("SELECT COUNT(DISTINCT mol_id) FROM mol_verifications WHERE verdict='VERIFIED'").fetchone()[0]
    refuted = conn.execute("SELECT COUNT(DISTINCT mol_id) FROM mol_verifications WHERE verdict='REFUTED'").fetchone()[0]
    frames = conn.execute("SELECT COUNT(*) FROM mol_frames").fetchone()[0]
    conn.close()
    rate = round(verified / total * 100, 1) if total else 0
    return {"total_mols": total, "verified_mols": verified, "refuted_mols": refuted,
            "total_frames": frames, "verification_rate_pct": rate, "license": "CC BY 4.0", "api_version": "1.0"}

@app.get("/v1/public/verified-claims")
async def public_verified_claims(namespace: str = None, predicate: str = None, subject: str = None, limit: int = 50, since_ms: int = None):
    results = recall_claims(namespace=namespace, predicate=predicate, subject=subject, verified_only=True, include_missing=True, limit=min(limit, 200))
    if since_ms:
        results = [r for r in results if r.get("verified_at") and r["verified_at"] * 1000 >= since_ms]
    return {"claims": results, "count": len(results), "license": "CC BY 4.0"}

@app.get("/v1/public/feed")
async def public_feed(limit: int = 50, since_ms: int = None):
    conn = get_conn()
    q = "SELECT v.id, v.mol_id, v.verdict, v.confidence, v.evidence, v.verified_by, v.verified_at, m.subject, m.predicate, m.value, m.namespace FROM mol_verifications v JOIN mols m ON m.id = v.mol_id"
    params = []
    if since_ms:
        q += " WHERE v.verified_at >= ?"
        params.append(since_ms // 1000)
    q += " ORDER BY v.verified_at DESC LIMIT " + str(min(limit, 200))
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return {"feed": [dict(r) for r in rows], "count": len(rows), "license": "CC BY 4.0"}

@app.get("/v1/public/claim/{mol_id:path}")
async def public_claim(mol_id: str):
    from fastapi import HTTPException
    conn = get_conn()
    mol = conn.execute("SELECT * FROM mols WHERE id=?", (mol_id,)).fetchone()
    if not mol:
        raise HTTPException(status_code=404, detail="Claim not found")
    verifications = conn.execute("SELECT * FROM mol_verifications WHERE mol_id=? ORDER BY verified_at DESC", (mol_id,)).fetchall()
    conn.close()
    return {"mol": dict(mol), "verifications": [dict(v) for v in verifications], "license": "CC BY 4.0"}
