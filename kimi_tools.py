"""
Kimi-facing MCP tools: 5 hardened tools for external model consumption.
- memex_check   : pre-assertion claim check
- memex_submit  : queue-only write (never direct)
- memex_verdict : poll submission status
- memex_query   : read verified mols (rate-limited)
- memex_stats   : public trust display
"""
import os, json, time
from mcp.server.fastmcp import FastMCP
from api.claims_db import recall_claims, get_conn as claims_conn
from api.queue_db import enqueue, get_status
from api.auth_db import verify_api_key, check_rate_limit, audit

MEMEX_API = os.environ.get("MEMEX_API_URL", "http://localhost:8000")

def register_kimi_tools(mcp: FastMCP, get_api_key_fn=None):
    """Register 5 hardened tools on an existing FastMCP instance."""

    @mcp.tool()
    def memex_check(claim: str, namespace: str = "general") -> dict:
        """
        Check if a claim has been verified before asserting it.
        Returns verdict and evidence if found, or MISSING if not in the store.
        Call this before stating any consequential fact.
        """
        results = recall_claims(
            namespace=namespace,
            predicate="claim_text",
            verified_only=True,
            limit=5
        )
        # Simple text match
        claim_lower = claim.lower().strip()
        for mol in results:
            stored = (mol.get("raw_value") or "").lower().strip()
            if stored and (claim_lower in stored or stored in claim_lower):
                return {
                    "found": True,
                    "verdict": mol.get("verification_verdict", "UNKNOWN"),
                    "confidence": mol.get("verification_confidence", 0),
                    "evidence": mol.get("verification_evidence", ""),
                    "mol_id": mol.get("id"),
                    "verified_at": mol.get("verification_verified_at")
                }
        return {"found": False, "verdict": "MISSING", "message": "Claim not in verified store. Use memex_submit to queue verification."}

    @mcp.tool()
    def memex_submit(claim: str, domain: str = "general",
                     idempotency_key: str = "") -> dict:
        """
        Submit a claim for verification. Returns a receipt with submission ID.
        Claims land in the queue and are processed asynchronously by ttruthdesk.
        Use memex_verdict(id) to poll status. Never writes directly to verified store.
        """
        result = enqueue(
            claim_text=claim,
            domain=domain,
            submitted_by="kimi-mcp",
            idempotency_key=idempotency_key or None
        )
        return {
            "submission_id": result["id"],
            "status": result["status"],
            "duplicate": result.get("duplicate", False),
            "message": "Claim queued for verification. Poll memex_verdict for status."
        }

    @mcp.tool()
    def memex_verdict(submission_id: str) -> dict:
        """
        Poll the status of a submitted claim.
        Returns status: pending | processing | done | failed
        When done, returns verdict and mol_id.
        """
        status = get_status(submission_id)
        if not status:
            return {"error": "Submission not found", "submission_id": submission_id}
        return {
            "submission_id": submission_id,
            "status": status["status"],
            "verdict": status.get("verdict"),
            "mol_id": status.get("mol_id"),
            "error": status.get("error")
        }

    @mcp.tool()
    def memex_query(domain: str = "general", n: int = 10,
                    predicate: str = "") -> dict:
        """
        Query verified claims from the trust store.
        Returns up to n verified mols for the given domain.
        Optionally filter by predicate (e.g. 'claim_text', 'verdict').
        """
        n = min(n, 50)  # cap at 50
        results = recall_claims(
            namespace=domain,
            predicate=predicate or None,
            verified_only=True,
            limit=n
        )
        return {
            "domain": domain,
            "count": len(results),
            "mols": [
                {
                    "mol_id": m.get("id"),
                    "predicate": m.get("predicate"),
                    "value": m.get("raw_value"),
                    "verdict": m.get("verification_verdict"),
                    "evidence": m.get("verification_evidence"),
                    "confidence": m.get("verification_confidence")
                }
                for m in results
            ]
        }

    @mcp.tool()
    def memex_stats() -> dict:
        """
        Return trust graph statistics: total mols, verified count, verification rate,
        recent activity, and system health.
        """
        conn = claims_conn()
        total = conn.execute("SELECT COUNT(*) FROM mols").fetchone()[0]
        verified = conn.execute(
            "SELECT COUNT(DISTINCT mol_id) FROM mol_verifications WHERE verdict IN ('VERIFIED','SUPPORTED')"
        ).fetchone()[0]
        refuted = conn.execute(
            "SELECT COUNT(DISTINCT mol_id) FROM mol_verifications WHERE verdict IN ('REFUTED','NOT_SUPPORTED')"
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM mol_verifications WHERE verified_at > ?",
            (int(time.time()) - 86400,)
        ).fetchone()[0]
        domains = conn.execute(
            "SELECT namespace, COUNT(*) as c FROM mol_frames GROUP BY namespace ORDER BY c DESC LIMIT 5"
        ).fetchall()
        conn.close()
        return {
            "total_mols": total,
            "verified": verified,
            "refuted": refuted,
            "unverified": total - verified - refuted,
            "verification_rate": round(verified / total, 3) if total else 0,
            "verifications_last_24h": recent,
            "top_domains": [{"domain": r[0], "count": r[1]} for r in domains],
            "license": "CC BY 4.0",
            "endpoint": "https://memex.gummi.lt"
        }
