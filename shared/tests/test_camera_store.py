from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from oak_dashcam_shared.camera_store import CameraStore
from oak_dashcam_shared.config import CameraConfig, CameraRole, Codec, Resolution


def _cam(
    id: str = "front",
    mxid: str = "auto",
    role: CameraRole = CameraRole.FRONT,
    fps: int = 30,
    codec: Codec = Codec.H265,
    resolution: Resolution = Resolution.R_1080P,
    bitrate_kbps: int = 8000,
) -> CameraConfig:
    return CameraConfig(
        id=id,
        mxid=mxid,
        role=role,
        resolution=resolution,
        fps=fps,
        codec=codec,
        bitrate_kbps=bitrate_kbps,
    )


def test_store_creates_table_and_starts_empty(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    assert store.is_empty() is True
    assert store.list_all() == []


def test_insert_and_list_roundtrip(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    store.insert(_cam(id="front", mxid="A", role=CameraRole.FRONT))
    store.insert(_cam(id="rear", mxid="B", role=CameraRole.REAR, codec=Codec.H264))

    cams = store.list_all()
    assert [c.id for c in cams] == ["front", "rear"]
    assert cams[0].mxid == "A"
    assert cams[0].role is CameraRole.FRONT
    assert cams[1].codec is Codec.H264


def test_insert_duplicate_id_raises(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    store.insert(_cam(id="front"))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert(_cam(id="front", mxid="X"))


def test_get_returns_none_for_missing(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    assert store.get("nope") is None


def test_update_overwrites_existing(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    store.insert(_cam(id="front", fps=30))
    assert store.update(_cam(id="front", fps=60)) is True
    assert store.get("front").fps == 60  # type: ignore[union-attr]


def test_update_unknown_camera_returns_false(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    assert store.update(_cam(id="nope")) is False


def test_delete_removes_row(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    store.insert(_cam(id="front"))
    assert store.delete("front") is True
    assert store.get("front") is None
    assert store.delete("front") is False


def test_seed_if_empty_inserts_all_cameras(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    seeded = store.seed_if_empty([_cam(id="front"), _cam(id="rear", role=CameraRole.REAR)])
    assert seeded is True
    assert [c.id for c in store.list_all()] == ["front", "rear"]


def test_seed_if_empty_is_noop_when_populated(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    store.insert(_cam(id="existing", mxid="EXISTING"))
    seeded = store.seed_if_empty([_cam(id="front"), _cam(id="rear")])
    assert seeded is False
    # Still only the pre-existing row.
    assert [c.id for c in store.list_all()] == ["existing"]


def test_store_persists_across_reopens(tmp_path: Path) -> None:
    db = tmp_path / "dashcam.db"
    CameraStore(db).insert(_cam(id="front", mxid="ABC"))
    assert CameraStore(db).get("front") is not None  # type: ignore[union-attr]


def test_rotation_default_is_zero(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    store.insert(_cam(id="front"))
    cam = store.get("front")
    assert cam is not None
    assert cam.rotation_degrees == 0


def test_rotation_insert_and_update(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    cam = CameraConfig(
        id="front",
        role=CameraRole.FRONT,
        rotation_degrees=180,
    )
    store.insert(cam)
    assert store.get("front").rotation_degrees == 180  # type: ignore[union-attr]

    updated = cam.model_copy(update={"rotation_degrees": 0})
    assert store.update(updated) is True
    assert store.get("front").rotation_degrees == 0  # type: ignore[union-attr]


def test_rotation_rejects_90_and_270() -> None:
    # 90°/270° aren't supported — the Literal type should raise.
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        CameraConfig(id="front", role=CameraRole.FRONT, rotation_degrees=90)  # type: ignore[arg-type]
    with pytest.raises(pydantic.ValidationError):
        CameraConfig(id="front", role=CameraRole.FRONT, rotation_degrees=270)  # type: ignore[arg-type]
    # Sanity: 0 and 180 are accepted.
    assert CameraConfig(id="a", role=CameraRole.FRONT, rotation_degrees=0).rotation_degrees == 0
    assert (
        CameraConfig(id="b", role=CameraRole.FRONT, rotation_degrees=180).rotation_degrees == 180
    )


def test_rotation_migration_adds_column_to_old_db(tmp_path: Path) -> None:
    """A DB created before the rotation_degrees column existed should
    gain the column transparently on the next open, with existing
    rows defaulting to 0."""
    db = tmp_path / "dashcam.db"
    # Hand-craft a pre-migration DB: the old schema, one row inserted
    # via bare SQL (bypassing CameraStore so we don't trigger the
    # migration path).
    old_schema = """
    CREATE TABLE cameras (
        id TEXT PRIMARY KEY,
        mxid TEXT NOT NULL,
        role TEXT NOT NULL,
        resolution TEXT NOT NULL,
        fps INTEGER NOT NULL,
        codec TEXT NOT NULL,
        bitrate_kbps INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """
    with sqlite3.connect(db) as conn:
        conn.executescript(old_schema)
        conn.execute(
            """
            INSERT INTO cameras
                (id, mxid, role, resolution, fps, codec, bitrate_kbps,
                 created_at, updated_at)
            VALUES ('legacy', 'auto', 'front', '1080p', 30, 'h265', 8000,
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )

    # Opening through CameraStore should ALTER TABLE ADD COLUMN on the fly.
    store = CameraStore(db)
    legacy = store.get("legacy")
    assert legacy is not None
    assert legacy.rotation_degrees == 0  # DEFAULT 0 applied to existing row

    # Second open must be a no-op (safe to run repeatedly).
    store2 = CameraStore(db)
    assert store2.get("legacy") is not None
