from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from oak_dashcam_shared.config import Codec
from oak_dashcam_shared.segment_index import SegmentIndex, SegmentRecord

from oak_dashcam_capture.camera import Camera
from oak_dashcam_capture.sinks import SegmentSink

log = logging.getLogger(__name__)


Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(tz=UTC)


class SegmentWriter:
    """Consumes frames from a `Camera` and writes them to rotating segment files.

    Segments rotate when `segment_seconds` have elapsed **and** the next frame
    is a keyframe — every segment therefore starts with an IDR and is
    independently decodable. Segments are placed under
    `{root}/{camera_id}/{YYYY-MM-DD}/{HH-MM-SS}.{sink.extension}`.

    The on-disk file format is owned by the `SegmentSink`: pass a
    `RawBitstreamSink` for diagnostic raw-bitstream output, or a
    `FfmpegMp4Sink` for real MP4 recordings. The writer itself doesn't know
    or care which.

    An optional `SegmentIndex` records one row per finalized segment once
    it's on disk. Passing `None` is fine — the writer just skips the index
    write. Tests that don't care about the index leave it as the default.
    """

    def __init__(
        self,
        camera: Camera,
        root: Path,
        segment_seconds: int,
        sink: SegmentSink,
        *,
        codec: Codec,
        clock: Clock | None = None,
        index: SegmentIndex | None = None,
    ) -> None:
        if segment_seconds <= 0:
            raise ValueError("segment_seconds must be positive")
        self._camera = camera
        self._root = root
        self._segment_seconds = segment_seconds
        self._sink = sink
        self._codec = codec
        self._clock: Clock = clock or _default_clock
        self._index = index

    async def run(self) -> None:
        current_path: Path | None = None
        started_at: datetime | None = None
        segment_start_us: int | None = None
        last_pts_us: int | None = None

        try:
            async for frame in self._camera.frames():
                if current_path is None:
                    # Wait for a keyframe before opening the first segment so
                    # every file on disk begins with an IDR.
                    if not frame.keyframe:
                        continue
                    current_path = self._next_path()
                    started_at = self._clock()
                    self._sink.open(current_path)
                    segment_start_us = frame.pts_us
                elif (
                    segment_start_us is not None
                    and frame.pts_us - segment_start_us >= self._segment_seconds * 1_000_000
                    and frame.keyframe
                ):
                    self._finalize(current_path, started_at, segment_start_us, last_pts_us)
                    current_path = self._next_path()
                    started_at = self._clock()
                    self._sink.open(current_path)
                    segment_start_us = frame.pts_us

                self._sink.write(frame)
                last_pts_us = frame.pts_us
        finally:
            if current_path is not None:
                self._finalize(current_path, started_at, segment_start_us, last_pts_us)

    def _finalize(
        self,
        path: Path,
        started_at: datetime | None,
        first_pts_us: int | None,
        last_pts_us: int | None,
    ) -> None:
        self._sink.close()
        if self._index is None:
            return
        if started_at is None or first_pts_us is None or last_pts_us is None:
            return
        if not path.exists():
            # Sink failed to finalize the file (e.g. ffmpeg errored and left
            # a .mp4.tmp behind). Don't index phantom rows.
            log.warning("segment %s not on disk after close; skipping index insert", path)
            return
        try:
            relative = str(path.relative_to(self._root))
        except ValueError:
            log.warning(
                "segment path %s not under root %s; skipping index insert", path, self._root
            )
            return
        duration_s = max(0.0, (last_pts_us - first_pts_us) / 1_000_000.0)
        record = SegmentRecord(
            camera_id=self._camera.camera_id,
            path=relative,
            started_at=started_at,
            duration_s=duration_s,
            size_bytes=path.stat().st_size,
            codec=self._codec.value,
            protected=False,
        )
        try:
            self._index.insert(record)
        except Exception:
            log.exception("failed to insert segment record for %s", path)

    def _next_path(self) -> Path:
        now = self._clock()
        return (
            self._root
            / self._camera.camera_id
            / now.strftime("%Y-%m-%d")
            / f"{now.strftime('%H-%M-%S')}.{self._sink.extension}"
        )
