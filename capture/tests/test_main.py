from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from oak_dashcam_capture.__main__ import (
    build_supervisors,
    resolve_auto_mxids,
    run_supervisors,
)
from oak_dashcam_capture.mock import MockCamera
from oak_dashcam_shared.config import (
    CameraConfig,
    CameraRole,
    Codec,
    DashcamConfig,
    Resolution,
    StorageConfig,
)


@pytest.fixture(autouse=True)
def _force_mock_cameras_and_raw_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests run without any OAK hardware — pin to mock mode regardless of the
    # host's DepthAI state so tests are deterministic on any machine.
    monkeypatch.setenv("OAK_DASHCAM_MOCK", "1")
    # MockCamera emits synthetic bytes that aren't valid HEVC, so a real
    # FfmpegMp4Sink would error out trying to parse them. Force the sink
    # builder to see "no ffmpeg" and fall back to RawBitstreamSink.
    import oak_dashcam_capture.__main__ as main_module

    monkeypatch.setattr(main_module.shutil, "which", lambda _name: None)


def _config(root: Path) -> DashcamConfig:
    return DashcamConfig(
        storage=StorageConfig(root=root, retention_gb=1, segment_seconds=5),
        cameras=[
            CameraConfig(
                id="front",
                role=CameraRole.FRONT,
                resolution=Resolution.R_1080P,
                fps=60,
                codec=Codec.H265,
            ),
            CameraConfig(
                id="rear",
                role=CameraRole.REAR,
                resolution=Resolution.R_1080P,
                fps=60,
                codec=Codec.H264,
            ),
        ],
    )


def test_build_supervisors_creates_one_mock_per_camera(tmp_path: Path) -> None:
    supervisors = build_supervisors(_config(tmp_path))
    assert len(supervisors) == 2
    # With OAK_DASHCAM_MOCK set, every built camera must be a MockCamera.
    assert all(isinstance(sup._camera, MockCamera) for sup in supervisors)


async def test_pipeline_produces_segments_for_every_camera(tmp_path: Path) -> None:
    supervisors = build_supervisors(_config(tmp_path))

    task = asyncio.create_task(run_supervisors(supervisors))
    await asyncio.sleep(0.3)  # ~18 frames per camera, enough to fill a segment
    await asyncio.gather(*(sup.stop() for sup in supervisors))
    await asyncio.wait_for(task, timeout=2.0)

    # Each camera must have produced at least one segment on disk under its own
    # directory, using the extension from its configured codec.
    front_segments = list((tmp_path / "front").rglob("*.h265"))
    rear_segments = list((tmp_path / "rear").rglob("*.h264"))
    assert front_segments, "front camera produced no segments"
    assert rear_segments, "rear camera produced no segments"
    assert all(p.stat().st_size > 0 for p in front_segments + rear_segments)


async def test_run_supervisors_with_empty_list_is_noop() -> None:
    await run_supervisors([])


class _FakeDeviceInfo:
    def __init__(self, device_id: str) -> None:
        self.deviceId = device_id


def _patch_available_devices(monkeypatch: pytest.MonkeyPatch, device_ids: list[str]) -> None:
    import depthai as dai

    monkeypatch.setattr(
        dai.Device,
        "getAllAvailableDevices",
        staticmethod(lambda: [_FakeDeviceInfo(i) for i in device_ids]),
    )


def _auto_cam(cam_id: str) -> CameraConfig:
    return CameraConfig(id=cam_id, role=CameraRole.FRONT)


def test_resolve_auto_mxids_distributes_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_available_devices(monkeypatch, ["AAAA", "BBBB"])
    resolved = resolve_auto_mxids([_auto_cam("front"), _auto_cam("rear")])
    assert [c.mxid for c in resolved] == ["AAAA", "BBBB"]


def test_resolve_auto_mxids_respects_pinned_mxids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_available_devices(monkeypatch, ["AAAA", "BBBB"])
    cameras = [
        _auto_cam("front"),
        CameraConfig(id="rear", role=CameraRole.REAR, mxid="AAAA"),
    ]
    resolved = resolve_auto_mxids(cameras)
    # Pinned camera keeps AAAA; auto camera gets BBBB (AAAA is already taken).
    assert {c.id: c.mxid for c in resolved} == {"front": "BBBB", "rear": "AAAA"}


def test_resolve_auto_mxids_fails_when_not_enough_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_available_devices(monkeypatch, ["AAAA"])
    with pytest.raises(RuntimeError, match="no unclaimed DepthAI device"):
        resolve_auto_mxids([_auto_cam("front"), _auto_cam("rear")])


def test_resolve_auto_mxids_fails_when_no_devices_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_available_devices(monkeypatch, [])
    with pytest.raises(RuntimeError, match="no DepthAI devices available"):
        resolve_auto_mxids([_auto_cam("front")])
