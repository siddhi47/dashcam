from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from oak_dashcam_capture.camera import Camera

log = logging.getLogger(__name__)


class CapturePipeline(Protocol):
    """Anything with an `async run()` that consumes frames from a camera.

    `SegmentWriter` satisfies this structurally. Defined as a Protocol so the
    supervisor has no import cycle with `segments.py` and so tests can inject
    trivial stand-ins without building real writers.
    """

    async def run(self) -> None: ...


class CameraSupervisor:
    """Runs a (camera, pipeline) pair with restart-on-crash semantics.

    Any exception raised by the camera or the pipeline is caught and logged,
    the camera is stopped cleanly, and the pipeline is restarted after an
    exponential backoff (capped at `max_restart_delay_s`). This is how the
    capture service guarantees that one camera dropout cannot affect the
    others — each camera runs inside its own supervisor task.

    The backoff resets to the initial delay after any iteration that ran for
    at least `stability_threshold_s` seconds, so a camera that runs fine for
    an hour and then drops out doesn't inherit a maximum-length backoff from
    some crash that happened long ago.
    """

    def __init__(
        self,
        camera: Camera,
        pipeline: CapturePipeline,
        *,
        initial_restart_delay_s: float = 5.0,
        max_restart_delay_s: float = 60.0,
        stability_threshold_s: float = 60.0,
    ) -> None:
        if initial_restart_delay_s <= 0:
            raise ValueError("initial_restart_delay_s must be positive")
        if max_restart_delay_s < initial_restart_delay_s:
            raise ValueError("max_restart_delay_s must be >= initial_restart_delay_s")
        self._camera = camera
        self._pipeline = pipeline
        self._initial_delay = initial_restart_delay_s
        self._max_delay = max_restart_delay_s
        self._stability_threshold = stability_threshold_s
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        delay = self._initial_delay

        while not self._stop_event.is_set():
            iteration_start = loop.time()
            try:
                await self._camera.start()
                await self._pipeline.run()
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await self._camera.stop()
                raise
            except Exception:
                log.exception(
                    "camera %s pipeline crashed",
                    self._camera.camera_id,
                )

            with contextlib.suppress(Exception):
                await self._camera.stop()

            if self._stop_event.is_set():
                return

            if (loop.time() - iteration_start) >= self._stability_threshold:
                delay = self._initial_delay

            log.info(
                "restarting camera %s in %.2fs",
                self._camera.camera_id,
                delay,
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except TimeoutError:
                pass

            delay = min(delay * 2, self._max_delay)

    async def stop(self) -> None:
        self._stop_event.set()
        with contextlib.suppress(Exception):
            await self._camera.stop()
