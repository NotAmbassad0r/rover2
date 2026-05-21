"""SQLite time-series store for ROVER2 metrics with automatic retention."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).parent / "data" / "metrics.db"
DEFAULT_RETENTION_DAYS = 7

_db: sqlite3.Connection | None = None


def _open() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            ts   INTEGER NOT NULL,
            name TEXT    NOT NULL,
            value REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_name_ts ON metrics(name, ts)")
    conn.commit()
    return conn


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = _open()
    return _db


def write(points: dict[str, float | None], ts: int | None = None) -> None:
    rows = [(ts or int(time.time()), k, v) for k, v in points.items() if v is not None]
    if not rows:
        return
    conn = db()
    conn.executemany("INSERT INTO metrics(ts, name, value) VALUES(?,?,?)", rows)
    conn.commit()


def query(name: str, since_ts: int) -> list[dict[str, Any]]:
    rows = db().execute(
        "SELECT ts, value FROM metrics WHERE name=? AND ts>=? ORDER BY ts",
        (name, since_ts),
    ).fetchall()
    return [{"ts": r[0], "v": r[1]} for r in rows]


def query_latest(names: list[str], since_ts: int) -> dict[str, float | None]:
    """Return the most recent value for each named metric since since_ts."""
    result: dict[str, float | None] = {}
    conn = db()
    for name in names:
        row = conn.execute(
            "SELECT value FROM metrics WHERE name=? AND ts>=? ORDER BY ts DESC LIMIT 1",
            (name, since_ts),
        ).fetchone()
        result[name] = row[0] if row else None
    return result


def available() -> list[str]:
    rows = db().execute(
        "SELECT DISTINCT name FROM metrics ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def purge(retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    cutoff = int(time.time()) - retention_days * 86400
    cur = db().execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
    db().commit()
    return cur.rowcount


def db_size_mb() -> float:
    try:
        return round(_DB_PATH.stat().st_size / (1024 * 1024), 2)
    except FileNotFoundError:
        return 0.0
