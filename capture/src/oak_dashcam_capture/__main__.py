from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

import depthai as dai
import uvicorn
from oak_dashcam_shared import CameraStore, DashcamConfig, SegmentIndex, load_config
from oak_dashcam_shared.config import CameraConfig

from oak_dashcam_capture.camera import Camera
from oak_dashcam_capture.depthai_camera import DepthAICamera
from oak_dashcam_capture.discovery import DiscoveryService, create_discovery_app
from oak_dashcam_capture.mock import MockCamera
from oak_dashcam_capture.retention import RetentionManager
from oak_dashcam_capture.segments import SegmentWriter
from oak_dashcam_capture.sinks import FfmpegMp4Sink, RawBitstreamSink, SegmentSink
from oak_dashcam_capture.supervisor import CameraSupervisor

log = logging.getLogger("oak_dashcam_capture")

_MOCK_ENV_VAR = "OAK_DASHCAM_MOCK"


def _should_use_mock() -> bool:
    """Return True if cameras should run as mocks instead of real OAK devices.

    Three ways to land on mock mode: explicit opt-in via `OAK_DASHCAM_MOCK=1`
    (dev machine), device enumeration fails entirely (no DepthAI runtime), or
    no devices are actually connected. Anything else uses real hardware.
    """
    if os.environ.get(_MOCK_ENV_VAR):
        log.info("%s is set; forcing mock cameras", _MOCK_ENV_VAR)
        return True
    try:
        devices = dai.Device.getAllAvailableDevices()
    except Exception as exc:
        log.warning("DepthAI device enumeration failed (%s); using mock cameras", exc)
        return True
    if not devices:
        log.warning("no DepthAI devices detected; using mock cameras")
        return True
    log.info("found %d DepthAI device(s): %s", len(devices), [d.deviceId for d in devices])
    return False


def _build_camera(
    cam_cfg: CameraConfig,
    *,
    use_mock: bool,
    camera_store: CameraStore | None = None,
) -> Camera:
    if use_mock:
        return MockCamera(
            camera_id=cam_cfg.id,
            fps=cam_cfg.fps,
            keyframe_interval=cam_cfg.fps,  # 1-second GOP
        )

    # If we have a store, register a callback so the DB tracks the real
    # MxID of whichever OAK the supervisor ends up booting. Before this,
    # new cameras seeded from YAML with `mxid: "auto"` would stay at
    # `"auto"` in the DB forever, and the discovery endpoint had no way
    # to know they were already in use.
    callback = None
    if camera_store is not None:

        def _on_resolved(camera_id: str, resolved_mxid: str) -> None:
            try:
                current = camera_store.get(camera_id)
                if current is None or current.mxid == resolved_mxid:
                    return
                updated = current.model_copy(update={"mxid": resolved_mxid})
                camera_store.update(updated)
                log.info(
                    "camera store: pinned %s to MxID %s (was %s)",
                    camera_id,
                    resolved_mxid,
                    current.mxid,
                )
            except Exception:
                log.exception(
                    "camera store: failed to pin resolved mxid for %s",
                    camera_id,
                )

        callback = _on_resolved

    return DepthAICamera(cam_cfg, on_device_resolved=callback)


def resolve_auto_mxids(cameras: list[CameraConfig]) -> list[CameraConfig]:
    """Replace `mxid="auto"` on each camera with a concrete deviceId.

    Without this, two cameras in the same config both asking for `auto` would
    race to open the same physical device (the first one enumerated by
    DepthAI), and the second would silently fail to claim hardware. We
    enumerate devices once here, honor any cameras that pin a specific mxid
    first, then hand out the remaining devices to the auto-cameras in config
    order.
    """
    devices = dai.Device.getAllAvailableDevices()
    if not devices:
        raise RuntimeError("no DepthAI devices available to resolve auto mxids")

    pinned: set[str] = {c.mxid for c in cameras if c.mxid != "auto"}
    available_ids: list[str] = [d.deviceId for d in devices if d.deviceId not in pinned]

    resolved: list[CameraConfig] = []
    for cam in cameras:
        if cam.mxid != "auto":
            resolved.append(cam)
            continue
        if not available_ids:
            raise RuntimeError(
                f"no unclaimed DepthAI device for camera {cam.id!r} "
                f"(have {len(devices)} device(s), {len(cameras)} cameras configured)"
            )
        assigned = available_ids.pop(0)
        log.info("assigning DepthAI device %s to camera %s", assigned, cam.id)
        resolved.append(cam.model_copy(update={"mxid": assigned}))
    return resolved


def _build_sink(cam_cfg: CameraConfig, *, use_mock: bool) -> SegmentSink:
    """Pick a segment sink for one camera.

    Default is `FfmpegMp4Sink` (playable MP4 output). Falls back to
    `RawBitstreamSink` in two cases:

    1. `ffmpeg` is not on PATH — recording still works, but you'll need to
       remux the `.h265` files before playback. Lets the service boot on a
       fresh dev box without a hard crash at startup.
    2. We're in mock mode — `MockCamera` emits synthetic bytes that aren't
       valid HEVC, and ffmpeg would reject every frame with "No start code
       is found". Routing mock runs through the raw sink keeps the mock
       path exercising the full pipeline (index, retention, rotation)
       without any codec-level noise.
    """
    if use_mock:
        log.info("mock mode: using raw bitstream sink for camera %s", cam_cfg.id)
        return RawBitstreamSink(cam_cfg.codec)
    if shutil.which("ffmpeg") is None:
        log.warning(
            "ffmpeg not found on PATH; falling back to raw %s bitstream output "
            "for camera %s (use `ffmpeg -f %s -i FILE -c copy OUT.mp4` to remux)",
            cam_cfg.codec.value,
            cam_cfg.id,
            cam_cfg.codec.value,
        )
        return RawBitstreamSink(cam_cfg.codec)
    return FfmpegMp4Sink(cam_cfg.codec, fps=cam_cfg.fps)


def build_supervisors(
    config: DashcamConfig,
    index: SegmentIndex | None = None,
    *,
    cameras_override: list[CameraConfig] | None = None,
    camera_store: CameraStore | None = None,
    camera_registry: dict[str, Camera] | None = None,
) -> list[CameraSupervisor]:
    """Construct one supervisor per configured camera.

    If `camera_registry` is provided, each constructed camera is
    recorded in it under its `camera_id`. The discovery sidecar reads
    from this registry to tap live MJPEG preview streams from
    supervisors that are already recording. Only real (non-mock)
    `DepthAICamera` instances are registered — mock cameras don't
    expose a preview stream.
    """
    use_mock = _should_use_mock()
    # Prefer `cameras_override` (DB-backed) when the caller provides it; the
    # test suite and any future callers that already hold a resolved camera
    # list can pass it directly to skip the YAML fallback.
    source_cameras = cameras_override if cameras_override is not None else config.cameras
    cameras = source_cameras if use_mock else resolve_auto_mxids(source_cameras)
    supervisors: list[CameraSupervisor] = []
    for cam_cfg in cameras:
        camera = _build_camera(cam_cfg, use_mock=use_mock, camera_store=camera_store)
        if camera_registry is not None and isinstance(camera, DepthAICamera):
            camera_registry[cam_cfg.id] = camera
        writer = SegmentWriter(
            camera=camera,
            root=config.storage.root,
            segment_seconds=config.storage.segment_seconds,
            sink=_build_sink(cam_cfg, use_mock=use_mock),
            codec=cam_cfg.codec,
            index=index,
        )
        supervisors.append(CameraSupervisor(camera=camera, pipeline=writer))
    return supervisors


async def run_supervisors(supervisors: list[CameraSupervisor]) -> None:
    """Run every supervisor concurrently until each returns on its own.

    One supervisor raising an unexpected exception must not take the others
    down — `return_exceptions=True` isolates failures so healthy cameras keep
    recording.
    """
    if not supervisors:
        log.warning("no cameras configured; nothing to run")
        return

    log.info("starting %d camera supervisor(s)", len(supervisors))
    results = await asyncio.gather(
        *(sup.run() for sup in supervisors),
        return_exceptions=True,
    )
    for sup, result in zip(supervisors, results, strict=True):
        cam_id = sup._camera.camera_id
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            log.error("supervisor for %s exited with error: %r", cam_id, result)


def _install_signal_handlers(
    supervisors: list[CameraSupervisor], retention: RetentionManager
) -> None:
    loop = asyncio.get_running_loop()
    # Keep strong references to stop() tasks so they can't be garbage collected
    # mid-shutdown. They're discarded when the process exits.
    pending_stops: set[asyncio.Task[None]] = set()

    def _track(coro: asyncio.Task[None]) -> None:
        pending_stops.add(coro)
        coro.add_done_callback(pending_stops.discard)

    def _trigger_shutdown(sig_name: str) -> None:
        log.info("received %s, stopping supervisors", sig_name)
        for sup in supervisors:
            _track(asyncio.create_task(sup.stop()))
        _track(asyncio.create_task(retention.stop()))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _trigger_shutdown, sig.name)


async def _main_async(config: DashcamConfig) -> None:
    # The clip index and the camera store share one DB file on the storage
    # volume. Both the webapp and retention read from here, and restarting
    # capture doesn't lose history or camera config.
    db_path = config.storage.root / "dashcam.db"
    index = SegmentIndex(db_path)
    camera_store = CameraStore(db_path)

    # First-run seed: if the DB has no cameras yet, copy them in from the
    # YAML config. After that the DB is authoritative; YAML is ignored for
    # cameras and edits come from the webapp.
    if camera_store.seed_if_empty(config.cameras):
        log.info("camera store was empty; seeded %d camera(s) from YAML", len(config.cameras))

    cameras = camera_store.list_all()
    if not cameras:
        log.warning("no cameras configured; capture will idle until one is added via the webapp")

    # Camera registry shared between supervisors and the discovery
    # sidecar. Supervisors populate it on construction; the sidecar
    # reads it to tap each camera's live MJPEG preview stream without
    # opening a second device.
    camera_registry: dict[str, Camera] = {}
    supervisors = build_supervisors(
        config,
        index=index,
        cameras_override=cameras,
        camera_store=camera_store,
        camera_registry=camera_registry,
    )
    retention = RetentionManager(
        root=config.storage.root,
        max_bytes=config.storage.retention_gb * 1024**3,
        index=index,
    )
    _install_signal_handlers(supervisors, retention)

    # Discovery HTTP sidecar for the "Add camera" UI and the live
    # preview tiles. Runs on port 8081 inside the container, reachable
    # only via the docker network (the webapp proxies it). Skipped in
    # mock mode since mock cameras aren't real DepthAI devices.
    discovery_task: asyncio.Task[None] | None = None
    if not _should_use_mock():
        # DiscoveryService expects a dict[str, DepthAICamera]; the
        # registry we build above is typed against the broader
        # `Camera` protocol for the mock path, but in non-mock mode
        # every entry IS a DepthAICamera so the cast is safe.
        from oak_dashcam_capture.depthai_camera import (
            DepthAICamera as _DepthAICamera,
        )

        depthai_registry: dict[str, _DepthAICamera] = {
            cid: cam for cid, cam in camera_registry.items() if isinstance(cam, _DepthAICamera)
        }
        discovery = DiscoveryService(camera_store, camera_registry=depthai_registry)
        discovery_app = create_discovery_app(discovery)
        uvicorn_config = uvicorn.Config(
            discovery_app,
            host="0.0.0.0",
            port=8081,
            log_level=config.logging.level.lower(),
        )
        uvicorn_server = uvicorn.Server(uvicorn_config)
        log.info("starting discovery HTTP sidecar on port 8081")
        discovery_task = asyncio.create_task(uvicorn_server.serve())

    # Run supervisors + retention + (optionally) discovery concurrently.
    # `return_exceptions=True` isolates them — a crash in one must not
    # take down the other.
    tasks: list[asyncio.Future[None] | asyncio.Task[None]] = [
        asyncio.ensure_future(run_supervisors(supervisors)),
        asyncio.ensure_future(retention.run()),
    ]
    if discovery_task is not None:
        tasks.append(discovery_task)
    await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oak-dashcam-capture")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/dashcam.yaml"),
        help="Path to dashcam.yaml",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    logging.basicConfig(
        level=config.logging.level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("loaded config with %d camera(s)", len(config.cameras))

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main_async(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
