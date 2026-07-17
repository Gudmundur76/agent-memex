"""
SM-1 Claim-tier database helpers for Memex.
Stores mol-frames (schema-bound claim units) with provenance,
typed absence reasons, and independent verification events.
"""
import sqlite3, json, uuid, time, hashlib
from typing import Optional

DB_PATH = "/data/memex.db"

ABSENCE_REASONS = {
    "ABSENT", "AMBIGUOUS", "NON_CANONICAL", "UNPARSABLE", "CONFLICTING", "REDACTED"
}

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_claim_tables():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mol_frames (
            id TEXT PRIMARY KEY,
            namespace TEXT NOT NULL,
            schema_fields TEXT NOT NULL,
            source_hash TEXT,
            source_node TEXT DEFAULT 'unknown',
            extracted_by TEXT DEFAULT 'unknown',
            emitted_at INTEGER NOT NULL,
            integrity_score REAL,
            fields_exact INTEGER DEFAULT 0,
            fields_missing_correct INTEGER DEFAULT 0,
            fields_corrupt INTEGER DEFAULT 0,
            frame_safety_pass INTEGER DEFAULT 1,
            raw_source TEXT
        );
        CREATE TABLE IF NOT EXISTS mols (
            id TEXT PRIMARY KEY,
            frame_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            value TEXT,
            value_type TEXT DEFAULT 'string',
            is_missing INTEGER DEFAULT 0,
            absence_reason TEXT,
            source_hash TEXT,
            source_node TEXT DEFAULT 'unknown',
            extracted_by TEXT DEFAULT 'unknown',
            emitted_at INTEGER NOT NULL,
            FOREIGN KEY (frame_id) REFERENCES mol_frames(id)
        );
        CREATE TABLE IF NOT EXISTS mol_verifications (
            id TEXT PRIMARY KEY,
            mol_id TEXT NOT NULL,
            frame_id TEXT NOT NULL,
            verified_by TEXT NOT NULL,
            verified_at INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            confidence REAL,
            evidence TEXT,
            notes TEXT,
            FOREIGN KEY (mol_id) REFERENCES mols(id)
        );
        CREATE INDEX IF NOT EXISTS idx_mols_frame ON mols(frame_id);
        CREATE INDEX IF NOT EXISTS idx_mols_namespace ON mols(namespace);
        CREATE INDEX IF NOT EXISTS idx_mols_subject ON mols(subject);
        CREATE INDEX IF NOT EXISTS idx_mols_predicate ON mols(predicate);
        CREATE INDEX IF NOT EXISTS idx_verifications_mol ON mol_verifications(mol_id);
    """)
    conn.commit()
    conn.close()

def _mol_id(frame_id: str, predicate: str) -> str:
    return "sha256:" + hashlib.sha256(f"{frame_id}:{predicate}".encode()).hexdigest()[:16]

def store_mol_frame(
    namespace: str,
    subject: str,
    schema_fields: list,
    mols_data: list,
    source_hash: str = None,
    source_node: str = "unknown",
    extracted_by: str = "unknown",
    raw_source: str = None,
) -> dict:
    """
    Store a complete mol-frame with all its mols.
    mols_data: list of dicts with keys: predicate, value (or None for MISSING),
               value_type, absence_reason (optional)
    """
    conn = get_conn()
    frame_id = "sha256:" + hashlib.sha256(
        f"{namespace}:{subject}:{int(time.time()*1000)}".encode()
    ).hexdigest()[:16]
    now = int(time.time() * 1000)

    # Compute integrity metrics
    fields_exact = 0
    fields_missing_correct = 0
    fields_corrupt = 0
    has_confabulation = False

    processed_mols = []
    for m in mols_data:
        predicate = m["predicate"]
        raw_value = m.get("value")
        is_missing = raw_value is None or str(raw_value).upper() == "MISSING"
        absence_reason = m.get("absence_reason", "ABSENT") if is_missing else None
        corrupt = m.get("corrupt", False)

        if corrupt:
            fields_corrupt += 1
            has_confabulation = True
        elif is_missing:
            fields_missing_correct += 1
        else:
            fields_exact += 1

        mol_id = _mol_id(frame_id, predicate)
        processed_mols.append({
            "id": mol_id,
            "frame_id": frame_id,
            "namespace": namespace,
            "subject": subject,
            "predicate": predicate,
            "value": None if is_missing else str(raw_value),
            "value_type": m.get("value_type", "string"),
            "is_missing": 1 if is_missing else 0,
            "absence_reason": absence_reason,
            "source_hash": source_hash,
            "source_node": source_node,
            "extracted_by": extracted_by,
            "emitted_at": now,
        })

    total = fields_exact + fields_missing_correct + fields_corrupt
    integrity_score = 0.0 if has_confabulation else (
        (fields_exact + fields_missing_correct) / total if total > 0 else 1.0
    )
    frame_safety_pass = 0 if has_confabulation else 1

    conn.execute(
        """INSERT INTO mol_frames
           (id, namespace, schema_fields, source_hash, source_node, extracted_by,
            emitted_at, integrity_score, fields_exact, fields_missing_correct,
            fields_corrupt, frame_safety_pass, raw_source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (frame_id, namespace, json.dumps(schema_fields), source_hash, source_node,
         extracted_by, now, integrity_score, fields_exact, fields_missing_correct,
         fields_corrupt, frame_safety_pass, raw_source)
    )
    for m in processed_mols:
        conn.execute(
            """INSERT INTO mols
               (id, frame_id, namespace, subject, predicate, value, value_type,
                is_missing, absence_reason, source_hash, source_node, extracted_by, emitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (m["id"], m["frame_id"], m["namespace"], m["subject"], m["predicate"],
             m["value"], m["value_type"], m["is_missing"], m["absence_reason"],
             m["source_hash"], m["source_node"], m["extracted_by"], m["emitted_at"])
        )
    conn.commit()

    frame_row = conn.execute("SELECT * FROM mol_frames WHERE id=?", (frame_id,)).fetchone()
    mol_rows  = conn.execute("SELECT * FROM mols WHERE frame_id=?", (frame_id,)).fetchall()
    conn.close()
    return _frame_to_dict(frame_row, mol_rows)

def recall_claims(
    namespace: str,
    subject: str = None,
    predicate: str = None,
    verified_only: bool = False,
    include_missing: bool = True,
    limit: int = 20,
) -> list:
    conn = get_conn()
    q = "SELECT m.* FROM mols m"
    params = []
    conditions = ["m.namespace=?"]
    params.append(namespace)

    if subject:
        conditions.append("m.subject=?")
        params.append(subject)
    if predicate:
        conditions.append("m.predicate=?")
        params.append(predicate)
    if not include_missing:
        conditions.append("m.is_missing=0")
    if verified_only:
        q += " JOIN mol_verifications v ON v.mol_id=m.id AND v.verdict='VERIFIED'"

    q += " WHERE " + " AND ".join(conditions)
    q += " ORDER BY m.emitted_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [_mol_to_dict(r) for r in rows]

def add_verification(
    mol_id: str,
    verified_by: str,
    verdict: str,
    confidence: float = None,
    evidence: str = None,
    notes: str = None,
) -> dict:
    """
    Attach an independent verification event to a mol.
    verdict: VERIFIED | DISPUTED | UNVERIFIED | SUPERSEDED
    """
    conn = get_conn()
    mol = conn.execute("SELECT * FROM mols WHERE id=?", (mol_id,)).fetchone()
    if not mol:
        conn.close()
        raise ValueError(f"mol {mol_id} not found")

    ver_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    conn.execute(
        """INSERT INTO mol_verifications
           (id, mol_id, frame_id, verified_by, verified_at, verdict, confidence, evidence, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ver_id, mol_id, mol["frame_id"], verified_by, now, verdict, confidence, evidence, notes)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM mol_verifications WHERE id=?", (ver_id,)).fetchone()
    conn.close()
    return dict(row)

def get_frame(frame_id: str) -> Optional[dict]:
    conn = get_conn()
    frame = conn.execute("SELECT * FROM mol_frames WHERE id=?", (frame_id,)).fetchone()
    if not frame:
        conn.close()
        return None
    mols = conn.execute("SELECT * FROM mols WHERE frame_id=?", (frame_id,)).fetchall()
    conn.close()
    return _frame_to_dict(frame, mols)

def _mol_to_dict(row) -> dict:
    d = dict(row)
    return d

def _frame_to_dict(frame_row, mol_rows) -> dict:
    d = dict(frame_row)
    d["schema_fields"] = json.loads(d.get("schema_fields") or "[]")
    d["mols"] = [_mol_to_dict(m) for m in mol_rows]
    return d
