"""SQLite-backed store of configured cameras.

Cameras started out in `config/dashcam.yaml` and were loaded fresh on every
capture service startup. Once the webapp grew a "configure cameras" UI,
that model stopped working — edits from the browser needed to survive
restarts, so cameras moved here.

The YAML file is still read, but only as a **first-run seed**:
`seed_if_empty(cameras)` inserts the YAML's camera list the first time the
DB is populated, and is a no-op forever after. Subsequent edits happen via
`insert`/`update`/`delete` from the webapp, and the DB is the single source
of truth for camera identity.

Schema is a 1:1 mirror of `CameraConfig` plus audit timestamps, so
round-tripping DB ↔ pydantic model is trivial. Schema evolution is
handled by `_migrate` on every open — ALTER TABLE ADD COLUMN is fast,
atomic in SQLite, and safe to run repeatedly.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oak_dashcam_shared.config import CameraConfig, CameraRole, Codec, Resolution

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id TEXT PRIMARY KEY,
    mxid TEXT NOT NULL,
    role TEXT NOT NULL,
    resolution TEXT NOT NULL,
    fps INTEGER NOT NULL,
    codec TEXT NOT NULL,
    bitrate_kbps INTEGER NOT NULL,
    rotation_degrees INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply idempotent schema migrations for existing DBs.

    Every migration must be safe to run multiple times — we check
    whether a column already exists via `PRAGMA table_info` before
    adding it. SQLite doesn't support `ALTER TABLE ADD COLUMN IF NOT
    EXISTS` until 3.35, so we roll our own check.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    if "rotation_degrees" not in existing:
        conn.execute(
            "ALTER TABLE cameras ADD COLUMN rotation_degrees INTEGER NOT NULL DEFAULT 0"
        )


class CameraStore:
    """Synchronous SQLite wrapper for the cameras table."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------ reads

    def list_all(self) -> list[CameraConfig]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, mxid, role, resolution, fps, codec, bitrate_kbps,
                       rotation_degrees
                FROM cameras
                ORDER BY id
                """
            ).fetchall()
        return [_row_to_camera(row) for row in rows]

    def get(self, camera_id: str) -> CameraConfig | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, mxid, role, resolution, fps, codec, bitrate_kbps,
                       rotation_degrees
                FROM cameras
                WHERE id = ?
                """,
                (camera_id,),
            ).fetchone()
        return _row_to_camera(row) if row else None

    def is_empty(self) -> bool:
        with self._lock, self._connect() as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM cameras").fetchone()
        return int(count) == 0

    # ----------------------------------------------------------------- writes

    def insert(self, camera: CameraConfig) -> None:
        """Insert a new camera. Raises `sqlite3.IntegrityError` on duplicate id."""
        now = datetime.now(tz=UTC).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cameras
                    (id, mxid, role, resolution, fps, codec, bitrate_kbps,
                     rotation_degrees, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    camera.id,
                    camera.mxid,
                    camera.role.value,
                    camera.resolution.value,
                    camera.fps,
                    camera.codec.value,
                    camera.bitrate_kbps,
                    camera.rotation_degrees,
                    now,
                    now,
                ),
            )

    def update(self, camera: CameraConfig) -> bool:
        """Overwrite an existing camera. Returns True if the row matched."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE cameras
                SET mxid = ?, role = ?, resolution = ?, fps = ?, codec = ?,
                    bitrate_kbps = ?, rotation_degrees = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    camera.mxid,
                    camera.role.value,
                    camera.resolution.value,
                    camera.fps,
                    camera.codec.value,
                    camera.bitrate_kbps,
                    camera.rotation_degrees,
                    datetime.now(tz=UTC).isoformat(),
                    camera.id,
                ),
            )
            return cursor.rowcount > 0

    def delete(self, camera_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
            return cursor.rowcount > 0

    # ------------------------------------------------------------ seeding

    def seed_if_empty(self, cameras: list[CameraConfig]) -> bool:
        """Populate the table with `cameras` iff the table is currently empty.

        Returns True if seeding happened, False if the table already had
        rows (meaning we should ignore the YAML and use what's in the DB).
        Called exactly once per capture startup.
        """
        with self._lock, self._connect() as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM cameras").fetchone()
            if int(count) > 0:
                return False
            now = datetime.now(tz=UTC).isoformat()
            conn.executemany(
                """
                INSERT INTO cameras
                    (id, mxid, role, resolution, fps, codec, bitrate_kbps,
                     rotation_degrees, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        cam.id,
                        cam.mxid,
                        cam.role.value,
                        cam.resolution.value,
                        cam.fps,
                        cam.codec.value,
                        cam.bitrate_kbps,
                        cam.rotation_degrees,
                        now,
                        now,
                    )
                    for cam in cameras
                ],
            )
            return True


def _row_to_camera(row: tuple[Any, ...]) -> CameraConfig:
    return CameraConfig(
        id=str(row[0]),
        mxid=str(row[1]),
        role=CameraRole(row[2]),
        resolution=Resolution(row[3]),
        fps=int(row[4]),
        codec=Codec(row[5]),
        bitrate_kbps=int(row[6]),
        rotation_degrees=int(row[7]),  # type: ignore[arg-type]
    )
