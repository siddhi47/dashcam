"""Discovery HTTP sidecar for the capture service.

Adds a tiny FastAPI app (served on a dedicated port by the capture
process itself) that exposes the OAK devices not currently owned by a
recording supervisor, plus a live MJPEG preview of one of them. The
webapp proxies both endpoints through `/api/discovery/*` so the
"Add camera" UI can show live video of the camera about to be added.

Architecture notes:

* The sidecar runs in the same Python process as the recording
  supervisors via `asyncio.gather`, so it shares the DepthAI library
  state with them. That means any device already held by a supervisor
  is invisible to this code — `dai.Device()` only returns unclaimed
  devices. This is exactly the filter we want: "unassigned cameras".
* **USB 2.0 mode is forced** (`dai.UsbSpeed.HIGH`) because USB 3.0
  SuperSpeed is unreliable on the Pi 4 with OAK devices — same reason
  as `depthai_camera.py`. 1080p H.265 at 8 Mbps fits comfortably under
  480 Mbps.
* **Specific-MxID targeting doesn't work** in DepthAI v3 for unbooted
  devices (the `dai.Device(mxid, speed)` overload fails with
  `X_LINK_DEVICE_NOT_FOUND`). So the preview endpoint always streams
  "the first unclaimed OAK" via the no-arg `dai.Device(speed)` form.
  This is the right behavior for the common case — plug in one new
  camera, add it in the UI, repeat.
* All DepthAI calls are **blocking** and run in `asyncio.to_thread`
  so they can't stall the event loop (which is also hosting the
  supervisors and the retention manager).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import depthai as dai
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from oak_dashcam_shared import CameraStore
from pydantic import BaseModel

from oak_dashcam_capture.depthai_camera import DepthAICamera

log = logging.getLogger(__name__)


_PREVIEW_WIDTH = 640
_PREVIEW_HEIGHT = 360
_PREVIEW_FPS = 10.0
_MJPEG_BOUNDARY = "frame"

# Luxonis OAK USB identifiers. 03e7 is Intel Movidius' vendor ID; the two
# product IDs correspond to the bootloader and operational firmware modes
# of the MyriadX VPU inside every OAK device.
_OAK_USB_VENDOR = "03e7"
_OAK_USB_PRODUCTS_BOOTED = "f63b"
_OAK_USB_PRODUCTS_BOOTLOADER = "2485"
_SYSFS_USB_ROOT = Path("/sys/bus/usb/devices")


class DiscoveredCamera(BaseModel):
    mxid: str
    assigned: bool


class DiscoveryResponse(BaseModel):
    cameras: list[DiscoveredCamera]


CameraRegistry = dict[str, DepthAICamera]


class DiscoveryService:
    """Owns the lock protecting concurrent DepthAI access from the sidecar.

    Holds a reference to the **camera registry** — a dict mapping
    `camera_id → DepthAICamera` maintained by the capture main for
    every currently-running supervisor. The registry lets the live
    preview endpoint tap the dual-output pipeline of an already-
    recording camera directly: no second device open, no USB
    contention, and the preview keeps working for as long as the
    camera is recording.

    The setup-preview endpoint (for *unassigned* cameras) still opens
    its own transient `dai.Device` because there's no supervisor yet.
    The lock protects that path against concurrent clients.
    """

    def __init__(
        self,
        camera_store: CameraStore,
        *,
        camera_registry: CameraRegistry | None = None,
    ) -> None:
        self._camera_store = camera_store
        self._camera_registry: CameraRegistry = (
            camera_registry if camera_registry is not None else {}
        )
        self._lock = asyncio.Lock()

    def get_camera(self, camera_id: str) -> DepthAICamera | None:
        return self._camera_registry.get(camera_id)

    async def stream_live_preview(self, camera_id: str) -> AsyncIterator[bytes]:
        """Subscribe to the MJPEG preview of a currently-recording camera.

        Yields `multipart/x-mixed-replace` chunks. No device open, no
        lock — we just tap the `DepthAICamera`'s subscriber fan-out.
        On client disconnect, the subscriber is removed and its queue
        is garbage collected.
        """
        camera = self._camera_registry.get(camera_id)
        if camera is None:
            return
        async for jpeg in camera.subscribe_preview():
            yield _mjpeg_chunk(jpeg)

    async def list_cameras(self) -> list[DiscoveredCamera]:
        """Return every OAK device currently visible on the USB bus.

        Enumeration reads `/sys/bus/usb/devices/` directly rather than
        calling into DepthAI. This is intentional: DepthAI v3's
        `dai.Device()` constructor can *hang* when the library's
        internal state is confused (e.g. after a failed boot, or while
        another device is mid-recording), and Python threads can't be
        killed — so a hung enumeration would wedge the endpoint
        forever. Reading sysfs is a pure stat-file operation, always
        fast, and correctly reports every OAK physically present.

        Caveat: sysfs can only return the real MxID for devices that
        are already *booted* (product ID f63b). OAKs in bootloader
        mode (product ID 2485) show up as a generic serial and are
        skipped here — if a freshly-plugged camera has never been
        booted by any process, the user won't see its real ID until
        they assign it and the supervisor boots it. In practice this
        hasn't been a real issue because capture's supervisor boots
        every assigned device on startup.

        Each camera is flagged with `assigned=True` if its MxID is
        already in the camera store.
        """
        assigned_mxids = {
            c.mxid for c in self._camera_store.list_all() if c.mxid and c.mxid != "auto"
        }

        # sysfs reads never block long; no need for asyncio.to_thread.
        mxids = _list_physical_oak_mxids()

        return [
            DiscoveredCamera(
                mxid=m,
                assigned=m in assigned_mxids,
            )
            for m in mxids
        ]

    async def stream_preview(self) -> AsyncIterator[bytes]:
        """Stream MJPEG from the first unclaimed OAK.

        Yields `multipart/x-mixed-replace` chunks ready to be sent by
        `StreamingResponse`. The device + pipeline are opened when the
        first byte is requested and closed when the generator is
        cancelled (i.e. the client disconnects).
        """
        async with self._lock:
            frame_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=4)
            loop = asyncio.get_running_loop()
            stop_flag = asyncio.Event()

            # Spin up the DepthAI pipeline on a worker thread. It blocks
            # on `queue.get()` internally, so we can't run it on the
            # asyncio loop directly. The thread posts encoded JPEG
            # frames into an asyncio.Queue via `call_soon_threadsafe`.
            worker = asyncio.create_task(
                asyncio.to_thread(_run_preview_worker, loop, frame_queue, stop_flag)
            )

            try:
                while True:
                    frame = await frame_queue.get()
                    if frame is None:
                        # Worker signalled end-of-stream (error or shutdown).
                        break
                    yield _mjpeg_chunk(frame)
            finally:
                stop_flag.set()
                # Drain any remaining items so the worker's put_nowait calls
                # don't raise QueueFull and leak.
                while not frame_queue.empty():
                    try:
                        frame_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    await asyncio.wait_for(worker, timeout=5.0)
                except (TimeoutError, asyncio.CancelledError):
                    log.warning("preview worker didn't exit within 5s")


def create_discovery_app(service: DiscoveryService) -> FastAPI:
    """Build the FastAPI app that the capture service hosts on port 8081."""
    app = FastAPI(title="oak-dashcam-capture-discovery", version="0.0.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/discovery/cameras", response_model=DiscoveryResponse)
    async def list_cameras() -> DiscoveryResponse:
        return DiscoveryResponse(cameras=await service.list_cameras())

    @app.get("/discovery/preview.mjpeg")
    async def preview() -> StreamingResponse:
        try:
            return StreamingResponse(
                service.stream_preview(),
                media_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/live/{camera_id}/preview.mjpeg")
    async def live_preview(camera_id: str) -> StreamingResponse:
        """Tap the running supervisor's MJPEG stream for a recording camera.

        Unlike `/discovery/preview.mjpeg`, this never opens a new
        `dai.Device` — it subscribes to the existing dual-output
        pipeline that's already encoding for storage. No USB
        contention, no risk of conflicting with the recorder.
        """
        camera = service.get_camera(camera_id)
        if camera is None:
            raise HTTPException(
                status_code=404,
                detail=f"camera {camera_id!r} is not currently running",
            )
        return StreamingResponse(
            service.stream_live_preview(camera_id),
            media_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
        )

    @app.post("/live/{camera_id}/reset")
    async def reset_camera(camera_id: str) -> dict[str, str]:
        """Stop a running camera's pipeline so its supervisor restarts it.

        This is the "Reset" button target. We don't tear down the
        supervisor; we just call `camera.stop()` on the DepthAICamera
        instance. The supervisor's existing restart-on-crash loop
        observes the pipeline ending, waits out `initial_restart_delay_s`,
        and opens a fresh device. Recording for the other camera is
        unaffected because each supervisor runs in its own asyncio
        task.

        Returns immediately (202) — we don't block waiting for the
        camera to come back, since the supervisor backoff means
        there's always a 5+ second gap and the UI can poll the live
        preview endpoint to detect when frames resume.
        """
        camera = service.get_camera(camera_id)
        if camera is None:
            raise HTTPException(
                status_code=404,
                detail=f"camera {camera_id!r} is not currently running",
            )
        log.info("reset requested for camera %s", camera_id)
        await camera.stop()
        return {"status": "resetting", "camera_id": camera_id}

    return app


# ---------------------------------------------------------------------------
# Worker helpers (run on a background thread, not the asyncio loop)
# ---------------------------------------------------------------------------


def _list_physical_oak_mxids() -> list[str]:
    """Read `/sys/bus/usb/devices` for all booted OAK devices.

    Returns the MxID (serial) of every USB device matching vendor
    `03e7` and operational-mode product id `f63b`. Bootloader-mode
    devices (product id `2485`) are skipped because their sysfs
    serial is a generic hex string, not the real MxID.

    This function never blocks, never raises on missing files, and
    never calls into DepthAI. Safe to call from the asyncio loop.
    """
    if not _SYSFS_USB_ROOT.exists():
        return []

    mxids: list[str] = []
    for usb_dev in _SYSFS_USB_ROOT.iterdir():
        vendor_file = usb_dev / "idVendor"
        product_file = usb_dev / "idProduct"
        serial_file = usb_dev / "serial"
        try:
            if not vendor_file.exists():
                continue
            vendor = vendor_file.read_text().strip()
            if vendor != _OAK_USB_VENDOR:
                continue
            product = product_file.read_text().strip() if product_file.exists() else ""
            if product != _OAK_USB_PRODUCTS_BOOTED:
                # Bootloader-mode devices (2485) don't have a real MxID
                # in sysfs yet — skip them. They'll show up after they
                # get booted by a supervisor.
                continue
            if not serial_file.exists():
                continue
            serial = serial_file.read_text().strip()
            if serial and serial not in mxids:
                mxids.append(serial)
        except OSError:
            # Files can disappear mid-iteration if a device disconnects;
            # just skip and move on.
            continue

    return mxids


def _run_preview_worker(
    loop: asyncio.AbstractEventLoop,
    frame_queue: asyncio.Queue[bytes | None],
    stop_flag: asyncio.Event,
) -> None:
    """Open an OAK, run an MJPEG pipeline, push JPEG bytes to the queue."""
    device: dai.Device | None = None
    pipeline: dai.Pipeline | None = None
    try:
        log.info("discovery: opening preview device (USB 2.0 mode)")
        try:
            device = dai.Device(dai.UsbSpeed.HIGH)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "no available" in msg or "already in use" in msg:
                log.info(
                    "discovery: no unassigned camera available to preview (%s)",
                    exc,
                )
                return  # quietly ends the stream; client gets an empty body
            raise
        log.info("discovery: previewing device %s", device.getDeviceId())

        pipeline = dai.Pipeline(defaultDevice=device)
        camera_node = pipeline.create(dai.node.Camera).build(
            boardSocket=dai.CameraBoardSocket.CAM_A,
        )
        camera_output = camera_node.requestOutput(
            size=(_PREVIEW_WIDTH, _PREVIEW_HEIGHT),
            type=dai.ImgFrame.Type.NV12,
            fps=_PREVIEW_FPS,
        )
        encoder = pipeline.create(dai.node.VideoEncoder)
        encoder.setDefaultProfilePreset(_PREVIEW_FPS, dai.VideoEncoderProperties.Profile.MJPEG)
        camera_output.link(encoder.input)
        queue = encoder.out.createOutputQueue(maxSize=2, blocking=False)
        pipeline.start()

        while not stop_flag.is_set():
            # Poll DepthAI's queue with a short timeout. Without a
            # timeout the `.get()` would block indefinitely and we
            # couldn't check `stop_flag`, so the worker would never
            # exit on client disconnect.
            pkt: Any = queue.get()
            if pkt is None:
                continue
            data = pkt.getData()
            jpeg = bytes(data.tobytes() if hasattr(data, "tobytes") else data)
            _post_frame(loop, frame_queue, jpeg)
    except BaseException:
        log.exception("discovery: preview worker crashed")
    finally:
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                log.exception("discovery: error stopping preview pipeline")
        if device is not None:
            try:
                device.close()
            except Exception:
                log.exception("discovery: error closing preview device")
        # Wake the consumer so it can exit its loop.
        _post_frame(loop, frame_queue, None)


def _post_frame(
    loop: asyncio.AbstractEventLoop,
    frame_queue: asyncio.Queue[bytes | None],
    frame: bytes | None,
) -> None:
    """Thread-safe enqueue. Drops frames if the queue is full (backpressure)."""

    def _do_put() -> None:
        # Consumer is slow — drop this frame instead of blocking the
        # DepthAI worker. MJPEG preview is best-effort.
        with contextlib.suppress(asyncio.QueueFull):
            frame_queue.put_nowait(frame)

    # Event loop already closed (shutdown race). Nothing to do.
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(_do_put)


def _mjpeg_chunk(jpeg: bytes) -> bytes:
    return (
        (
            f"--{_MJPEG_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n"
        ).encode()
        + jpeg
        + b"\r\n"
    )
