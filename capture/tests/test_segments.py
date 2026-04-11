from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from oak_dashcam_capture.camera import EncodedFrame
from oak_dashcam_capture.mock import MockCamera
from oak_dashcam_capture.segments import SegmentWriter
from oak_dashcam_capture.sinks import RawBitstreamSink
from oak_dashcam_shared.config import Codec
from oak_dashcam_shared.segment_index import SegmentIndex


class _ScriptedCamera:
    """Camera that yields a fixed list of frames with no real-time delay."""

    def __init__(self, camera_id: str, script: list[EncodedFrame]) -> None:
        self._camera_id = camera_id
        self._script = script

    @property
    def camera_id(self) -> str:
        return self._camera_id

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def frames(self) -> AsyncIterator[EncodedFrame]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[EncodedFrame]:
        for frame in self._script:
            yield frame


def _fake_clock(start: datetime, step_s: int) -> callable[[], datetime]:  # type: ignore[valid-type]
    state = {"now": start}

    def _tick() -> datetime:
        value = state["now"]
        state["now"] = value + timedelta(seconds=step_s)
        return value

    return _tick


def _frame(pts_us: int, *, keyframe: bool, size: int = 16) -> EncodedFrame:
    return EncodedFrame(data=bytes(size), pts_us=pts_us, keyframe=keyframe)


async def test_segment_writer_skips_leading_non_keyframes(tmp_path: Path) -> None:
    script = [
        _frame(0, keyframe=False),
        _frame(100, keyframe=False),
        _frame(200, keyframe=True),
        _frame(300, keyframe=False),
    ]
    cam = _ScriptedCamera("front", script)
    writer = SegmentWriter(
        cam, tmp_path, segment_seconds=1, sink=RawBitstreamSink(Codec.H265), codec=Codec.H265
    )

    await writer.run()

    segments = list(tmp_path.rglob("*.h265"))
    assert len(segments) == 1
    # First two non-keyframes were dropped → file holds the last two frames only.
    assert segments[0].stat().st_size == 32


async def test_segment_writer_rotates_on_keyframe_after_interval(tmp_path: Path) -> None:
    script = [
        _frame(0, keyframe=True),  # segment 1 opens
        _frame(500_000, keyframe=False),
        _frame(1_200_000, keyframe=False),  # past 1s, but not keyframe — no rotate
        _frame(1_500_000, keyframe=True),  # past 1s AND keyframe → rotate to seg 2
        _frame(2_000_000, keyframe=False),
        _frame(3_000_000, keyframe=True),  # past another 1s → seg 3
    ]
    cam = _ScriptedCamera("rear", script)
    writer = SegmentWriter(
        cam,
        tmp_path,
        segment_seconds=1,
        sink=RawBitstreamSink(Codec.H265),
        codec=Codec.H265,
        clock=_fake_clock(datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC), step_s=1),
    )

    await writer.run()

    segments = sorted(tmp_path.rglob("*.h265"))
    assert len(segments) == 3
    # Frame distribution: seg1=3, seg2=2, seg3=1 → sizes 48, 32, 16 bytes.
    assert [s.stat().st_size for s in segments] == [48, 32, 16]


async def test_segment_writer_path_layout(tmp_path: Path) -> None:
    script = [_frame(0, keyframe=True)]
    cam = _ScriptedCamera("cabin", script)
    writer = SegmentWriter(
        cam,
        tmp_path,
        segment_seconds=60,
        sink=RawBitstreamSink(Codec.H265),
        codec=Codec.H265,
        clock=_fake_clock(datetime(2026, 4, 10, 9, 15, 30, tzinfo=UTC), step_s=60),
    )

    await writer.run()

    expected = tmp_path / "cabin" / "2026-04-10" / "09-15-30.h265"
    assert expected.exists()


async def test_segment_writer_sink_extension_drives_filename(tmp_path: Path) -> None:
    script = [_frame(0, keyframe=True)]
    cam = _ScriptedCamera("front", script)
    writer = SegmentWriter(
        cam, tmp_path, segment_seconds=60, sink=RawBitstreamSink(Codec.H264), codec=Codec.H264
    )

    await writer.run()

    assert list(tmp_path.rglob("*.h264"))
    assert not list(tmp_path.rglob("*.h265"))


async def test_segment_writer_closes_final_segment_on_stop(tmp_path: Path) -> None:
    cam = MockCamera(camera_id="front", fps=60, keyframe_interval=30)
    writer = SegmentWriter(
        cam, tmp_path, segment_seconds=10, sink=RawBitstreamSink(Codec.H265), codec=Codec.H265
    )

    await cam.start()
    writer_task = asyncio.create_task(writer.run())
    await asyncio.sleep(0.3)
    await cam.stop()
    await asyncio.wait_for(writer_task, timeout=1.0)

    segments = list(tmp_path.rglob("*.h265"))
    assert len(segments) == 1
    # At 60fps for ~0.3s we expect ~18 frames of 1024 bytes each — give slack.
    assert segments[0].stat().st_size > 5 * 1024


async def test_segment_writer_inserts_index_row_per_segment(tmp_path: Path) -> None:
    script = [
        _frame(0, keyframe=True),
        _frame(500_000, keyframe=False),
        _frame(1_500_000, keyframe=True),  # triggers rotation
        _frame(2_000_000, keyframe=False),
    ]
    cam = _ScriptedCamera("front", script)
    index = SegmentIndex(tmp_path / "index.db")
    writer = SegmentWriter(
        cam,
        tmp_path / "data",
        segment_seconds=1,
        sink=RawBitstreamSink(Codec.H265),
        codec=Codec.H265,
        clock=_fake_clock(datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC), step_s=1),
        index=index,
    )

    await writer.run()

    rows = index.list_by_camera("front")
    # Two segments → two rows (newest first).
    assert len(rows) == 2
    assert all(r.camera_id == "front" for r in rows)
    assert all(r.codec == "h265" for r in rows)
    # Paths are stored relative to root and begin with the camera id.
    assert all(r.path.startswith("front/") for r in rows)
    # Seg 1 covers pts 0 → 500_000 → 1_500_000 (last pts before rotate is
    # the 500_000 one; the keyframe that triggers rotation opens seg 2).
    # Durations ≥ 0 — precise values depend on the finalize timing but must
    # not be negative.
    assert all(r.duration_s >= 0 for r in rows)


async def test_segment_writer_skips_index_when_index_is_none(tmp_path: Path) -> None:
    script = [_frame(0, keyframe=True)]
    cam = _ScriptedCamera("front", script)
    writer = SegmentWriter(
        cam,
        tmp_path,
        segment_seconds=60,
        sink=RawBitstreamSink(Codec.H265),
        codec=Codec.H265,
        index=None,
    )
    # Must not raise even though index is None.
    await writer.run()
    assert list(tmp_path.rglob("*.h265"))


async def test_segment_writer_survives_index_failure(tmp_path: Path) -> None:
    class _BrokenIndex:
        def insert(self, _record: object) -> int:
            raise RuntimeError("index offline")

    script = [_frame(0, keyframe=True)]
    cam = _ScriptedCamera("front", script)
    writer = SegmentWriter(
        cam,
        tmp_path,
        segment_seconds=60,
        sink=RawBitstreamSink(Codec.H265),
        codec=Codec.H265,
        index=_BrokenIndex(),  # type: ignore[arg-type]
    )
    # A crashing index must not propagate out of run() — recording continues.
    await writer.run()
    assert list(tmp_path.rglob("*.h265"))


def test_segment_writer_rejects_invalid_segment_seconds(tmp_path: Path) -> None:
    cam = _ScriptedCamera("front", [])
    with pytest.raises(ValueError, match="segment_seconds"):
        SegmentWriter(
            cam,
            tmp_path,
            segment_seconds=0,
            sink=RawBitstreamSink(Codec.H265),
            codec=Codec.H265,
        )
