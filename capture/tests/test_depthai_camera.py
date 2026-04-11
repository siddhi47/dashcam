"""Tests for the hardware-independent parts of DepthAICamera.

The pipeline-construction code path requires a real OAK device and is not
exercised here; it gets its first test run on the Pi. What we CAN test
without hardware is device selection (`_find_device`) and packet conversion
(`_packet_to_frame`) — the two places the integration is most likely to
drift from the DepthAI API.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import depthai as dai
import numpy as np
import pytest
from oak_dashcam_capture.camera import EncodedFrame
from oak_dashcam_capture.depthai_camera import (
    DepthAIDeviceNotFoundError,
    _find_device,
    _packet_to_frame,
)


class _FakeDeviceInfo:
    """Duck-type for dai.DeviceInfo that only exposes `deviceId`."""

    def __init__(self, device_id: str) -> None:
        self.deviceId = device_id


class _FakePacket:
    """Duck-type for dai.EncodedFrame used by `_packet_to_frame`."""

    def __init__(
        self,
        *,
        timestamp: timedelta,
        data: bytes,
        frame_type: Any,
    ) -> None:
        self._timestamp = timestamp
        self._data = np.frombuffer(data, dtype=np.uint8)
        self._frame_type = frame_type

    def getTimestampDevice(self) -> timedelta:
        return self._timestamp

    def getData(self) -> np.ndarray:
        return self._data

    def getFrameType(self) -> Any:
        return self._frame_type


def test_find_device_auto_picks_first() -> None:
    devices = [_FakeDeviceInfo("AAAA"), _FakeDeviceInfo("BBBB")]
    assert _find_device("auto", devices=devices).deviceId == "AAAA"  # type: ignore[arg-type]


def test_find_device_matches_exact_mxid() -> None:
    devices = [_FakeDeviceInfo("AAAA"), _FakeDeviceInfo("BBBB")]
    assert _find_device("BBBB", devices=devices).deviceId == "BBBB"  # type: ignore[arg-type]


def test_find_device_raises_on_empty_list() -> None:
    with pytest.raises(DepthAIDeviceNotFoundError, match="no DepthAI devices"):
        _find_device("auto", devices=[])


def test_find_device_raises_on_unknown_mxid() -> None:
    devices = [_FakeDeviceInfo("AAAA")]
    with pytest.raises(DepthAIDeviceNotFoundError, match="no DepthAI device with MxID"):
        _find_device("ZZZZ", devices=devices)  # type: ignore[arg-type]


def test_packet_to_frame_first_packet_sets_start_timestamp() -> None:
    packet = _FakePacket(
        timestamp=timedelta(seconds=12, microseconds=345_000),
        data=b"abcdef",
        frame_type=dai.EncodedFrame.FrameType.I,
    )
    frame, start_ts = _packet_to_frame(packet, start_ts_us=None)

    assert isinstance(frame, EncodedFrame)
    assert frame.pts_us == 0
    assert frame.keyframe is True
    assert frame.data == b"abcdef"
    assert start_ts == 12_345_000


def test_packet_to_frame_subsequent_packet_uses_relative_pts() -> None:
    first = _FakePacket(
        timestamp=timedelta(seconds=12, microseconds=345_000),
        data=b"\x00" * 4,
        frame_type=dai.EncodedFrame.FrameType.I,
    )
    _, start_ts = _packet_to_frame(first, start_ts_us=None)

    second = _FakePacket(
        timestamp=timedelta(seconds=12, microseconds=845_000),
        data=b"\x01" * 4,
        frame_type=dai.EncodedFrame.FrameType.P,
    )
    frame, out_start_ts = _packet_to_frame(second, start_ts_us=start_ts)

    assert frame.pts_us == 500_000
    assert frame.keyframe is False
    assert out_start_ts == start_ts  # preserved across calls


def test_packet_to_frame_handles_raw_bytes_payload() -> None:
    class _BytesPacket:
        def getTimestampDevice(self) -> timedelta:
            return timedelta(0)

        def getData(self) -> bytes:
            return b"hello"

        def getFrameType(self) -> Any:
            return dai.EncodedFrame.FrameType.I

    frame, _ = _packet_to_frame(_BytesPacket(), start_ts_us=None)
    assert frame.data == b"hello"


def test_packet_to_frame_non_keyframe_types() -> None:
    for ft in (dai.EncodedFrame.FrameType.P, dai.EncodedFrame.FrameType.B):
        packet = _FakePacket(
            timestamp=timedelta(0),
            data=b"\x00",
            frame_type=ft,
        )
        frame, _ = _packet_to_frame(packet, start_ts_us=0)
        assert frame.keyframe is False


# ---------------------------------------------------------------------------
# Preview subscriber fan-out
#
# The dual-output pipeline itself can't be tested without real DepthAI
# hardware (it takes a `dai.Device` to build a pipeline), but the
# subscriber-registry machinery is pure asyncio + a dict and can be
# exercised in isolation by directly driving `_broadcast_preview` and
# `subscribe_preview`.
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402

from oak_dashcam_capture.depthai_camera import DepthAICamera  # noqa: E402
from oak_dashcam_shared.config import CameraConfig, CameraRole  # noqa: E402


def _make_camera() -> DepthAICamera:
    return DepthAICamera(
        CameraConfig(id="front", role=CameraRole.FRONT),
    )


async def test_subscribe_preview_yields_broadcast_frames() -> None:
    cam = _make_camera()
    cam._loop = asyncio.get_running_loop()

    received: list[bytes] = []

    async def consume() -> None:
        async for jpeg in cam.subscribe_preview():
            received.append(jpeg)
            if len(received) >= 3:
                return

    task = asyncio.create_task(consume())
    # Give the subscriber a chance to register before broadcasting.
    await asyncio.sleep(0.01)
    cam._broadcast_preview(b"frame-1")
    cam._broadcast_preview(b"frame-2")
    cam._broadcast_preview(b"frame-3")

    await asyncio.wait_for(task, timeout=1.0)
    assert received == [b"frame-1", b"frame-2", b"frame-3"]


async def test_subscribe_preview_fans_out_to_multiple_consumers() -> None:
    cam = _make_camera()
    cam._loop = asyncio.get_running_loop()

    async def collect(limit: int) -> list[bytes]:
        out: list[bytes] = []
        async for jpeg in cam.subscribe_preview():
            out.append(jpeg)
            if len(out) >= limit:
                return out
        return out

    t1 = asyncio.create_task(collect(2))
    t2 = asyncio.create_task(collect(2))
    await asyncio.sleep(0.01)

    cam._broadcast_preview(b"a")
    cam._broadcast_preview(b"b")

    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
    # Both subscribers see every frame.
    assert r1 == [b"a", b"b"]
    assert r2 == [b"a", b"b"]


async def test_subscribe_preview_removes_subscriber_on_exit() -> None:
    cam = _make_camera()
    cam._loop = asyncio.get_running_loop()

    # Drive the async generator directly so we can guarantee that
    # `.aclose()` runs the `finally` block before we assert on the
    # subscriber set — letting the generator fall out of scope relies
    # on garbage collection, which isn't deterministic.
    assert len(cam._preview_subscribers) == 0
    gen = cam.subscribe_preview()
    # Prime the generator so it adds itself to the subscriber set.
    get_first = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.01)
    assert len(cam._preview_subscribers) == 1

    cam._broadcast_preview(b"x")
    first = await asyncio.wait_for(get_first, timeout=1.0)
    assert first == b"x"

    # Explicitly close the generator — runs the `finally` cleanup.
    await gen.aclose()
    assert len(cam._preview_subscribers) == 0


async def test_broadcast_preview_drops_frames_for_slow_consumer() -> None:
    cam = _make_camera()
    cam._loop = asyncio.get_running_loop()

    # Subscribe but never consume — the subscriber's queue fills up.
    from oak_dashcam_capture.depthai_camera import _PREVIEW_SUBSCRIBER_QUEUE

    slow_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_PREVIEW_SUBSCRIBER_QUEUE)
    cam._preview_subscribers.add(slow_queue)

    # Broadcast more frames than the queue can hold; extras must be
    # silently dropped rather than raising.
    for i in range(_PREVIEW_SUBSCRIBER_QUEUE + 5):
        cam._broadcast_preview(f"frame-{i}".encode())

    # Drain so the `call_soon_threadsafe` callbacks actually fire.
    await asyncio.sleep(0.01)

    # Queue is at most full; extras were dropped.
    assert slow_queue.qsize() == _PREVIEW_SUBSCRIBER_QUEUE
    cam._preview_subscribers.discard(slow_queue)
