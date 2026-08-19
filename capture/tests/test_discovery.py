"""Tests for the discovery module.

The DepthAI-dependent code paths (opening devices, MJPEG streaming) are
not exercised here — they require real OAK hardware and get their first
test run on the Pi. What we CAN test without hardware:

* the `_mjpeg_chunk` framing helper
* the FastAPI app shape + healthz endpoint
* the `/discovery/cameras` endpoint returning the `DiscoveryService.list_cameras`
  output (with a stub service injected)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oak_dashcam_capture.discovery import (
    DiscoveredCamera,
    DiscoveryService,
    _mjpeg_chunk,
    create_discovery_app,
)
from oak_dashcam_shared import CameraStore
from oak_dashcam_shared.config import CameraConfig, CameraRole
from oak_dashcam_shared.segment_index import SegmentRecord  # noqa: F401

# ---------------------------------------------------------------------------
# _mjpeg_chunk
# ---------------------------------------------------------------------------


def test_mjpeg_chunk_framing() -> None:
    payload = b"\xff\xd8\xff\xe0\x00\x10JFIF"  # fake JPEG header
    chunk = _mjpeg_chunk(payload)
    assert chunk.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg\r\n" in chunk
    assert f"Content-Length: {len(payload)}\r\n\r\n".encode() in chunk
    assert chunk.endswith(payload + b"\r\n")


def test_mjpeg_chunk_empty_payload() -> None:
    chunk = _mjpeg_chunk(b"")
    assert b"Content-Length: 0\r\n\r\n\r\n" in chunk


# ---------------------------------------------------------------------------
# FastAPI app shape + /discovery/cameras behavior with a stub service
# ---------------------------------------------------------------------------


class _FakeCamera:
    """Drop-in stand-in for DepthAICamera in tests.

    Only implements what `create_discovery_app` actually calls on a
    camera from the registry: currently, just `stop()`. Records the
    call count so tests can assert the endpoint actually triggered
    the reset path.
    """

    def __init__(self) -> None:
        self.stop_count = 0

    async def stop(self) -> None:
        self.stop_count += 1

    def latest_detections(self) -> dict[str, object]:
        return {
            "enabled": True,
            "ts": 1234.5,
            "detections": [
                {
                    "label": 0,
                    "label_name": "car",
                    "confidence": 0.9,
                    "bbox": [0.1, 0.2, 0.3, 0.4],
                }
            ],
        }


class _StubDiscoveryService:
    """Minimal stand-in that doesn't touch DepthAI.

    Implements the surface `create_discovery_app` exercises:
    `list_cameras`, `stream_preview`, `get_camera`, and
    `stream_live_preview`. All of them return canned data so the
    endpoints can be smoke-tested without any hardware.
    """

    def __init__(
        self,
        cameras: list[DiscoveredCamera],
        live_camera_ids: set[str] | None = None,
    ) -> None:
        self._cameras = cameras
        # Map camera_id -> _FakeCamera so the reset endpoint can call
        # `.stop()` on a real object.
        self.live_cameras: dict[str, _FakeCamera] = {
            cid: _FakeCamera() for cid in (live_camera_ids or set())
        }

    async def list_cameras(self) -> list[DiscoveredCamera]:
        return list(self._cameras)

    async def stream_preview(self) -> AsyncIterator[bytes]:
        # One "frame" then done — used only by the streaming-response smoke
        # test below. This never opens a real device.
        yield _mjpeg_chunk(b"\xff\xd8test-jpeg\xff\xd9")

    def get_camera(self, camera_id: str) -> _FakeCamera | None:
        return self.live_cameras.get(camera_id)

    async def stream_live_preview(self, camera_id: str) -> AsyncIterator[bytes]:
        yield _mjpeg_chunk(f"live:{camera_id}".encode())


@pytest.fixture
def stub_app() -> TestClient:
    stub = _StubDiscoveryService(
        cameras=[
            DiscoveredCamera(mxid="18443010AAAA", assigned=False),
            DiscoveredCamera(mxid="18443010BBBB", assigned=True),
        ],
        live_camera_ids={"front"},  # only `front` is "running"
    )
    # The production create_discovery_app takes a DiscoveryService, but we
    # only use a subset of its methods; the stub duck-types them.
    app = create_discovery_app(stub)  # type: ignore[arg-type]
    return TestClient(app)


def test_healthz(stub_app: TestClient) -> None:
    resp = stub_app.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_discovery_cameras_returns_stub_list(stub_app: TestClient) -> None:
    resp = stub_app.get("/discovery/cameras")
    assert resp.status_code == 200
    body = resp.json()
    assert "cameras" in body
    assert len(body["cameras"]) == 2
    assert body["cameras"][0]["mxid"] == "18443010AAAA"
    assert body["cameras"][0]["assigned"] is False
    assert body["cameras"][1]["assigned"] is True


def test_preview_endpoint_streams_mjpeg(stub_app: TestClient) -> None:
    with stub_app.stream("GET", "/discovery/preview.mjpeg") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("multipart/x-mixed-replace")
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
    assert b"--frame" in body
    assert b"image/jpeg" in body


def test_live_preview_endpoint_streams_mjpeg_for_registered_camera(
    stub_app: TestClient,
) -> None:
    with stub_app.stream("GET", "/live/front/preview.mjpeg") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("multipart/x-mixed-replace")
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
    assert b"--frame" in body
    assert b"live:front" in body


def test_live_preview_endpoint_404_for_unknown_camera(stub_app: TestClient) -> None:
    resp = stub_app.get("/live/nope/preview.mjpeg")
    assert resp.status_code == 404


def test_live_detections_endpoint_returns_snapshot(stub_app: TestClient) -> None:
    resp = stub_app.get("/live/front/detections")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["detections"][0]["label_name"] == "car"
    assert body["detections"][0]["bbox"] == [0.1, 0.2, 0.3, 0.4]


def test_live_detections_endpoint_404_for_unknown_camera(stub_app: TestClient) -> None:
    resp = stub_app.get("/live/nope/detections")
    assert resp.status_code == 404


def test_reset_endpoint_calls_camera_stop() -> None:
    # Need direct access to the stub to assert on the fake camera's
    # stop_count, so we build a fresh app rather than using the
    # shared fixture.
    stub = _StubDiscoveryService(cameras=[], live_camera_ids={"front"})
    client = TestClient(create_discovery_app(stub))  # type: ignore[arg-type]

    resp = client.post("/live/front/reset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resetting"
    assert body["camera_id"] == "front"
    assert stub.live_cameras["front"].stop_count == 1


def test_reset_endpoint_404_for_unknown_camera(stub_app: TestClient) -> None:
    resp = stub_app.post("/live/nope/reset")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DiscoveryService.list_cameras assigned-filter
# ---------------------------------------------------------------------------


def test_list_cameras_marks_assigned_mxids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CameraStore(tmp_path / "dashcam.db")
    store.insert(
        CameraConfig(
            id="front",
            mxid="18443010PINNED",
            role=CameraRole.FRONT,
        )
    )

    service = DiscoveryService(store)

    # Patch the sysfs reader so no filesystem access is needed.
    from oak_dashcam_capture import discovery as discovery_mod

    monkeypatch.setattr(
        discovery_mod,
        "_list_physical_oak_mxids",
        lambda: ["18443010PINNED", "18443010NEW"],
    )

    import asyncio

    result = asyncio.run(service.list_cameras())
    by_mxid = {c.mxid: c for c in result}
    # PINNED is in the store → assigned=True
    assert by_mxid["18443010PINNED"].assigned is True
    # NEW is free → assigned=False
    assert by_mxid["18443010NEW"].assigned is False


def test_list_cameras_ignores_auto_placeholder_in_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A camera pinned to mxid="auto" must NOT cause discovery to think a
    # real MxID is already assigned — "auto" is a placeholder, not a
    # specific device id.
    store = CameraStore(tmp_path / "dashcam.db")
    store.insert(
        CameraConfig(
            id="front",
            mxid="auto",
            role=CameraRole.FRONT,
        )
    )

    service = DiscoveryService(store)
    from oak_dashcam_capture import discovery as discovery_mod

    monkeypatch.setattr(
        discovery_mod,
        "_list_physical_oak_mxids",
        lambda: ["18443010REAL"],
    )

    import asyncio

    result = asyncio.run(service.list_cameras())
    assert result[0].assigned is False


def test_list_physical_oak_mxids_reads_sysfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit test for the sysfs walker with a fake /sys tree."""
    fake_sysfs = tmp_path / "sys"
    fake_sysfs.mkdir()

    def _make_device(name: str, vendor: str, product: str, serial: str) -> None:
        d = fake_sysfs / name
        d.mkdir()
        (d / "idVendor").write_text(vendor + "\n")
        (d / "idProduct").write_text(product + "\n")
        (d / "serial").write_text(serial + "\n")

    # 1 booted OAK, 1 bootloader OAK (skipped), 1 unrelated USB device.
    _make_device("1-1.1", "03e7", "f63b", "18443010REAL1")
    _make_device("1-1.2", "03e7", "2485", "03e72485")  # bootloader, skipped
    _make_device("1-1.3", "1234", "5678", "other")  # not an OAK
    _make_device("1-1.4", "03e7", "f63b", "18443010REAL2")

    from oak_dashcam_capture import discovery as discovery_mod

    monkeypatch.setattr(discovery_mod, "_SYSFS_USB_ROOT", fake_sysfs)

    result = discovery_mod._list_physical_oak_mxids()
    assert set(result) == {"18443010REAL1", "18443010REAL2"}


def test_list_physical_oak_mxids_handles_missing_sysfs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from oak_dashcam_capture import discovery as discovery_mod

    monkeypatch.setattr(discovery_mod, "_SYSFS_USB_ROOT", tmp_path / "does-not-exist")
    assert discovery_mod._list_physical_oak_mxids() == []


# Suppress pytest's "never-called" warning on SegmentRecord (imported only
# so pytest collects this file if related imports change).
_ = datetime.now(tz=UTC)
