from __future__ import annotations

import asyncio
from itertools import pairwise

import pytest
from oak_dashcam_capture.camera import Camera, EncodedFrame
from oak_dashcam_capture.mock import MockCamera


async def _collect(cam: MockCamera, duration_s: float) -> list[EncodedFrame]:
    collected: list[EncodedFrame] = []

    async def runner() -> None:
        async for frame in cam.frames():
            collected.append(frame)

    await cam.start()
    task = asyncio.create_task(runner())
    await asyncio.sleep(duration_s)
    await cam.stop()
    await asyncio.wait_for(task, timeout=1.0)
    return collected


def test_mock_camera_satisfies_camera_protocol() -> None:
    cam = MockCamera(camera_id="front")
    assert isinstance(cam, Camera)
    assert cam.camera_id == "front"


async def test_mock_camera_emits_frames_at_configured_rate() -> None:
    cam = MockCamera(camera_id="front", fps=60, keyframe_interval=10)
    frames = await _collect(cam, duration_s=0.25)

    # 60 fps * 0.25s ≈ 15 frames; allow slack for scheduling jitter.
    assert 10 <= len(frames) <= 20, f"expected ~15 frames, got {len(frames)}"
    assert all(len(f.data) == 1024 for f in frames)


async def test_mock_camera_marks_keyframes_on_interval() -> None:
    cam = MockCamera(camera_id="front", fps=120, keyframe_interval=5)
    frames = await _collect(cam, duration_s=0.15)

    keyframe_indices = [i for i, f in enumerate(frames) if f.keyframe]
    assert keyframe_indices, "expected at least one keyframe"
    assert keyframe_indices[0] == 0, "first frame must be a keyframe"
    for prev, curr in pairwise(keyframe_indices):
        assert curr - prev == 5


async def test_mock_camera_pts_monotonic_increasing() -> None:
    cam = MockCamera(camera_id="front", fps=30)
    frames = await _collect(cam, duration_s=0.2)

    assert len(frames) >= 3
    pts_values = [f.pts_us for f in frames]
    assert pts_values == sorted(pts_values)
    assert pts_values[0] >= 0


async def test_mock_camera_stops_cleanly() -> None:
    cam = MockCamera(camera_id="front", fps=30)
    frames = await _collect(cam, duration_s=0.1)
    # After stop, iterator must have terminated — a second collect starts fresh.
    assert not cam._running  # internal check: stop() flipped the flag
    assert len(frames) >= 1


def test_mock_camera_rejects_invalid_fps() -> None:
    with pytest.raises(ValueError, match="fps"):
        MockCamera(camera_id="front", fps=0)


def test_mock_camera_rejects_invalid_keyframe_interval() -> None:
    with pytest.raises(ValueError, match="keyframe_interval"):
        MockCamera(camera_id="front", keyframe_interval=0)
