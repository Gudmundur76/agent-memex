import os
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Memex — Open Agent Memory Layer",
    description="Free, open persistent memory for any AI agent. Works with Claude, ChatGPT, Perplexity, and any custom agent via MCP or REST.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve landing page
import pathlib
LANDING = pathlib.Path(__file__).parent.parent / "landing"
if LANDING.exists():
    app.mount("/static", StaticFiles(directory=str(LANDING)), name="static")


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

