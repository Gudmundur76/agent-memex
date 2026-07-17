"""
Background queue processor.
Polls submission_queue every 30s, sends pending claims to ttruthdesk,
stores results as mol-frames in Memex.
"""
import asyncio, httpx, json, time, os, logging
from api.queue_db import get_pending, mark_processing, mark_done, mark_failed
from api.claims_db import store_mol_frame

logger = logging.getLogger("queue_processor")

TTRUTHDESK_URL = os.environ.get("TTRUTHDESK_URL", "http://ttruthdesk-api:3000")
POLL_INTERVAL = int(os.environ.get("QUEUE_POLL_INTERVAL", "30"))

async def process_one(item: dict):
    qid = item["id"]
    claim = item["claim_text"]
    domain = item["domain"]
    mark_processing(qid)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{TTRUTHDESK_URL}/api/public/verify-claim",
                json={"claim": claim}
            )
            r.raise_for_status()
            data = r.json()
        verdict = data.get("verdict", "INSUFFICIENT_EVIDENCE")
        signal = data.get("signalDensity", 0.0)
        pmids = [p.get("pmid") for p in data.get("pubmedResults", []) if p.get("pmid")]
        evidence = f"ttruthdesk:{verdict}:signal={signal:.2f}"
        if pmids:
            evidence += f":pmids={','.join(str(p) for p in pmids[:3])}"
        # Store as mol-frame
        frame = store_mol_frame(
            namespace=domain,
            schema_fields=["claim_text", "verdict", "signal_density"],
            mols=[
                {"predicate": "claim_text", "raw_value": claim, "is_missing": False},
                {"predicate": "verdict", "raw_value": verdict, "is_missing": False},
                {"predicate": "signal_density", "raw_value": str(signal), "is_missing": False},
            ],
            source_node="ttruthdesk",
            extracted_by="queue_processor",
            source_hash=qid
        )
        mol_id = frame["mols"][0]["id"] if frame.get("mols") else None
        mark_done(qid, verdict, mol_id)
        logger.info(f"[queue] {qid[:8]} → {verdict} (signal={signal:.2f})")
    except Exception as e:
        mark_failed(qid, str(e)[:200])
        logger.error(f"[queue] {qid[:8]} FAILED: {e}")

async def run_processor():
    logger.info(f"[queue] Processor started (poll every {POLL_INTERVAL}s)")
    while True:
        try:
            pending = get_pending(limit=5)
            if pending:
                logger.info(f"[queue] Processing {len(pending)} items")
                for item in pending:
                    await process_one(item)
        except Exception as e:
            logger.error(f"[queue] Processor error: {e}")
        await asyncio.sleep(POLL_INTERVAL)
