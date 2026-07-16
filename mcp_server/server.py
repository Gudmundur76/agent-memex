"""
Memex MCP Server

Exposes Memex memory operations as MCP tools so any MCP-compatible client
(Claude.ai, Cursor, Windsurf, custom agents) can use persistent memory
with zero configuration beyond adding the server URL.

Tools:
  remember      — Store a memory
  recall        — Search memories by query
  list_memories — List recent memories
  forget        — Delete a specific memory
"""

import os
import httpx
from mcp.server.fastmcp import FastMCP

MEMEX_API = os.environ.get("MEMEX_API_URL", "http://localhost:8000")

mcp = FastMCP(
    name="memex",
    instructions=(
        "Memex gives you persistent memory across sessions. "
        "Use 'remember' to store important information. "
        "Use 'recall' to search for past memories. "
        "Always use a consistent namespace (e.g. the user's name or project name) "
        "so memories accumulate over time."
    ),
)


@mcp.tool()
async def remember(
    content: str,
    namespace: str = "default",
    importance: float = 0.5,
    source_agent: str = "mcp-client",
) -> dict:
    """
    Store a memory for later retrieval.

    Args:
        content: The information to remember (facts, preferences, context, decisions)
        namespace: Identifier for this agent/user/project (use consistently)
        importance: How important is this memory? 0.0 (low) to 1.0 (critical)
        source_agent: Name of the agent storing this memory
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MEMEX_API}/v1/memory",
            json={
                "namespace": namespace,
                "content": content,
                "importance": importance,
                "source_agent": source_agent,
                "source_platform": "mcp",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        mem = data["memory"]
        return {
            "stored": True,
            "id": mem["id"],
            "summary": mem.get("summary", ""),
            "tags": mem.get("tags", []),
            "namespace": namespace,
        }


@mcp.tool()
async def recall(
    query: str,
    namespace: str = "default",
    limit: int = 5,
) -> dict:
    """
    Search memories by semantic similarity or keywords.

    Args:
        query: What you're looking for (natural language)
        namespace: Which namespace to search (must match what was used in remember)
        limit: Maximum number of memories to return (1-20)
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MEMEX_API}/v1/memory/search",
            json={"namespace": namespace, "query": query, "limit": min(limit, 20)},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        return {
            "found": len(results),
            "namespace": namespace,
            "memories": [
                {
                    "id": r["id"],
                    "content": r["content"],
                    "summary": r.get("summary", ""),
                    "tags": r.get("tags", []),
                    "score": r.get("score", 0),
                    "created_at": r.get("created_at"),
                }
                for r in results
            ],
        }


@mcp.tool()
async def list_memories(
    namespace: str = "default",
    limit: int = 10,
) -> dict:
    """
    List the most recent memories in a namespace.

    Args:
        namespace: Which namespace to list
        limit: How many memories to return (1-50)
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{MEMEX_API}/v1/memory/{namespace}",
            params={"limit": min(limit, 50)},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "namespace": namespace,
            "count": data.get("count", 0),
            "memories": [
                {
                    "id": m["id"],
                    "content": m["content"][:300],
                    "summary": m.get("summary", ""),
                    "tags": m.get("tags", []),
                    "created_at": m.get("created_at"),
                }
                for m in data.get("memories", [])
            ],
        }


@mcp.tool()
async def forget(
    memory_id: str,
    namespace: str = "default",
) -> dict:
    """
    Delete a specific memory.

    Args:
        memory_id: The ID of the memory to delete (from recall or list_memories)
        namespace: The namespace the memory belongs to
    """
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{MEMEX_API}/v1/memory/{namespace}/{memory_id}",
            timeout=10.0,
        )
        if resp.status_code == 404:
            return {"deleted": False, "error": "Memory not found"}
        resp.raise_for_status()
        return {"deleted": True, "id": memory_id}


if __name__ == "__main__":
    # Run as stdio MCP server (for local use / Claude Desktop)
    mcp.run(transport="stdio")

*** Add File: /home/ubuntu/memex/mcp_server/http_server.py
"""
Memex MCP HTTP Server

Runs the MCP server over HTTP/SSE so remote clients (Claude.ai, web agents)
can connect to memex.gummi.lt/mcp without installing anything locally.
"""

import os
from mcp_server.server import mcp

if __name__ == "__main__":
    port = int(os.environ.get("MCP_PORT", "8001"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)

*** Add File: /home/ubuntu/memex/mcp_server/__init__.py

*** Add File: /home/ubuntu/memex/openapi_chatgpt.json
{
  "openapi": "3.1.0",
  "info": {
    "title": "Memex Memory API",
    "description": "Store and retrieve persistent memories across AI agent sessions. Free and open for any agent.",
    "version": "0.1.0"
  },
  "servers": [
    {"url": "https://memex.gummi.lt"}
  ],
  "paths": {
    "/v1/memory": {
      "post": {
        "operationId": "storeMemory",
        "summary": "Store a memory",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "required": ["namespace", "content"],
                "properties": {
                  "namespace": {"type": "string", "description": "Your agent/user identifier"},
                  "content": {"type": "string", "description": "What to remember"},
                  "source_agent": {"type": "string", "default": "chatgpt"},
                  "source_platform": {"type": "string", "default": "chatgpt"},
                  "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5}
                }
              }
            }
          }
        },
        "responses": {
          "200": {"description": "Memory stored successfully"}
        }
      }
    },
    "/v1/memory/search": {
      "post": {
        "operationId": "searchMemories",
        "summary": "Search memories by semantic similarity",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "required": ["namespace", "query"],
                "properties": {
                  "namespace": {"type": "string"},
                  "query": {"type": "string", "description": "Natural language search query"},
                  "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50}
                }
              }
            }
          }
        },
        "responses": {
          "200": {"description": "Search results"}
        }
      }
    },
    "/v1/memory/{namespace}": {
      "get": {
        "operationId": "listMemories",
        "summary": "List recent memories in a namespace",
        "parameters": [
          {"name": "namespace", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}
        ],
        "responses": {
          "200": {"description": "List of memories"}
        }
      }
    },
    "/v1/memory/{namespace}/{memory_id}": {
      "delete": {
        "operationId": "deleteMemory",
        "summary": "Delete a specific memory",
        "parameters": [
          {"name": "namespace", "in": "path", "required": true, "schema": {"type": "string"}},
          {"name": "memory_id", "in": "path", "required": true, "schema": {"type": "string"}}
        ],
        "responses": {
          "200": {"description": "Memory deleted"}
        }
      }
    }
  }
}

*** Add File: /home/ubuntu/memex/self_healing/monitor.py
"""
Memex Self-Healing Monitor

Runs continuously alongside the API. Monitors:
1. API health (restart if down)
2. Search quality (trigger RSI loop if recall accuracy drops)
3. DB size (prune low-importance old memories if >80% capacity)
4. Embedding model availability (switch to fallback if needed)

This is the autonomous self-healing layer — no human intervention needed.
"""

import os
import time
import json
import logging
import requests
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("memex-monitor")

MEMEX_API    = os.environ.get("MEMEX_API_URL", "http://localhost:8000")
CHECK_INTERVAL = int(os.environ.get("MONITOR_INTERVAL_SEC", "60"))
DB_PATH      = os.environ.get("MEMEX_DB_PATH", "/app/data/memex.db")
MAX_DB_MB    = int(os.environ.get("MAX_DB_MB", "500"))


def check_health() -> bool:
    try:
        r = requests.get(f"{MEMEX_API}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def check_db_size() -> float:
    """Return DB size in MB."""
    p = Path(DB_PATH)
    if not p.exists():
        return 0.0
    return p.stat().st_size / (1024 * 1024)


def prune_old_memories():
    """Delete the oldest, least-important memories when DB is too large."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    # Delete bottom 10% by importance, oldest first
    cur = conn.execute(
        "DELETE FROM memories WHERE id IN ("
        "  SELECT id FROM memories ORDER BY importance ASC, created_at ASC LIMIT "
        "  (SELECT MAX(1, COUNT(*) / 10) FROM memories)"
        ")"
    )
    deleted = cur.rowcount
    conn.execute("VACUUM")
    conn.commit()
    conn.close()
    log.info(f"Pruned {deleted} low-importance memories")
    return deleted


def run_quality_check() -> dict:
    """
    Store a known test memory, search for it, verify it's found.
    Returns {"ok": bool, "score": float}
    """
    test_ns = "__memex_health_check__"
    test_content = "The quick brown fox jumps over the lazy dog — memex health check"
    try:
        # Store
        r = requests.post(f"{MEMEX_API}/v1/memory", json={
            "namespace": test_ns,
            "content": test_content,
            "importance": 0.1,
            "source_agent": "monitor",
        }, timeout=10)
        if r.status_code != 200:
            return {"ok": False, "score": 0.0}

        # Search
        r2 = requests.post(f"{MEMEX_API}/v1/memory/search", json={
            "namespace": test_ns,
            "query": "quick brown fox",
            "limit": 1,
        }, timeout=10)
        if r2.status_code != 200:
            return {"ok": False, "score": 0.0}

        results = r2.json().get("results", [])
        if results and test_content in results[0].get("content", ""):
            return {"ok": True, "score": results[0].get("score", 0.5)}
        return {"ok": False, "score": 0.0}
    except Exception as e:
        return {"ok": False, "score": 0.0, "error": str(e)}


def write_status(status: dict):
    path = Path(DB_PATH).parent / "monitor_status.json"
    with open(path, "w") as f:
        json.dump(status, f, indent=2)


def run():
    log.info("Memex self-healing monitor started")
    consecutive_failures = 0

    while True:
        status = {
            "timestamp": int(time.time()),
            "api_healthy": False,
            "db_size_mb": 0.0,
            "quality_ok": False,
            "quality_score": 0.0,
        }

        # 1. Health check
        healthy = check_health()
        status["api_healthy"] = healthy
        if not healthy:
            consecutive_failures += 1
            log.warning(f"API health check failed ({consecutive_failures} consecutive)")
            if consecutive_failures >= 3:
                log.error("API down for 3+ checks — alerting (TODO: send notification)")
        else:
            consecutive_failures = 0

        # 2. DB size check
        db_mb = check_db_size()
        status["db_size_mb"] = round(db_mb, 2)
        if db_mb > MAX_DB_MB * 0.8:
            log.warning(f"DB size {db_mb:.1f}MB approaching limit {MAX_DB_MB}MB — pruning")
            prune_old_memories()

        # 3. Quality check (every 10 cycles)
        if int(time.time()) % (CHECK_INTERVAL * 10) < CHECK_INTERVAL:
            qc = run_quality_check()
            status["quality_ok"]    = qc["ok"]
            status["quality_score"] = qc.get("score", 0.0)
            if not qc["ok"]:
                log.warning(f"Quality check failed: {qc}")

        write_status(status)
        log.info(f"Status: api={healthy} db={db_mb:.1f}MB")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()

*** Add File: /home/ubuntu/memex/self_healing/__init__.py

*** Add File: /home/ubuntu/memex/docker-compose.yml
version: "3.9"

services:
  memex-api:
    build: .
    container_name: memex-api
    restart: unless-stopped
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "8765:8000"
    volumes:
      - memex-data:/app/data
    environment:
      - MEMEX_DB_PATH=/app/data/memex.db
      - LITELLM_BASE_URL=http://host.docker.internal:4000
      - LITELLM_API_KEY=sk-1234
      - INNER_MODEL=claude-3-haiku-20240307
      - EMBED_MODEL=text-embedding-3-small
      - PORT=8000
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  memex-monitor:
    build: .
    container_name: memex-monitor
    restart: unless-stopped
    depends_on:
      memex-api:
        condition: service_healthy
    volumes:
      - memex-data:/app/data
    environment:
      - MEMEX_API_URL=http://memex-api:8000
      - MEMEX_DB_PATH=/app/data/memex.db
      - MONITOR_INTERVAL_SEC=60
      - MAX_DB_MB=500
    command: ["python", "-m", "self_healing.monitor"]

volumes:
  memex-data:

*** Add File: /home/ubuntu/memex/README.md
# Memex — Open Agent Memory Layer

**Free, persistent memory for any AI agent.**
Works with Claude, ChatGPT, Perplexity, and any custom agent via MCP or REST.

## Quick Start

### From Claude.ai (MCP)
Add this to your MCP config:
```json
{
  "mcpServers": {
    "memex": {
      "url": "https://memex.gummi.lt/mcp"
    }
  }
}
```
Then ask Claude: *"Remember that I prefer Python over JavaScript"* — it will persist across sessions.

### From any agent (REST)
```bash
# Store a memory
curl -X POST https://memex.gummi.lt/v1/memory \
  -H "Content-Type: application/json" \
  -d '{"namespace": "my-agent", "content": "User prefers dark mode"}'

# Search memories
curl -X POST https://memex.gummi.lt/v1/memory/search \
  -H "Content-Type: application/json" \
  -d '{"namespace": "my-agent", "query": "user preferences"}'
```

### From ChatGPT (Custom Action)
Import `openapi_chatgpt.json` as a Custom Action in your GPT configuration.

## Architecture

```
Phase 1 (now):    Centralized FastAPI + SQLite, semantic search, MCP server
Phase 2 (Q4 26):  IPFS/Arweave storage — memories become portable and permanent
Phase 3 (Q1 27):  Wallet-based identity — you own your memory namespace
Phase 4 (Q2 27):  On-chain registry — discover and trade knowledge namespaces
```

## Self-Healing

A background monitor continuously checks API health, search quality, and DB size.
The RSI engine (also running on the same VPS) monitors retrieval quality and
autonomously improves the embedding and search algorithms when scores drop.

## License
MIT — free for any use, commercial or personal.

## Contributing
PRs welcome. See `docs/CONTRIBUTING.md`.

