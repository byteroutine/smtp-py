import sqlite3
from datetime import datetime, timezone

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS transmissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_text TEXT,
    body_html TEXT,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_transmissions_status ON transmissions(status);
CREATE INDEX IF NOT EXISTS idx_transmissions_recipient ON transmissions(recipient);
CREATE INDEX IF NOT EXISTS idx_transmissions_created_at ON transmissions(created_at);
"""

VALID_STATUSES = {"QUEUED", "SENDING", "RETRYING", "SENT", "FAILED"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_path(database_path: str | None) -> str:
    if database_path:
        return database_path
    return get_settings().database_path


def _connect(database_path: str | None = None) -> sqlite3.Connection:
    path = _resolve_path(database_path)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA busy_timeout=5000;")
    connection.execute("PRAGMA foreign_keys=ON;")
    return connection


def init_db(database_path: str | None = None) -> str:
    path = _resolve_path(database_path)
    connection = _connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.commit()
    finally:
        connection.close()
    return path


def queue_email(
    recipient: str,
    subject: str,
    body_text: str | None = None,
    body_html: str | None = None,
    database_path: str | None = None,
) -> int:
    now = _now_iso()
    connection = _connect(database_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO transmissions
                (recipient, subject, body_text, body_html, status, attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'QUEUED', 0, ?, ?)
            """,
            (recipient.strip(), subject, body_text, body_html, now, now),
        )
        transmission_id = int(cursor.lastrowid)
        connection.commit()
        return transmission_id
    finally:
        connection.close()


def update_status(
    transmission_id: int,
    status: str,
    last_error: str | None = None,
    increment_attempts: bool = False,
    database_path: str | None = None,
) -> None:
    normalized = status.upper()
    if normalized not in VALID_STATUSES:
        raise ValueError(f"Unknown status: {status}")
    now = _now_iso()
    connection = _connect(database_path)
    try:
        if increment_attempts:
            connection.execute(
                """
                UPDATE transmissions
                SET status = ?, last_error = ?, attempts = attempts + 1, updated_at = ?,
                    sent_at = CASE WHEN ? = 'SENT' THEN ? ELSE sent_at END
                WHERE id = ?
                """,
                (normalized, last_error, now, normalized, now, transmission_id),
            )
        else:
            connection.execute(
                """
                UPDATE transmissions
                SET status = ?, last_error = ?, updated_at = ?,
                    sent_at = CASE WHEN ? = 'SENT' THEN ? ELSE sent_at END
                WHERE id = ?
                """,
                (normalized, last_error, now, normalized, now, transmission_id),
            )
        connection.commit()
    finally:
        connection.close()


def fetch_by_status(
    status: str,
    limit: int = 100,
    database_path: str | None = None,
) -> list[dict]:
    normalized = status.upper()
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM transmissions WHERE status = ? ORDER BY id ASC LIMIT ?",
            (normalized, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def fetch_recent(limit: int = 50, database_path: str | None = None) -> list[dict]:
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM transmissions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_stats(database_path: str | None = None) -> dict:
    connection = _connect(database_path)
    try:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM transmissions GROUP BY status"
        ).fetchall()
        stats: dict[str, int] = {status: 0 for status in VALID_STATUSES}
        for row in rows:
            stats[row["status"]] = int(row["total"])
        stats["TOTAL"] = sum(stats.values())
        return stats
    finally:
        connection.close()


def reset_retryable(database_path: str | None = None) -> int:
    now = _now_iso()
    connection = _connect(database_path)
    try:
        cursor = connection.execute(
            "UPDATE transmissions SET status = 'QUEUED', last_error = NULL, updated_at = ? WHERE status = 'FAILED'",
            (now,),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def claim_transmission(transmission_id: int, database_path: str | None = None) -> bool:
    now = _now_iso()
    connection = _connect(database_path)
    try:
        cursor = connection.execute(
            """
            UPDATE transmissions
            SET status = 'SENDING', updated_at = ?
            WHERE id = ? AND status IN ('QUEUED', 'RETRYING')
            """,
            (now, transmission_id),
        )
        connection.commit()
        return int(cursor.rowcount) == 1
    finally:
        connection.close()
