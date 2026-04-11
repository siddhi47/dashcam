"""One-shot snapshot CLI: grabs a single JPEG from every configured camera.

Purpose is purely diagnostic — "are my OAK cameras actually seeing what I
think they're seeing?" This is deliberately separate from the recording
pipeline so it can run while capture is stopped without touching any of
the recording code paths.

Usage:

    uv run python -m oak_dashcam_capture.snapshot
    uv run python -m oak_dashcam_capture.snapshot --output-dir /tmp/snaps
    uv run python -m oak_dashcam_capture.snapshot --config config/dashcam.yaml

Produces `snapshot-{camera_id}.jpg` per camera. Open them with any image
viewer.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import depthai as dai
from oak_dashcam_shared import load_config
from oak_dashcam_shared.config import CameraConfig

from oak_dashcam_capture.depthai_camera import _RESOLUTION_PIXELS

log = logging.getLogger("oak_dashcam_snapshot")

# Discard this many frames before saving, so auto-exposure / white balance
# have time to converge and we don't ship a black or blown-out image.
_WARMUP_FRAMES = 10


def _capture_snapshot(cam_cfg: CameraConfig, output: Path) -> None:
    log.info(
        "capturing snapshot from camera %s (device %s) -> %s",
        cam_cfg.id,
        cam_cfg.mxid,
        output,
    )

    devices = {d.deviceId: d for d in dai.Device.getAllAvailableDevices()}
    if cam_cfg.mxid not in devices:
        raise RuntimeError(f"device {cam_cfg.mxid} not found (available: {list(devices)})")

    device = dai.Device(devices[cam_cfg.mxid])
    pipeline = dai.Pipeline(defaultDevice=device)

    width, height = _RESOLUTION_PIXELS[cam_cfg.resolution]
    camera_node = pipeline.create(dai.node.Camera).build(
        boardSocket=dai.CameraBoardSocket.CAM_A,
    )
    cam_out = camera_node.requestOutput(
        size=(width, height),
        type=dai.ImgFrame.Type.NV12,
        fps=float(cam_cfg.fps),
    )

    encoder = pipeline.create(dai.node.VideoEncoder)
    encoder.setDefaultProfilePreset(
        float(cam_cfg.fps),
        dai.VideoEncoderProperties.Profile.MJPEG,
    )
    cam_out.link(encoder.input)

    queue = encoder.out.createOutputQueue(maxSize=1, blocking=False)

    try:
        pipeline.start()
        packet: Any = None
        for _ in range(_WARMUP_FRAMES):
            packet = queue.get()
        if packet is None:
            raise RuntimeError("no frames received from camera")

        raw = packet.getData()
        data = raw.tobytes() if hasattr(raw, "tobytes") else bytes(raw)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        log.info("wrote %d bytes to %s", len(data), output)
    finally:
        try:
            pipeline.stop()
        except Exception:
            log.exception("error stopping pipeline")
        try:
            device.close()
        except Exception:
            log.exception("error closing device")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oak-dashcam-snapshot")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/dashcam.yaml"),
        help="Path to dashcam.yaml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory to write snapshot-{camera_id}.jpg files",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)

    # Import lazily so importing this module for testing doesn't pull the
    # whole __main__ wiring.
    from oak_dashcam_capture.__main__ import resolve_auto_mxids

    try:
        cameras = resolve_auto_mxids(config.cameras)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    failures = 0
    for cam in cameras:
        output = args.output_dir / f"snapshot-{cam.id}.jpg"
        try:
            _capture_snapshot(cam, output)
        except Exception:
            log.exception("failed to capture snapshot for camera %s", cam.id)
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
