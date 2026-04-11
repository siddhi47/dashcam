from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oak_dashcam_shared import CameraStore, SegmentIndex, SegmentRecord
from oak_dashcam_shared.config import (
    CameraConfig,
    CameraRole,
    Codec,
    DashcamConfig,
    Resolution,
    StorageConfig,
)
from oak_dashcam_webapp.app import create_app


@pytest.fixture
def app_env(tmp_path: Path) -> tuple[TestClient, CameraStore, SegmentIndex, Path]:
    storage_root = tmp_path / "data"
    storage_root.mkdir(parents=True)
    config = DashcamConfig(
        storage=StorageConfig(root=storage_root, retention_gb=1, segment_seconds=5),
        cameras=[],
    )
    db = storage_root / "dashcam.db"
    store = CameraStore(db)
    index = SegmentIndex(db)
    # Seed one camera so existing endpoints have something to chew on.
    store.insert(
        CameraConfig(
            id="front",
            mxid="AAAA",
            role=CameraRole.FRONT,
            resolution=Resolution.R_1080P,
            fps=30,
            codec=Codec.H265,
            bitrate_kbps=8000,
        )
    )
    app = create_app(config, camera_store=store, segment_index=index)
    return TestClient(app), store, index, storage_root


# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------


def test_healthz(app_env: tuple[TestClient, CameraStore, SegmentIndex, Path]) -> None:
    client, *_ = app_env
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# camera CRUD
# ---------------------------------------------------------------------------


def test_list_cameras_returns_seeded(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, *_ = app_env
    resp = client.get("/api/cameras")
    assert resp.status_code == 200
    rows = resp.json()
    assert [c["id"] for c in rows] == ["front"]
    assert rows[0]["mxid"] == "AAAA"
    assert rows[0]["role"] == "front"


def test_get_camera_by_id(app_env: tuple[TestClient, CameraStore, SegmentIndex, Path]) -> None:
    client, *_ = app_env
    resp = client.get("/api/cameras/front")
    assert resp.status_code == 200
    assert resp.json()["id"] == "front"


def test_get_missing_camera_is_404(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, *_ = app_env
    assert client.get("/api/cameras/nope").status_code == 404


def test_create_camera(app_env: tuple[TestClient, CameraStore, SegmentIndex, Path]) -> None:
    client, store, *_ = app_env
    resp = client.post(
        "/api/cameras",
        json={
            "id": "rear",
            "mxid": "BBBB",
            "role": "rear",
            "fps": 30,
            "codec": "h265",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["camera"]["id"] == "rear"
    assert body["restart_required"] is True
    assert store.get("rear") is not None


def test_create_camera_duplicate_is_409(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, *_ = app_env
    resp = client.post(
        "/api/cameras",
        json={"id": "front", "mxid": "X", "role": "front"},
    )
    assert resp.status_code == 409


def test_create_camera_bad_id_is_422(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, *_ = app_env
    resp = client.post(
        "/api/cameras",
        json={"id": "not a slug!", "role": "front"},
    )
    assert resp.status_code == 422


def test_update_camera(app_env: tuple[TestClient, CameraStore, SegmentIndex, Path]) -> None:
    client, store, *_ = app_env
    resp = client.put(
        "/api/cameras/front",
        json={
            "mxid": "CHANGED",
            "role": "front",
            "resolution": "720p",
            "fps": 60,
            "codec": "h264",
            "bitrate_kbps": 4000,
        },
    )
    assert resp.status_code == 200
    updated = store.get("front")
    assert updated is not None
    assert updated.mxid == "CHANGED"
    assert updated.fps == 60
    assert updated.codec is Codec.H264


def test_update_missing_camera_is_404(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, *_ = app_env
    resp = client.put(
        "/api/cameras/nope",
        json={
            "mxid": "X",
            "role": "front",
            "resolution": "1080p",
            "fps": 30,
            "codec": "h265",
            "bitrate_kbps": 8000,
        },
    )
    assert resp.status_code == 404


def test_delete_camera(app_env: tuple[TestClient, CameraStore, SegmentIndex, Path]) -> None:
    client, store, *_ = app_env
    resp = client.delete("/api/cameras/front")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "front", "restart_required": True}
    assert store.get("front") is None


# ---------------------------------------------------------------------------
# segments list + video streaming
# ---------------------------------------------------------------------------


def _make_segment(storage_root: Path, camera: str, name: str, data: bytes) -> str:
    """Create a segment file on disk and return its path relative to root."""
    rel = f"{camera}/2026-04-10/{name}"
    abs_path = storage_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(data)
    return rel


def test_list_segments_filters_by_camera(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, _store, index, storage_root = app_env
    rel1 = _make_segment(storage_root, "front", "00.mp4", b"A" * 100)
    rel2 = _make_segment(storage_root, "rear", "00.mp4", b"B" * 100)
    for rel in (rel1, rel2):
        index.insert(
            SegmentRecord(
                camera_id=rel.split("/", 1)[0],
                path=rel,
                started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
                duration_s=60.0,
                size_bytes=100,
                codec="h265",
                protected=False,
            )
        )
    front_resp = client.get("/api/segments?camera=front")
    assert front_resp.status_code == 200
    front = front_resp.json()
    assert len(front) == 1
    assert front[0]["camera_id"] == "front"

    all_resp = client.get("/api/segments")
    assert len(all_resp.json()) == 2


def test_protect_segment_flips_flag(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, _store, index, storage_root = app_env
    rel = _make_segment(storage_root, "front", "00.mp4", b"A" * 10)
    index.insert(
        SegmentRecord(
            camera_id="front",
            path=rel,
            started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
            duration_s=1.0,
            size_bytes=10,
            codec="h265",
        )
    )
    resp = client.post(f"/api/segments/{rel}/protect?protected=true")
    assert resp.status_code == 200
    assert index.get_by_path(rel).protected is True  # type: ignore[union-attr]

    resp2 = client.post(f"/api/segments/{rel}/protect?protected=false")
    assert resp2.status_code == 200
    assert index.get_by_path(rel).protected is False  # type: ignore[union-attr]


def test_stream_segment_full_file(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, _store, index, storage_root = app_env
    payload = b"\x00\x01\x02\x03" * 50
    rel = _make_segment(storage_root, "front", "00.mp4", payload)
    index.insert(
        SegmentRecord(
            camera_id="front",
            path=rel,
            started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
            duration_s=1.0,
            size_bytes=len(payload),
            codec="h265",
        )
    )
    resp = client.get(f"/api/segments/{rel}/video")
    assert resp.status_code == 200
    assert resp.content == payload
    assert resp.headers["content-type"].startswith("video/")
    assert resp.headers["accept-ranges"] == "bytes"


def test_stream_segment_range_request(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, _store, index, storage_root = app_env
    payload = bytes(range(256))  # 256 byte pattern
    rel = _make_segment(storage_root, "front", "00.mp4", payload)
    index.insert(
        SegmentRecord(
            camera_id="front",
            path=rel,
            started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
            duration_s=1.0,
            size_bytes=len(payload),
            codec="h265",
        )
    )
    resp = client.get(
        f"/api/segments/{rel}/video",
        headers={"Range": "bytes=10-19"},
    )
    assert resp.status_code == 206
    assert resp.content == payload[10:20]
    assert resp.headers["content-range"] == f"bytes 10-19/{len(payload)}"


def test_stream_segment_invalid_range_is_416(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, _store, index, storage_root = app_env
    payload = b"X" * 100
    rel = _make_segment(storage_root, "front", "00.mp4", payload)
    index.insert(
        SegmentRecord(
            camera_id="front",
            path=rel,
            started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
            duration_s=1.0,
            size_bytes=len(payload),
            codec="h265",
        )
    )
    resp = client.get(
        f"/api/segments/{rel}/video",
        headers={"Range": "bytes=500-600"},  # past end of file
    )
    assert resp.status_code == 416


def test_stream_missing_segment_is_404(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, *_ = app_env
    resp = client.get("/api/segments/front/2026-04-10/nope.mp4/video")
    assert resp.status_code == 404


def test_stream_segment_row_without_file_is_410(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, _store, index, _storage_root = app_env
    index.insert(
        SegmentRecord(
            camera_id="front",
            path="front/2026-04-10/ghost.mp4",
            started_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC),
            duration_s=1.0,
            size_bytes=1,
            codec="h265",
        )
    )
    resp = client.get("/api/segments/front/2026-04-10/ghost.mp4/video")
    assert resp.status_code == 410
