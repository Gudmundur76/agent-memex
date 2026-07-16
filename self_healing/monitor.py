"""
Memex Self-Healing Monitor

Runs continuously. Monitors API health, search quality, and DB size.
Prunes low-importance memories when DB exceeds 80% of MAX_DB_MB.
"""

import os
import time
import json
import logging
import sqlite3
import requests
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("memex-monitor")

MEMEX_API      = os.environ.get("MEMEX_API_URL", "http://localhost:8000")
CHECK_INTERVAL = int(os.environ.get("MONITOR_INTERVAL_SEC", "60"))
DB_PATH        = os.environ.get("MEMEX_DB_PATH", "/app/data/memex.db")
MAX_DB_MB      = int(os.environ.get("MAX_DB_MB", "500"))


def check_health() -> bool:
    try:
        r = requests.get(f"{MEMEX_API}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def check_db_size() -> float:
    p = Path(DB_PATH)
    return p.stat().st_size / (1024 * 1024) if p.exists() else 0.0


def prune_old_memories() -> int:
    conn = sqlite3.connect(DB_PATH)
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
    test_ns = "__memex_health_check__"
    test_content = "The quick brown fox jumps over the lazy dog — memex health check"
    try:
        r = requests.post(f"{MEMEX_API}/v1/memory", json={
            "namespace": test_ns, "content": test_content,
            "importance": 0.1, "source_agent": "monitor",
        }, timeout=10)
        if r.status_code != 200:
            return {"ok": False, "score": 0.0}
        r2 = requests.post(f"{MEMEX_API}/v1/memory/search", json={
            "namespace": test_ns, "query": "quick brown fox", "limit": 1,
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
    cycle = 0
    while True:
        cycle += 1
        status = {"timestamp": int(time.time()), "api_healthy": False, "db_size_mb": 0.0}
        healthy = check_health()
        status["api_healthy"] = healthy
        if not healthy:
            consecutive_failures += 1
            log.warning(f"API health check failed ({consecutive_failures} consecutive)")
        else:
            consecutive_failures = 0
        db_mb = check_db_size()
        status["db_size_mb"] = round(db_mb, 2)
        if db_mb > MAX_DB_MB * 0.8:
            log.warning(f"DB {db_mb:.1f}MB approaching limit — pruning")
            prune_old_memories()
        if cycle % 10 == 0:
            qc = run_quality_check()
            status["quality_ok"] = qc["ok"]
            status["quality_score"] = qc.get("score", 0.0)
            if not qc["ok"]:
                log.warning(f"Quality check failed: {qc}")
        write_status(status)
        log.info(f"Status: api={healthy} db={db_mb:.1f}MB")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()

