from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from oak_dashcam_capture.camera import EncodedFrame
from oak_dashcam_capture.supervisor import CameraSupervisor


class _FakeCamera:
    """Test stand-in that satisfies the `Camera` protocol.

    The camera exposes a `running` event so a fake pipeline can block until
    the camera is told to stop — matching how a real pipeline blocks inside
    `async for frame in camera.frames()` until the device shuts down.
    """

    def __init__(self, camera_id: str = "front") -> None:
        self._camera_id = camera_id
        self.start_count = 0
        self.stop_count = 0
        self.running = asyncio.Event()

    @property
    def camera_id(self) -> str:
        return self._camera_id

    async def start(self) -> None:
        self.start_count += 1
        self.running.set()

    async def stop(self) -> None:
        self.stop_count += 1
        self.running.clear()

    def frames(self) -> AsyncIterator[EncodedFrame]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[EncodedFrame]:
        if False:  # pragma: no cover - keeps this an async generator
            yield  # type: ignore[unreachable]


Behavior = Callable[[], Awaitable[None]]


class _FakePipeline:
    """Pipeline whose per-run behavior is scripted.

    Each call to `run()` pops the next behavior from `behaviors` and awaits
    it. Once `behaviors` is empty, `run()` blocks until the camera is stopped
    — mimicking a healthy pipeline that only exits when the camera shuts
    down.
    """

    def __init__(self, camera: _FakeCamera, behaviors: list[Behavior] | None = None) -> None:
        self._camera = camera
        self.behaviors: list[Behavior] = list(behaviors or [])
        self.run_count = 0

    async def run(self) -> None:
        self.run_count += 1
        if self.behaviors:
            await self.behaviors.pop(0)()
            return
        # Healthy steady-state: block until camera.stop() clears `running`.
        while self._camera.running.is_set():
            await asyncio.sleep(0.005)


async def _boom() -> None:
    raise RuntimeError("simulated camera pipeline crash")


async def test_supervisor_runs_pipeline_and_stops_cleanly() -> None:
    cam = _FakeCamera()
    pipeline = _FakePipeline(cam)
    sup = CameraSupervisor(cam, pipeline)

    task = asyncio.create_task(sup.run())
    await asyncio.sleep(0.02)
    await sup.stop()
    await asyncio.wait_for(task, timeout=1.0)

    assert cam.start_count == 1
    assert cam.stop_count >= 1
    assert pipeline.run_count == 1


async def test_supervisor_restarts_after_pipeline_crash() -> None:
    cam = _FakeCamera()
    pipeline = _FakePipeline(cam, behaviors=[_boom])
    sup = CameraSupervisor(cam, pipeline, initial_restart_delay_s=0.01, max_restart_delay_s=0.05)

    task = asyncio.create_task(sup.run())
    # First run crashes → backoff → second run blocks healthy.
    await asyncio.sleep(0.1)
    await sup.stop()
    await asyncio.wait_for(task, timeout=1.0)

    assert pipeline.run_count == 2
    assert cam.start_count == 2
    assert cam.stop_count >= 2


async def test_supervisor_keeps_restarting_on_repeated_crashes() -> None:
    cam = _FakeCamera()
    pipeline = _FakePipeline(cam, behaviors=[_boom, _boom, _boom])
    sup = CameraSupervisor(cam, pipeline, initial_restart_delay_s=0.01, max_restart_delay_s=0.05)

    task = asyncio.create_task(sup.run())
    await asyncio.sleep(0.3)
    await sup.stop()
    await asyncio.wait_for(task, timeout=1.0)

    # Three scripted crashes, then a healthy run → 4 total pipeline invocations.
    assert pipeline.run_count == 4


async def test_supervisor_stop_is_prompt_during_backoff() -> None:
    cam = _FakeCamera()
    # Every run crashes, so the supervisor spends most of its time in backoff.
    pipeline = _FakePipeline(cam, behaviors=[_boom] * 20)
    sup = CameraSupervisor(cam, pipeline, initial_restart_delay_s=0.5, max_restart_delay_s=1.0)

    task = asyncio.create_task(sup.run())
    await asyncio.sleep(0.05)  # let first crash happen and enter backoff

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await sup.stop()
    await asyncio.wait_for(task, timeout=0.5)
    elapsed = loop.time() - t0

    # Stop must interrupt the 0.5s backoff, not wait it out.
    assert elapsed < 0.2, f"stop took {elapsed:.3f}s, expected near-immediate"


async def test_supervisor_stop_during_healthy_run_exits_quickly() -> None:
    cam = _FakeCamera()
    pipeline = _FakePipeline(cam)
    sup = CameraSupervisor(cam, pipeline)

    task = asyncio.create_task(sup.run())
    await asyncio.sleep(0.02)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await sup.stop()
    await asyncio.wait_for(task, timeout=0.5)
    elapsed = loop.time() - t0

    assert elapsed < 0.1


def test_supervisor_rejects_invalid_initial_delay() -> None:
    cam = _FakeCamera()
    pipeline = _FakePipeline(cam)
    with pytest.raises(ValueError, match="initial_restart_delay_s"):
        CameraSupervisor(cam, pipeline, initial_restart_delay_s=0)


def test_supervisor_rejects_max_less_than_initial() -> None:
    cam = _FakeCamera()
    pipeline = _FakePipeline(cam)
    with pytest.raises(ValueError, match="max_restart_delay_s"):
        CameraSupervisor(cam, pipeline, initial_restart_delay_s=2.0, max_restart_delay_s=1.0)
