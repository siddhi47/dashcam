from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from oak_dashcam_capture.retention import RetentionManager
from oak_dashcam_shared.segment_index import SegmentIndex, SegmentRecord


def _write_segment(root: Path, camera: str, name: str, *, size: int, mtime: float) -> Path:
    """Create a fake segment file with controlled size + mtime."""
    path = root / camera / "2026-04-10" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_retention_rejects_zero_max_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        RetentionManager(tmp_path, max_bytes=0)


def test_retention_rejects_zero_scan_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scan_interval_s"):
        RetentionManager(tmp_path, max_bytes=1000, scan_interval_s=0)


# ---------------------------------------------------------------------------
# Enforce behavior
# ---------------------------------------------------------------------------


def test_retention_noop_when_root_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    manager = RetentionManager(missing, max_bytes=1000)
    manager.enforce()  # must not raise


def test_retention_noop_when_under_limit(tmp_path: Path) -> None:
    _write_segment(tmp_path, "front", "a.mp4", size=100, mtime=1.0)
    _write_segment(tmp_path, "front", "b.mp4", size=100, mtime=2.0)
    manager = RetentionManager(tmp_path, max_bytes=500)
    manager.enforce()
    assert sorted(p.name for p in tmp_path.rglob("*.mp4")) == ["a.mp4", "b.mp4"]


def test_retention_deletes_oldest_when_over_limit(tmp_path: Path) -> None:
    _write_segment(tmp_path, "front", "a.mp4", size=100, mtime=1.0)  # oldest
    _write_segment(tmp_path, "front", "b.mp4", size=100, mtime=2.0)
    _write_segment(tmp_path, "front", "c.mp4", size=100, mtime=3.0)  # latest, protected
    # Limit of 250 allows exactly two files (200 bytes) — the protected one
    # plus one more. Oldest (a.mp4) must be the one deleted.
    manager = RetentionManager(tmp_path, max_bytes=250)
    manager.enforce()

    survivors = {p.name for p in tmp_path.rglob("*.mp4")}
    assert survivors == {"b.mp4", "c.mp4"}


def test_retention_keeps_deleting_until_under_limit(tmp_path: Path) -> None:
    for i in range(5):
        _write_segment(tmp_path, "front", f"seg{i}.mp4", size=100, mtime=float(i))
    # max 150 bytes; latest protected → 1 survives (latest) + room for at most
    # one more non-latest = 200 bytes, which is over 150. So retention must
    # delete until only the latest remains (size 100 ≤ 150).
    manager = RetentionManager(tmp_path, max_bytes=150)
    manager.enforce()

    survivors = sorted(p.name for p in tmp_path.rglob("*.mp4"))
    assert survivors == ["seg4.mp4"]


def test_retention_protects_latest_per_camera_independently(tmp_path: Path) -> None:
    # Two cameras, each with one old + one new segment. Both "latest" files
    # must survive even if the total would otherwise put us under the limit
    # with just one of them protected.
    _write_segment(tmp_path, "front", "old.mp4", size=100, mtime=1.0)
    _write_segment(tmp_path, "front", "new.mp4", size=100, mtime=10.0)
    _write_segment(tmp_path, "rear", "old.mp4", size=100, mtime=2.0)
    _write_segment(tmp_path, "rear", "new.mp4", size=100, mtime=11.0)

    manager = RetentionManager(tmp_path, max_bytes=150)
    manager.enforce()

    survivors = {(p.parent.parent.name, p.name) for p in tmp_path.rglob("*.mp4")}
    assert ("front", "new.mp4") in survivors
    assert ("rear", "new.mp4") in survivors
    # Old files from both cameras should be gone.
    assert ("front", "old.mp4") not in survivors
    assert ("rear", "old.mp4") not in survivors


def test_retention_ignores_files_with_foreign_extensions(tmp_path: Path) -> None:
    _write_segment(tmp_path, "front", "a.mp4", size=100, mtime=1.0)
    foreign = tmp_path / "front" / "2026-04-10" / "notes.txt"
    foreign.write_bytes(b"x" * 10_000)  # big, but must not be touched
    os.utime(foreign, (0.5, 0.5))

    manager = RetentionManager(tmp_path, max_bytes=50)
    manager.enforce()

    assert foreign.exists(), "retention deleted a non-segment file"
    # The .mp4 is protected (latest per camera), so it stays too.
    assert (tmp_path / "front" / "2026-04-10" / "a.mp4").exists()


def test_retention_ignores_loose_files_at_storage_root(tmp_path: Path) -> None:
    # A file directly under the storage root with a segment extension should
    # still be ignored — it doesn't match our `{camera}/{date}/...` layout.
    loose = tmp_path / "orphan.mp4"
    loose.write_bytes(b"x" * 10_000)
    _write_segment(tmp_path, "front", "a.mp4", size=100, mtime=1.0)

    manager = RetentionManager(tmp_path, max_bytes=50)
    manager.enforce()
    assert loose.exists()


def test_retention_considers_all_known_extensions(tmp_path: Path) -> None:
    _write_segment(tmp_path, "front", "a.h265", size=100, mtime=1.0)  # oldest
    _write_segment(tmp_path, "front", "b.h264", size=100, mtime=2.0)
    _write_segment(tmp_path, "front", "c.mp4", size=100, mtime=3.0)  # latest, protected

    # Limit of 250 keeps two files — confirms all three extensions are seen
    # and the oldest (regardless of extension) is the one deleted.
    manager = RetentionManager(tmp_path, max_bytes=250)
    manager.enforce()

    survivors = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert "a.h265" not in survivors
    assert "b.h264" in survivors
    assert "c.mp4" in survivors


def test_retention_never_deletes_if_only_protected_files_exist(tmp_path: Path) -> None:
    # Only one file per camera → all files are "latest" → all protected.
    # Retention should log a warning but not touch anything.
    _write_segment(tmp_path, "front", "only.mp4", size=1000, mtime=1.0)
    _write_segment(tmp_path, "rear", "only.mp4", size=1000, mtime=2.0)

    manager = RetentionManager(tmp_path, max_bytes=100)
    manager.enforce()

    assert (tmp_path / "front" / "2026-04-10" / "only.mp4").exists()
    assert (tmp_path / "rear" / "2026-04-10" / "only.mp4").exists()


# ---------------------------------------------------------------------------
# SegmentIndex integration
# ---------------------------------------------------------------------------


def _index_record(rel_path: str, *, protected: bool = False) -> SegmentRecord:
    return SegmentRecord(
        camera_id=rel_path.split("/", 1)[0],
        path=rel_path,
        started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
        duration_s=60.0,
        size_bytes=100,
        codec="mp4",
        protected=protected,
    )


def test_retention_skips_incident_protected_segments_from_index(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_segment(data_root, "front", "old-incident.mp4", size=100, mtime=1.0)
    _write_segment(data_root, "front", "old-regular.mp4", size=100, mtime=2.0)
    _write_segment(data_root, "front", "newer.mp4", size=100, mtime=3.0)
    _write_segment(data_root, "front", "newest.mp4", size=100, mtime=4.0)

    index = SegmentIndex(tmp_path / "dashcam.db")
    index.insert(_index_record("front/2026-04-10/old-incident.mp4", protected=True))
    index.insert(_index_record("front/2026-04-10/old-regular.mp4"))
    index.insert(_index_record("front/2026-04-10/newer.mp4"))
    index.insert(_index_record("front/2026-04-10/newest.mp4"))

    # Limit 250 bytes — need to free 150 bytes from ~400 bytes total. Latest
    # (newest.mp4) is protected by the latest-per-camera rule; old-incident.mp4
    # is protected by the DB flag. Retention must delete `old-regular.mp4` and
    # `newer.mp4` (the only two non-protected files) even though incident is
    # older than both.
    manager = RetentionManager(data_root, max_bytes=250, index=index)
    manager.enforce()

    survivors = {p.name for p in data_root.rglob("*.mp4")}
    assert "old-incident.mp4" in survivors, "incident-protected file was deleted"
    assert "newest.mp4" in survivors, "latest-per-camera file was deleted"
    assert "old-regular.mp4" not in survivors
    assert "newer.mp4" not in survivors


def test_retention_deletes_index_row_when_file_deleted(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _write_segment(data_root, "front", "old.mp4", size=100, mtime=1.0)
    _write_segment(data_root, "front", "new.mp4", size=100, mtime=2.0)

    index = SegmentIndex(tmp_path / "dashcam.db")
    index.insert(_index_record("front/2026-04-10/old.mp4"))
    index.insert(_index_record("front/2026-04-10/new.mp4"))

    manager = RetentionManager(data_root, max_bytes=150, index=index)
    manager.enforce()

    # `old.mp4` deleted from disk → its row must also be gone from the index.
    assert index.get_by_path("front/2026-04-10/old.mp4") is None
    # `new.mp4` was protected (latest) → still in both.
    assert index.get_by_path("front/2026-04-10/new.mp4") is not None
    assert (data_root / "front" / "2026-04-10" / "new.mp4").exists()


def test_retention_survives_index_protected_paths_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    _write_segment(data_root, "front", "old.mp4", size=100, mtime=1.0)
    _write_segment(data_root, "front", "new.mp4", size=100, mtime=2.0)

    index = SegmentIndex(tmp_path / "dashcam.db")
    index.insert(_index_record("front/2026-04-10/old.mp4"))
    index.insert(_index_record("front/2026-04-10/new.mp4"))

    # Simulate the DB being corrupt or locked.
    def _boom() -> set[str]:
        raise RuntimeError("db offline")

    monkeypatch.setattr(index, "protected_paths", _boom)

    # Even with no information about protected clips, retention must keep
    # running and fall back to "nothing extra is protected".
    manager = RetentionManager(data_root, max_bytes=150, index=index)
    manager.enforce()

    # `old.mp4` still gets deleted (latest-per-camera rule still applies).
    assert not (data_root / "front" / "2026-04-10" / "old.mp4").exists()
    assert (data_root / "front" / "2026-04-10" / "new.mp4").exists()


def test_retention_survives_index_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    _write_segment(data_root, "front", "a.mp4", size=100, mtime=1.0)
    _write_segment(data_root, "front", "b.mp4", size=100, mtime=2.0)
    _write_segment(data_root, "front", "c.mp4", size=100, mtime=3.0)

    index = SegmentIndex(tmp_path / "dashcam.db")
    index.insert(_index_record("front/2026-04-10/a.mp4"))

    def _boom(_path: str) -> bool:
        raise RuntimeError("db offline")

    monkeypatch.setattr(index, "delete_by_path", _boom)

    manager = RetentionManager(data_root, max_bytes=150, index=index)
    # A failing `delete_by_path` must not prevent file deletion — the DB row
    # becomes orphaned but the disk is still reclaimed.
    manager.enforce()

    assert not (data_root / "front" / "2026-04-10" / "a.mp4").exists()
    assert not (data_root / "front" / "2026-04-10" / "b.mp4").exists()
    assert (data_root / "front" / "2026-04-10" / "c.mp4").exists()
