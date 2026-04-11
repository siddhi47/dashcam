from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from oak_dashcam_capture.camera import EncodedFrame


class MockCamera:
    """Camera implementation that emits synthetic frames without any hardware.

    Used for local development, CI, and tests — any code path that depends on
    `Camera` should be exercised against `MockCamera` so the capture service
    can be built and tested end-to-end without an OAK device.
    """

    def __init__(
        self,
        camera_id: str,
        fps: int = 30,
        keyframe_interval: int = 30,
        frame_size_bytes: int = 1024,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        if keyframe_interval <= 0:
            raise ValueError("keyframe_interval must be positive")
        self._camera_id = camera_id
        self._fps = fps
        self._keyframe_interval = keyframe_interval
        self._frame_size_bytes = frame_size_bytes
        self._running = False

    @property
    def camera_id(self) -> str:
        return self._camera_id

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    def frames(self) -> AsyncIterator[EncodedFrame]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[EncodedFrame]:
        period = 1.0 / self._fps
        loop = asyncio.get_running_loop()
        start = loop.time()
        next_tick = start
        counter = 0
        while self._running:
            next_tick += period
            sleep_for = next_tick - loop.time()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            pts_us = int((loop.time() - start) * 1_000_000)
            yield EncodedFrame(
                data=bytes(self._frame_size_bytes),
                pts_us=pts_us,
                keyframe=(counter % self._keyframe_interval) == 0,
            )
            counter += 1
