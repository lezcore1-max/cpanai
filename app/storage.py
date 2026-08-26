"""
Persistent audit trail. Every inspection gets logged with its scores and
decision, and a reviewer can later confirm or override that decision — this
is the raw material for both the compliance audit trail and the feedback
loop that would improve detection thresholds over time.

SQLite is enough for a prototype; in production this would be a proper
event store / data warehouse table feeding a monitoring dashboard.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "controlplane.db"


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                use_case TEXT NOT NULL,
                question TEXT,
                response TEXT,
                responsibility_score INTEGER,
                performance_score INTEGER,
                cost_score INTEGER,
                total_score INTEGER,
                decision TEXT,
                reasoning TEXT,
                review TEXT,
                latency_ms INTEGER,
                over_budget INTEGER,
                override_reason TEXT,
                compound_incident INTEGER,
                incident_type TEXT
            )
            """
        )
        # Additive migrations for databases created before these columns existed.
        _add_column_if_missing(conn, "latency_ms", "INTEGER")
        _add_column_if_missing(conn, "over_budget", "INTEGER")
        _add_column_if_missing(conn, "override_reason", "TEXT")
        _add_column_if_missing(conn, "compound_incident", "INTEGER")
        _add_column_if_missing(conn, "incident_type", "TEXT")


def _add_column_if_missing(conn: sqlite3.Connection, col: str, typedef: str) -> None:
    try:
        conn.execute(f"ALTER TABLE audit_log ADD COLUMN {col} {typedef}")
    except sqlite3.OperationalError:
        pass  # Column already exists — safe to ignore


@dataclass
class LogEntryIn:
    use_case: str
    question: str
    response: str
    responsibility_score: int
    performance_score: int
    cost_score: int
    total_score: int
    decision: str
    reasoning: str
    latency_ms: int | None = None
    over_budget: bool | None = None
    override_reason: str | None = None
    compound_incident: bool | None = None
    incident_type: str | None = None


def insert_log(entry: LogEntryIn) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO audit_log
                (created_at, use_case, question, response, responsibility_score,
                 performance_score, cost_score, total_score, decision, reasoning,
                 review, latency_ms, over_budget, override_reason,
                 compound_incident, incident_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                entry.use_case,
                entry.question,
                entry.response,
                entry.responsibility_score,
                entry.performance_score,
                entry.cost_score,
                entry.total_score,
                entry.decision,
                entry.reasoning,
                entry.latency_ms,
                int(entry.over_budget) if entry.over_budget is not None else None,
                entry.override_reason,
                int(entry.compound_incident) if entry.compound_incident is not None else None,
                entry.incident_type,
            ),
        )
        return cur.lastrowid


def insert_pending(use_case: str, question: str, response: str) -> int:
    """
    Pre-insert a row with decision='PENDING' for async inspections. Returns
    the row ID so the background task can update it when inspection completes,
    and the frontend can poll for the result by ID.
    """
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO audit_log
                (created_at, use_case, question, response, decision)
            VALUES (?, ?, ?, ?, 'PENDING')
            """,
            (datetime.now(timezone.utc).isoformat(), use_case, question, response),
        )
        return cur.lastrowid


def update_log_entry(entry_id: int, entry: LogEntryIn) -> None:
    """Update a PENDING row with the completed inspection result."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE audit_log SET
                responsibility_score = ?,
                performance_score    = ?,
                cost_score           = ?,
                total_score          = ?,
                decision             = ?,
                reasoning            = ?,
                latency_ms           = ?,
                over_budget          = ?,
                override_reason      = ?,
                compound_incident    = ?,
                incident_type        = ?
            WHERE id = ?
            """,
            (
                entry.responsibility_score,
                entry.performance_score,
                entry.cost_score,
                entry.total_score,
                entry.decision,
                entry.reasoning,
                entry.latency_ms,
                int(entry.over_budget) if entry.over_budget is not None else None,
                entry.override_reason,
                int(entry.compound_incident) if entry.compound_incident is not None else None,
                entry.incident_type,
                entry_id,
            ),
        )


def get_log_entry(entry_id: int) -> dict | None:
    """Fetch a single audit log entry by ID (used for async polling)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE id = ?", (entry_id,)
        ).fetchone()
        return dict(row) if row else None


def list_log(limit: int = 200) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def set_review(entry_id: int, review: str | None) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE audit_log SET review = ? WHERE id = ?", (review, entry_id)
        )
        return cur.rowcount > 0


def clear_log() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM audit_log")


def get_metrics() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT decision, review FROM audit_log").fetchall()

    counts = {"PASS": 0, "FIX": 0, "HUMAN": 0, "BLOCK": 0}
    reviewed = 0
    confirmed = 0
    for row in rows:
        if row["decision"] in counts:
            counts[row["decision"]] += 1
        if row["review"] is not None:
            reviewed += 1
            if row["review"] == "confirm":
                confirmed += 1

    accuracy_pct = round((confirmed / reviewed) * 100) if reviewed else None
    return {
        "counts": counts,
        "reviewed": reviewed,
        "reviewer_confirmed_accuracy_pct": accuracy_pct,
        "total_inspections": len(rows),
    }
