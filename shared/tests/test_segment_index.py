from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from oak_dashcam_shared.segment_index import SegmentIndex, SegmentRecord


def _record(
    camera_id: str = "front",
    path: str = "front/2026-04-10/12-00-00.mp4",
    started_at: datetime | None = None,
    duration_s: float = 60.0,
    size_bytes: int = 1_000_000,
    codec: str = "h265",
    protected: bool = False,
) -> SegmentRecord:
    return SegmentRecord(
        camera_id=camera_id,
        path=path,
        started_at=started_at or datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
        duration_s=duration_s,
        size_bytes=size_bytes,
        codec=codec,
        protected=protected,
    )


def test_index_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "dashcam.db"
    SegmentIndex(db_path)
    assert db_path.exists()
    # Sanity-check the table exists.
    with sqlite3.connect(db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "segments" in names


def test_insert_returns_id_and_get_roundtrip(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    rec = _record()
    new_id = idx.insert(rec)
    assert new_id > 0

    fetched = idx.get_by_path(rec.path)
    assert fetched is not None
    assert fetched.camera_id == rec.camera_id
    assert fetched.path == rec.path
    assert fetched.started_at == rec.started_at
    assert fetched.duration_s == rec.duration_s
    assert fetched.size_bytes == rec.size_bytes
    assert fetched.codec == rec.codec
    assert fetched.protected is False


def test_insert_duplicate_path_raises(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    idx.insert(_record())
    with pytest.raises(sqlite3.IntegrityError):
        idx.insert(_record())


def test_list_by_camera_orders_newest_first(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    idx.insert(_record(path="front/1.mp4", started_at=base))
    idx.insert(_record(path="front/2.mp4", started_at=base + timedelta(minutes=1)))
    idx.insert(_record(path="front/3.mp4", started_at=base + timedelta(minutes=2)))
    idx.insert(_record(path="rear/1.mp4", camera_id="rear", started_at=base))

    rows = idx.list_by_camera("front")
    assert [r.path for r in rows] == ["front/3.mp4", "front/2.mp4", "front/1.mp4"]
    rear_rows = idx.list_by_camera("rear")
    assert [r.path for r in rear_rows] == ["rear/1.mp4"]


def test_list_by_camera_limit(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    for i in range(5):
        idx.insert(_record(path=f"front/{i}.mp4", started_at=base + timedelta(minutes=i)))
    assert len(idx.list_by_camera("front", limit=3)) == 3


def test_list_by_camera_before_returns_older_page(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    for i in range(5):
        idx.insert(_record(path=f"front/{i}.mp4", started_at=base + timedelta(minutes=i)))

    first_page = idx.list_by_camera("front", limit=2)
    assert [r.path for r in first_page] == ["front/4.mp4", "front/3.mp4"]

    # Use the oldest `started_at` from the first page as the pivot.
    next_page = idx.list_by_camera("front", limit=2, before=first_page[-1].started_at)
    assert [r.path for r in next_page] == ["front/2.mp4", "front/1.mp4"]

    final_page = idx.list_by_camera("front", limit=10, before=next_page[-1].started_at)
    assert [r.path for r in final_page] == ["front/0.mp4"]


def test_list_all_before_pages_older_segments(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    idx.insert(_record(path="front/a.mp4", started_at=base))
    idx.insert(_record(path="front/b.mp4", started_at=base + timedelta(minutes=1)))
    idx.insert(_record(path="rear/c.mp4", camera_id="rear", started_at=base + timedelta(minutes=2)))

    first = idx.list_all(limit=2)
    assert [r.path for r in first] == ["rear/c.mp4", "front/b.mp4"]

    older = idx.list_all(limit=5, before=first[-1].started_at)
    assert [r.path for r in older] == ["front/a.mp4"]


def test_list_all_across_cameras(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    idx.insert(_record(path="front/1.mp4", started_at=base))
    idx.insert(
        _record(path="rear/1.mp4", camera_id="rear", started_at=base + timedelta(seconds=30))
    )
    rows = idx.list_all()
    assert {r.camera_id for r in rows} == {"front", "rear"}
    # Newest first.
    assert rows[0].camera_id == "rear"


def test_set_protected_toggles_flag_and_reports_match(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    rec = _record()
    idx.insert(rec)
    assert idx.set_protected(rec.path, True) is True
    assert idx.get_by_path(rec.path).protected is True  # type: ignore[union-attr]
    assert idx.set_protected(rec.path, False) is True
    assert idx.get_by_path(rec.path).protected is False  # type: ignore[union-attr]


def test_set_protected_unknown_path_returns_false(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    assert idx.set_protected("nope/nope.mp4", True) is False


def test_delete_by_path_removes_row(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    rec = _record()
    idx.insert(rec)
    assert idx.delete_by_path(rec.path) is True
    assert idx.get_by_path(rec.path) is None
    # Second delete reports no match.
    assert idx.delete_by_path(rec.path) is False


def test_protected_paths_returns_only_flagged(tmp_path: Path) -> None:
    idx = SegmentIndex(tmp_path / "dashcam.db")
    idx.insert(_record(path="front/a.mp4"))
    idx.insert(_record(path="front/b.mp4", protected=True))
    idx.insert(_record(path="front/c.mp4"))
    idx.set_protected("front/c.mp4", True)
    assert idx.protected_paths() == {"front/b.mp4", "front/c.mp4"}


def test_index_persists_across_reopens(tmp_path: Path) -> None:
    db_path = tmp_path / "dashcam.db"
    idx1 = SegmentIndex(db_path)
    rec = _record()
    idx1.insert(rec)

    idx2 = SegmentIndex(db_path)  # reopen
    fetched = idx2.get_by_path(rec.path)
    assert fetched is not None
    assert fetched.camera_id == "front"
