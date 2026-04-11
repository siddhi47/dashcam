"""SQLite-backed index of recorded segments.

Both the capture service (writer) and the webapp backend (reader) agree on
the schema here. Capture inserts one row per finalized segment; the webapp
queries for listings, date ranges, and protected clips; retention reads the
`protected` flag to skip incident clips during loop-delete.

The DB file lives on the shared storage volume alongside the segments
themselves (`{storage.root}/dashcam.db`) so the webapp can be brought up
against an existing recording directory with zero bootstrap.

This module ships the synchronous client used by the capture service.
The webapp will wrap the same schema with `aiosqlite` for async reads.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    duration_s REAL NOT NULL,
    size_bytes INTEGER NOT NULL,
    codec TEXT NOT NULL,
    protected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_camera_started
    ON segments (camera_id, started_at DESC);
"""


@dataclass(frozen=True, slots=True)
class SegmentRecord:
    """One row in the segments table.

    `path` is stored **relative to the storage root** so the index stays
    portable if the root ever moves (rename, symlink, mounted elsewhere).
    """

    camera_id: str
    path: str
    started_at: datetime
    duration_s: float
    size_bytes: int
    codec: str
    protected: bool = False


class SegmentIndex:
    """Synchronous SQLite wrapper for the segments table.

    Thread-safe: each public method takes an internal lock and opens a
    short-lived connection. Write volume is low (one insert per segment
    rotation per camera, i.e. roughly once per 60 seconds per camera), so
    the overhead of not pooling connections is negligible compared to
    the complexity savings.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        # WAL gives the webapp concurrent read access while capture writes.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def insert(self, record: SegmentRecord) -> int:
        """Insert a segment row. Returns the new row id."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO segments
                    (camera_id, path, started_at, duration_s, size_bytes,
                     codec, protected, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.camera_id,
                    record.path,
                    record.started_at.isoformat(),
                    record.duration_s,
                    record.size_bytes,
                    record.codec,
                    int(record.protected),
                    datetime.now(tz=UTC).isoformat(),
                ),
            )
            new_id = cursor.lastrowid
            assert new_id is not None
            return new_id

    def list_by_camera(
        self,
        camera_id: str,
        *,
        limit: int = 100,
        before: datetime | None = None,
    ) -> list[SegmentRecord]:
        """Most recent segments for one camera, newest first.

        `before` lets the caller page backwards through history: pass
        the `started_at` of the oldest row from the previous page and
        you get the next (older) page of segments strictly before that
        timestamp. This pairs naturally with the `(camera_id, started_at DESC)`
        index so each page is an O(limit) seek.
        """
        where = "WHERE camera_id = ?"
        params: list[object] = [camera_id]
        if before is not None:
            where += " AND started_at < ?"
            params.append(before.isoformat())
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT camera_id, path, started_at, duration_s, size_bytes,
                       codec, protected
                FROM segments
                {where}
                ORDER BY started_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_all(
        self,
        *,
        limit: int = 1000,
        before: datetime | None = None,
    ) -> list[SegmentRecord]:
        """All segments newest-first, optionally paged via `before` timestamp."""
        where = ""
        params: list[object] = []
        if before is not None:
            where = "WHERE started_at < ?"
            params.append(before.isoformat())
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT camera_id, path, started_at, duration_s, size_bytes,
                       codec, protected
                FROM segments
                {where}
                ORDER BY started_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def get_by_path(self, path: str) -> SegmentRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT camera_id, path, started_at, duration_s, size_bytes,
                       codec, protected
                FROM segments
                WHERE path = ?
                """,
                (path,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def set_protected(self, path: str, protected: bool) -> bool:
        """Mark a segment as protected (or not). Returns True if it matched."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE segments SET protected = ? WHERE path = ?",
                (int(protected), path),
            )
            return cursor.rowcount > 0

    def delete_by_path(self, path: str) -> bool:
        """Remove a row by its relative path. Used when retention deletes a file."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM segments WHERE path = ?", (path,))
            return cursor.rowcount > 0

    def protected_paths(self) -> set[str]:
        """Relative paths of all segments currently flagged as protected."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT path FROM segments WHERE protected = 1").fetchall()
        return {row[0] for row in rows}


def _row_to_record(row: tuple[Any, ...]) -> SegmentRecord:
    return SegmentRecord(
        camera_id=str(row[0]),
        path=str(row[1]),
        started_at=datetime.fromisoformat(str(row[2])),
        duration_s=float(row[3]),
        size_bytes=int(row[4]),
        codec=str(row[5]),
        protected=bool(row[6]),
    )
