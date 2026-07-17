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



# ─── SM-1 Claim-tier MCP tools ───────────────────────────────────────────────

@mcp.tool()
async def remember_claim(
    namespace: str,
    subject: str,
    schema_fields: list,
    mols: list,
    source_node: str = "mcp-client",
    extracted_by: str = "mcp-client",
    raw_source: str = None,
) -> dict:
    """
    Store a schema-bound SM-1 mol-frame (claim-tier memory).

    Use this instead of 'remember' when the content is a structured factual claim
    that requires provenance, exact source copying, and explicit absence semantics.

    Args:
        namespace: Identifier for this agent/user/project
        subject: The entity this frame describes (e.g. "NORDLYS", "patient-001")
        schema_fields: List of field names in the schema (e.g. ["name", "dose", "date"])
        mols: List of claim objects. Each must have:
              - predicate (str): field name from schema_fields
              - value (str|None): exact source substring, or null/None for MISSING
              - value_type (str, optional): "string"|"identifier"|"quantity"|"date"|"route"
              - absence_reason (str, optional): "ABSENT"|"AMBIGUOUS"|"NON_CANONICAL"|"UNPARSABLE"|"CONFLICTING"
        source_node: Which model/agent produced this frame
        extracted_by: Which connector/synapse performed extraction
        raw_source: Optional original source text for audit trail
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MEMEX_API}/v1/claim/frame",
            json={
                "namespace": namespace,
                "subject": subject,
                "schema_fields": schema_fields,
                "mols": mols,
                "source_node": source_node,
                "extracted_by": extracted_by,
                "raw_source": raw_source,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        frame = data["frame"]
        return {
            "stored": True,
            "frame_id": frame["id"],
            "subject": subject,
            "integrity_score": frame.get("integrity_score"),
            "fields_exact": frame.get("fields_exact"),
            "fields_missing_correct": frame.get("fields_missing_correct"),
            "fields_corrupt": frame.get("fields_corrupt"),
            "frame_safety_pass": bool(frame.get("frame_safety_pass")),
            "mol_count": len(frame.get("mols", [])),
        }


@mcp.tool()
async def recall_claims(
    namespace: str,
    subject: str = None,
    predicate: str = None,
    verified_only: bool = False,
    include_missing: bool = True,
    limit: int = 20,
) -> dict:
    """
    Recall structured claim mols from the claim-tier.

    Use this instead of 'recall' when you need structured facts with provenance,
    not prose memories. Results are schema-bound claim units, not free text.

    Args:
        namespace: Which namespace to search
        subject: Filter by entity (e.g. "NORDLYS")
        predicate: Filter by field name (e.g. "dose")
        verified_only: If True, only return mols with a VERIFIED verification event
        include_missing: If False, exclude MISSING mols (only return present values)
        limit: Max results (1-100)
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MEMEX_API}/v1/claim/recall",
            json={
                "namespace": namespace,
                "subject": subject,
                "predicate": predicate,
                "verified_only": verified_only,
                "include_missing": include_missing,
                "limit": limit,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        mols = data.get("mols", [])
        return {
            "found": len(mols),
            "namespace": namespace,
            "mols": [
                {
                    "id": m["id"],
                    "subject": m["subject"],
                    "predicate": m["predicate"],
                    "value": m["value"] if not m.get("is_missing") else "MISSING",
                    "absence_reason": m.get("absence_reason"),
                    "value_type": m.get("value_type"),
                    "source_node": m.get("source_node"),
                    "extracted_by": m.get("extracted_by"),
                    "emitted_at": m.get("emitted_at"),
                }
                for m in mols
            ],
        }


@mcp.tool()
async def verify_claim(
    mol_id: str,
    verdict: str,
    verified_by: str = "mcp-client",
    confidence: float = None,
    evidence: str = None,
    notes: str = None,
) -> dict:
    """
    Attach an independent verification event to a stored mol.

    This is the mechanism that separates extraction output from verified knowledge.
    A mol is not trusted merely because it is structured — it must be independently
    verified. This tool records that verification event.

    Args:
        mol_id: The mol ID to verify (from recall_claims results)
        verdict: "VERIFIED" | "DISPUTED" | "UNVERIFIED" | "SUPERSEDED"
        verified_by: Identity of the verifier (agent name, human reviewer, citation.is)
        confidence: Optional 0.0-1.0 confidence score
        evidence: Optional source or evidence reference
        notes: Optional free-text notes
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MEMEX_API}/v1/claim/verify",
            json={
                "mol_id": mol_id,
                "verified_by": verified_by,
                "verdict": verdict,
                "confidence": confidence,
                "evidence": evidence,
                "notes": notes,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        ver = data["verification"]
        return {
            "stored": True,
            "verification_id": ver["id"],
            "mol_id": mol_id,
            "verdict": verdict,
            "verified_by": verified_by,
            "verified_at": ver.get("verified_at"),
        }
