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


def test_rotate_camera_toggles_0_to_180_and_back(
    monkeypatch: pytest.MonkeyPatch,
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, store, *_ = app_env

    # Stub out the httpx call that tries to ping the capture sidecar
    # so the test runs without a real capture process.
    class _StubResponse:
        def __init__(self) -> None:
            self.status_code = 200

    class _StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _StubAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, url: str) -> _StubResponse:
            return _StubResponse()

    from oak_dashcam_webapp import app as app_module

    monkeypatch.setattr(app_module.httpx, "AsyncClient", _StubAsyncClient)

    # Seed camera starts at 0° (default).
    cam_before = store.get("front")
    assert cam_before is not None
    assert cam_before.rotation_degrees == 0

    # First rotate → 180°.
    resp1 = client.post("/api/cameras/front/rotate")
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["camera"]["rotation_degrees"] == 180
    assert body1["restart_required"] is True
    assert store.get("front").rotation_degrees == 180  # type: ignore[union-attr]

    # Second rotate → back to 0°.
    resp2 = client.post("/api/cameras/front/rotate")
    assert resp2.status_code == 200
    assert resp2.json()["camera"]["rotation_degrees"] == 0
    assert store.get("front").rotation_degrees == 0  # type: ignore[union-attr]


def test_rotate_unknown_camera_is_404(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, *_ = app_env
    assert client.post("/api/cameras/nope/rotate").status_code == 404


def test_rotate_tolerates_sidecar_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    """Rotation must persist even if the capture sidecar is down.

    The sidecar reset is a courtesy kick — the supervisor will pick
    up the new rotation on the next boot regardless. A 503 from the
    sidecar shouldn't roll the DB update back.
    """
    client, store, *_ = app_env

    class _FailingAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FailingAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, url: str) -> object:
            raise httpx.RequestError("sidecar offline")

    import httpx
    from oak_dashcam_webapp import app as app_module

    monkeypatch.setattr(app_module.httpx, "AsyncClient", _FailingAsyncClient)

    resp = client.post("/api/cameras/front/rotate")
    # Still 200 — the rotation was persisted before the failed kick.
    assert resp.status_code == 200
    assert store.get("front").rotation_degrees == 180  # type: ignore[union-attr]


def test_reset_camera_endpoint_proxies_to_capture(
    monkeypatch: pytest.MonkeyPatch,
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, *_ = app_env

    # Stub out httpx.AsyncClient so the test doesn't try to reach
    # http://capture:8081 (which isn't running in tests).
    calls: list[str] = []

    class _StubResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.content = b'{"status":"resetting","camera_id":"front"}'
            self.headers = {"content-type": "application/json"}

    class _StubAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _StubAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, url: str) -> _StubResponse:
            calls.append(url)
            return _StubResponse()

    from oak_dashcam_webapp import app as app_module

    monkeypatch.setattr(app_module.httpx, "AsyncClient", _StubAsyncClient)

    resp = client.post("/api/cameras/front/reset")
    assert resp.status_code == 200
    assert resp.json() == {"status": "resetting", "camera_id": "front"}
    assert len(calls) == 1
    assert calls[0].endswith("/live/front/reset")


def test_list_segments_before_query_param_pages(
    app_env: tuple[TestClient, CameraStore, SegmentIndex, Path],
) -> None:
    client, _store, index, storage_root = app_env
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    from datetime import timedelta

    for i in range(5):
        rel = _make_segment(storage_root, "front", f"{i:02}.mp4", b"x")
        index.insert(
            SegmentRecord(
                camera_id="front",
                path=rel,
                started_at=base + timedelta(minutes=i),
                duration_s=60.0,
                size_bytes=1,
                codec="h265",
            )
        )

    first = client.get("/api/segments?limit=2").json()
    assert len(first) == 2
    # Newest first: minutes 4 then 3.
    assert first[0]["started_at"].startswith("2026-04-10T12:04:")

    # The ISO timestamp contains a `+` for the UTC offset which becomes a
    # literal space once URL-decoded, so we need httpx `params=` to do the
    # encoding for us rather than string-interpolating into the URL.
    oldest_on_first_page = first[-1]["started_at"]
    older = client.get(
        "/api/segments",
        params={"limit": 10, "before": oldest_on_first_page},
    ).json()
    assert [r["started_at"][:19] for r in older] == [
        "2026-04-10T12:02:00",
        "2026-04-10T12:01:00",
        "2026-04-10T12:00:00",
    ]


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
