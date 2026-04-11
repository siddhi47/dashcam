"""Camera implementation backed by a physical Luxonis OAK device via DepthAI.

The DepthAI Python API is blocking (`MessageQueue.get()` blocks the caller
until a packet arrives) so the actual device interaction runs in a worker
thread. Encoded packets are pushed across the thread boundary into an
`asyncio.Queue` which the `frames()` async iterator drains, so this class
still satisfies the `Camera` protocol from the consumer side.

One `DepthAICamera` instance owns one `dai.Device` and one pipeline. If the
config declares multiple cameras, instantiate one of these per camera — the
supervisor already runs them independently.

Validation status: device enumeration and packet conversion are unit-tested
with fake DepthAI objects. The pipeline-construction path (build pipeline,
open device, link nodes, start streaming) is NOT exercised without real
hardware — it runs for the first time on the Pi. If something there breaks,
the error will surface in `_worker` and be reported back to the caller of
`start()` via `_start_error`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

import depthai as dai
from oak_dashcam_shared.config import CameraConfig, Codec, Resolution

from oak_dashcam_capture.camera import EncodedFrame

log = logging.getLogger(__name__)


def _sensor_resolution(resolution: Resolution) -> Any:
    """Map our `Resolution` enum to DepthAI's `ColorCameraProperties.SensorResolution`.

    Kept as a function (not a dict constant) so it's evaluated lazily —
    `dai.ColorCameraProperties.SensorResolution` is resolved at call
    time, which makes the module import safe even when DepthAI's
    internals shift between minor versions.
    """
    enum = dai.ColorCameraProperties.SensorResolution
    match resolution:
        case Resolution.R_720P:
            return enum.THE_720_P
        case Resolution.R_1080P:
            return enum.THE_1080_P
        case Resolution.R_4K:
            return enum.THE_4_K

# Live-preview stream parameters. These are deliberately small so the
# MJPEG encoder runs cheap on the OAK and the resulting stream is
# reasonable to send over a Wi-Fi AP to a phone or a browser on the LAN.
# 480x270 is 1/4 resolution linearly, 1/16 in pixel count — well under
# what you can see on a phone screen anyway, and at 30 fps the resulting
# bandwidth is ~3-5 Mbps per camera which fits comfortably inside USB 2.0
# even with two cameras recording H.265 simultaneously.
#
# The MJPEG encoder runs at the same fps as the storage stream (sensor
# fps, typically 30) — this gives smooth motion in the preview and
# keeps the pipeline simple: no rate division, no worker-side skip
# logic, the encoder does what the camera tells it.
_PREVIEW_WIDTH = 480
_PREVIEW_HEIGHT = 270
# How many frames each browser subscriber buffers before frames get
# dropped. Four is enough to hide a momentary WebSocket hiccup without
# letting a stalled client back-pressure the MJPEG worker.
_PREVIEW_SUBSCRIBER_QUEUE = 4


def _codec_profile(codec: Codec) -> dai.VideoEncoderProperties.Profile:
    match codec:
        case Codec.H264:
            return dai.VideoEncoderProperties.Profile.H264_MAIN
        case Codec.H265:
            return dai.VideoEncoderProperties.Profile.H265_MAIN


class DepthAIDeviceNotFoundError(RuntimeError):
    """Raised when no DepthAI device matches the requested MxID."""


def _find_device(mxid: str, devices: list[dai.DeviceInfo] | None = None) -> dai.DeviceInfo:
    """Pick a DepthAI device by MxID.

    `mxid="auto"` selects the first available device. Anything else is matched
    exactly against each device's `deviceId`. Raises
    `DepthAIDeviceNotFoundError` if nothing matches — the supervisor will catch
    this and restart with backoff, so cameras can come and go as USB changes.
    """
    if devices is None:
        devices = dai.Device.getAllAvailableDevices()
    if not devices:
        raise DepthAIDeviceNotFoundError("no DepthAI devices detected")
    if mxid == "auto":
        return devices[0]
    for dev in devices:
        if dev.deviceId == mxid:
            return dev
    available = [d.deviceId for d in devices]
    raise DepthAIDeviceNotFoundError(
        f"no DepthAI device with MxID {mxid!r} (available: {available})"
    )


def _packet_to_frame(
    packet: Any,
    start_ts_us: int | None,
) -> tuple[EncodedFrame, int]:
    """Convert one DepthAI packet to an `EncodedFrame` with normalized PTS.

    Returns `(frame, start_ts_us)` where the second element is the absolute
    device timestamp of the first packet ever seen — subsequent calls should
    thread it back in so PTS values are relative to stream start.
    """
    ts_td = packet.getTimestampDevice()
    absolute_us = int(ts_td.total_seconds() * 1_000_000)
    if start_ts_us is None:
        start_ts_us = absolute_us

    raw = packet.getData()
    data = raw.tobytes() if hasattr(raw, "tobytes") else bytes(raw)

    return (
        EncodedFrame(
            data=data,
            pts_us=absolute_us - start_ts_us,
            keyframe=packet.getFrameType() == dai.EncodedFrame.FrameType.I,
        ),
        start_ts_us,
    )


class DepthAICamera:
    """`Camera` backed by a real OAK device.

    `on_device_resolved` is an optional callback invoked on the worker
    thread once the DepthAI library has opened the device and returned
    its real MxID. The capture main wires this to `CameraStore.update`
    so the DB transitions from the `mxid="auto"` placeholder set by
    the YAML seed to the actual hardware serial. This is what makes
    the discovery endpoint correctly mark running cameras as
    `assigned=True` in the webapp UI.
    """

    def __init__(
        self,
        cam_cfg: CameraConfig,
        *,
        on_device_resolved: Callable[[str, str], None] | None = None,
    ) -> None:
        self._cfg = cam_cfg
        self._on_device_resolved = on_device_resolved
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[EncodedFrame | None] | None = None
        self._thread: threading.Thread | None = None
        self._preview_thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        # Live preview subscribers: each HTTP client calling
        # `subscribe_preview()` gets its own bounded asyncio.Queue that
        # the MJPEG worker thread fans out to. The set is only mutated
        # from the asyncio loop thread so no lock is needed.
        self._preview_subscribers: set[asyncio.Queue[bytes | None]] = set()

    @property
    def camera_id(self) -> str:
        return self._cfg.id

    async def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("DepthAICamera already started")
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._stop_flag.clear()
        self._ready.clear()
        self._start_error = None

        self._thread = threading.Thread(
            target=self._worker,
            name=f"depthai-{self._cfg.id}",
            daemon=True,
        )
        self._thread.start()

        # Wait for the worker to either fully open the device or fail.
        await asyncio.to_thread(self._ready.wait)
        if self._start_error is not None:
            self._thread = None
            raise self._start_error

    async def stop(self) -> None:
        self._stop_flag.set()
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join)
            self._thread = None
        preview_thread = self._preview_thread
        if preview_thread is not None:
            await asyncio.to_thread(preview_thread.join, 2.0)
            self._preview_thread = None
        if self._queue is not None and self._loop is not None:
            # Signal end-of-stream to anyone still draining `frames()`.
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        # Wake any live preview subscribers so they exit cleanly.
        if self._loop is not None:
            for subscriber in list(self._preview_subscribers):
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(subscriber.put_nowait, None)

    def frames(self) -> AsyncIterator[EncodedFrame]:
        return self._drain()

    async def _drain(self) -> AsyncIterator[EncodedFrame]:
        if self._queue is None:
            raise RuntimeError("DepthAICamera.frames() called before start()")
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    def subscribe_preview(self) -> AsyncIterator[bytes]:
        """Yield JPEG frames from the live MJPEG preview stream.

        Multiple callers can subscribe at the same time — each gets
        their own small queue and the MJPEG worker fans out every
        encoded frame to all of them. On backpressure (a slow
        subscriber whose queue is full) we drop that subscriber's
        frames silently rather than stall the others; preview is
        best-effort.
        """
        return self._drain_preview()

    async def _drain_preview(self) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_PREVIEW_SUBSCRIBER_QUEUE)
        self._preview_subscribers.add(queue)
        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    return
                yield frame
        finally:
            self._preview_subscribers.discard(queue)

    def _worker(self) -> None:
        device: dai.Device | None = None
        pipeline: dai.Pipeline | None = None
        queue: dai.MessageQueue | None = None
        preview_queue: dai.MessageQueue | None = None
        try:
            # We deliberately force USB 2.0 HIGH speed. On a Pi 4 with OAK
            # devices, USB 3.0 SuperSpeed negotiation is flaky — the link
            # comes up, runs for ~8 seconds, then drops with no kernel
            # error, leaving the device stuck in an unbooted state.
            # USB 2.0 HIGH (480 Mbps) is rock-solid in the same setup and
            # easily carries 1080p H.265 at 8 Mbps per camera (~2% of the
            # available bandwidth).
            #
            # We also use the **no-arg** `dai.Device(maxUsbSpeed)` form
            # instead of `dai.Device(mxid, maxUsbSpeed)`. The latter
            # overload searches with `X_LINK_ANY_STATE` which excludes
            # devices in the bootloader state our hardware often ends up
            # in, and fails with `X_LINK_DEVICE_NOT_FOUND` even when the
            # kernel plainly sees the device. The no-arg form uses a
            # broader discovery path that actually boots unbooted
            # devices. We log the resolved MxID after open so multi-cam
            # setups can still tell which camera they ended up with.
            log.info(
                "opening DepthAI device for camera %s (USB 2.0 mode, mxid hint=%s)",
                self._cfg.id,
                self._cfg.mxid,
            )
            device = dai.Device(dai.UsbSpeed.HIGH)
            resolved_mxid = str(device.getDeviceId())
            log.info(
                "camera %s resolved to DepthAI device %s",
                self._cfg.id,
                resolved_mxid,
            )
            if self._on_device_resolved is not None:
                try:
                    self._on_device_resolved(self._cfg.id, resolved_mxid)
                except Exception:
                    log.exception("on_device_resolved callback for %s failed", self._cfg.id)
            pipeline = dai.Pipeline(defaultDevice=device)

            # We use `dai.node.ColorCamera` (the v2-style unified camera
            # node) rather than the newer `dai.node.Camera` +
            # `requestOutput` API. Two reasons:
            #
            # 1. ColorCamera natively exposes `.video` (full-res NV12)
            #    AND `.preview` (downscaled, configurable) outputs in
            #    one node. No second `requestOutput` call — which on
            #    Pi 4 + DepthAI v3.5 silently breaks the pipeline. No
            #    ImageManip — which on the same combo seems to load
            #    the Myriad X enough to cause X_LINK_ERROR disconnects
            #    when two cameras are running at once.
            # 2. ColorCamera's `.preview` runs on dedicated ISP
            #    hardware that downscales the sensor output straight
            #    to the target preview size, so encoding MJPEG from it
            #    is much cheaper than re-encoding a full-res frame.
            color_cam = pipeline.create(dai.node.ColorCamera)
            color_cam.setBoardSocket(dai.CameraBoardSocket.CAM_A)
            color_cam.setResolution(_sensor_resolution(self._cfg.resolution))
            color_cam.setFps(float(self._cfg.fps))
            color_cam.setPreviewSize(_PREVIEW_WIDTH, _PREVIEW_HEIGHT)
            color_cam.setInterleaved(False)

            # 180° rotation for ceiling-mounted cameras. Handled at the
            # ColorCamera ISP level so both the recording and preview
            # branches see an already-flipped frame — no extra
            # ImageManip, no extra VPU cost. Only 0° and 180° are
            # supported; see the comment on `CameraConfig.rotation_degrees`.
            if self._cfg.rotation_degrees == 180:
                color_cam.setImageOrientation(
                    dai.CameraImageOrientation.ROTATE_180_DEG
                )
            log.info(
                "camera %s: rotation configured to %d deg",
                self._cfg.id,
                self._cfg.rotation_degrees,
            )

            # Storage encoder: full-res H.265 off the `.video` output.
            encoder = pipeline.create(dai.node.VideoEncoder)
            encoder.setDefaultProfilePreset(
                float(self._cfg.fps),
                _codec_profile(self._cfg.codec),
            )
            encoder.setBitrateKbps(self._cfg.bitrate_kbps)
            encoder.setKeyframeFrequency(self._cfg.fps)  # 1-second GOP
            color_cam.video.link(encoder.input)
            queue = encoder.out.createOutputQueue(maxSize=30, blocking=False)

            # Preview path: ColorCamera's `.preview` output is BGR,
            # but VideoEncoder's MJPEG profile only accepts NV12 /
            # YUV400p. An `ImageManip` node does the color-space
            # conversion — and *only* the color-space conversion,
            # since `.preview` is already downscaled on the ISP to
            # our target size. That makes this ImageManip much
            # cheaper than the full-res resize variant we tried
            # earlier (which caused X_LINK_ERROR disconnects under
            # two-camera load).
            preview_manip = pipeline.create(dai.node.ImageManip)
            preview_manip.initialConfig.setFrameType(dai.ImgFrame.Type.NV12)
            color_cam.preview.link(preview_manip.inputImage)

            # Preview encoder: MJPEG at the full sensor fps. The preview
            # frames are tiny (_PREVIEW_WIDTH x _PREVIEW_HEIGHT, already
            # downscaled on the ISP) so the aggregate encoder + USB work
            # is well inside the Pi 4's USB 2.0 headroom even with two
            # cameras running.
            preview_encoder = pipeline.create(dai.node.VideoEncoder)
            preview_encoder.setDefaultProfilePreset(
                float(self._cfg.fps),
                dai.VideoEncoderProperties.Profile.MJPEG,
            )
            preview_manip.out.link(preview_encoder.input)
            preview_queue = preview_encoder.out.createOutputQueue(
                maxSize=4, blocking=False
            )

            pipeline.start()
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            self._cleanup(pipeline, device)
            return

        self._ready.set()

        # Spawn the MJPEG worker thread now that the pipeline is
        # running and `preview_queue` exists. It runs its own blocking
        # `queue.get()` loop and fans frames out to all current
        # subscribers via the asyncio loop.
        self._preview_thread = threading.Thread(
            target=self._mjpeg_worker,
            args=(preview_queue,),
            name=f"depthai-{self._cfg.id}-preview",
            daemon=True,
        )
        self._preview_thread.start()

        start_ts_us: int | None = None
        try:
            while not self._stop_flag.is_set():
                packet = queue.get()
                if packet is None:
                    continue
                frame, start_ts_us = _packet_to_frame(packet, start_ts_us)
                if self._loop is not None and self._queue is not None:
                    self._loop.call_soon_threadsafe(self._queue.put_nowait, frame)
        except BaseException:
            log.exception("DepthAI worker for %s crashed", self._cfg.id)
            raise
        finally:
            self._cleanup(pipeline, device)
            if self._loop is not None and self._queue is not None:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    def _mjpeg_worker(self, preview_queue: Any) -> None:
        """Background thread: drain DepthAI's MJPEG queue, fan out to subscribers.

        Runs until `self._stop_flag` is set or `preview_queue.get()`
        returns None (pipeline closed). Never touches the asyncio loop
        directly — all subscriber-queue updates go through
        `call_soon_threadsafe`.
        """
        log.info("camera %s: preview MJPEG worker started", self._cfg.id)
        try:
            while not self._stop_flag.is_set():
                try:
                    packet = preview_queue.get()
                except Exception:
                    log.exception("camera %s: MJPEG queue read failed", self._cfg.id)
                    break
                if packet is None:
                    continue
                if not self._preview_subscribers:
                    # Nobody watching — skip the bytes conversion to save work.
                    continue
                raw = packet.getData()
                jpeg = bytes(raw.tobytes() if hasattr(raw, "tobytes") else raw)
                self._broadcast_preview(jpeg)
        except BaseException:
            log.exception("camera %s: MJPEG worker crashed", self._cfg.id)
        finally:
            log.info("camera %s: preview MJPEG worker stopped", self._cfg.id)

    def _broadcast_preview(self, jpeg: bytes) -> None:
        """Publish a JPEG frame to every current subscriber.

        Runs on the MJPEG worker thread; hands off to the asyncio loop
        via `call_soon_threadsafe` so subscriber queues are only
        mutated from the loop thread. Subscribers with full queues
        (slow consumers) silently drop the frame — preview is
        best-effort and we never stall the worker on a slow client.
        """
        loop = self._loop
        if loop is None:
            return

        def _deliver() -> None:
            for subscriber in list(self._preview_subscribers):
                with contextlib.suppress(asyncio.QueueFull):
                    subscriber.put_nowait(jpeg)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_deliver)

    @staticmethod
    def _cleanup(pipeline: dai.Pipeline | None, device: dai.Device | None) -> None:
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                log.exception("error stopping DepthAI pipeline")
        if device is not None:
            try:
                device.close()
            except Exception:
                log.exception("error closing DepthAI device")
