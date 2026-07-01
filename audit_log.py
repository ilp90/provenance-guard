"""Structured audit log + submission state, backed by SQLite.

Two tables:
  submissions  — current state of each piece of content (for appeals in M5)
  audit_log    — append-only, one row per event (classification | appeal)

Everything is structured (no print() logging). See planning.md §1, §7.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).with_name("provenance.db")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                content_id   TEXT PRIMARY KEY,
                creator_id   TEXT,
                text         TEXT NOT NULL,
                title        TEXT,
                attribution  TEXT,
                confidence   REAL,
                llm_score    REAL,
                stylometry_score REAL,
                status       TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id  TEXT NOT NULL,
                creator_id  TEXT,
                timestamp   TEXT NOT NULL,
                event_type  TEXT NOT NULL,          -- 'classification' | 'appeal'
                attribution TEXT,
                confidence  REAL,
                llm_score   REAL,
                stylometry_score REAL,
                status      TEXT,
                detail      TEXT                    -- JSON blob for extras (rationale, appeal reason)
            )
            """
        )


def record_classification(*, content_id, creator_id, text, title,
                          attribution, confidence, llm_score,
                          stylometry_score=None, status="classified",
                          detail=None):
    """Persist a submission's decision and append a classification audit event."""
    ts = _now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO submissions
                (content_id, creator_id, text, title, attribution, confidence,
                 llm_score, stylometry_score, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (content_id, creator_id, text, title, attribution, confidence,
             llm_score, stylometry_score, status, ts),
        )
        conn.execute(
            """
            INSERT INTO audit_log
                (content_id, creator_id, timestamp, event_type, attribution,
                 confidence, llm_score, stylometry_score, status, detail)
            VALUES (?, ?, ?, 'classification', ?, ?, ?, ?, ?, ?)
            """,
            (content_id, creator_id, ts, attribution, confidence, llm_score,
             stylometry_score, status,
             json.dumps(detail or {})),
        )
    return ts


def record_appeal(*, content_id, reason):
    """Flip a submission to under_review and append an appeal audit event.

    Returns the submission row (dict) if found, else None.
    """
    ts = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE content_id = ?", (content_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE submissions SET status = 'under_review' WHERE content_id = ?",
            (content_id,),
        )
        conn.execute(
            """
            INSERT INTO audit_log
                (content_id, creator_id, timestamp, event_type, attribution,
                 confidence, llm_score, stylometry_score, status, detail)
            VALUES (?, ?, ?, 'appeal', ?, ?, ?, ?, 'under_review', ?)
            """,
            (content_id, row["creator_id"], ts, row["attribution"],
             row["confidence"], row["llm_score"], row["stylometry_score"],
             json.dumps({"appeal_reasoning": reason,
                         "original_decision": {
                             "attribution": row["attribution"],
                             "confidence": row["confidence"],
                             "llm_score": row["llm_score"],
                             "stylometry_score": row["stylometry_score"],
                         }})),
        )
        return dict(row)


def get_log(limit=50):
    """Return the most recent audit-log entries as a list of dicts."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    entries = []
    for r in rows:
        e = dict(r)
        try:
            e["detail"] = json.loads(e["detail"]) if e["detail"] else {}
        except (TypeError, json.JSONDecodeError):
            e["detail"] = {}
        entries.append(e)
    return entries
