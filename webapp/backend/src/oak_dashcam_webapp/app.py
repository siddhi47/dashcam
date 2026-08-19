"""FastAPI app for the oak-dashcam webapp backend.

Reads its data from the shared SQLite DB produced by the capture service
(`{storage.root}/dashcam.db`) and serves segment files directly off the
same storage volume. Capture and webapp never share in-process state —
coordination happens entirely through the DB and the filesystem.

Endpoints (all JSON unless noted):

    GET    /api/cameras                  — list all configured cameras
    GET    /api/cameras/{id}             — one camera
    POST   /api/cameras                  — create a camera
    PUT    /api/cameras/{id}             — update a camera
    DELETE /api/cameras/{id}             — delete a camera
    GET    /api/segments                 — list segments (optional ?camera=)
    POST   /api/segments/{id}/protect    — mark as incident (?protected=true|false)
    GET    /api/segments/{id}/video      — stream the segment's MP4 with HTTP range
    GET    /healthz                      — health check

Mutations to cameras take effect after the capture service restarts —
dynamic supervisor reconfiguration is a follow-up. The response body of
camera mutation endpoints includes `{"restart_required": true}` so the
frontend can show a banner.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from oak_dashcam_shared import CameraStore, DashcamConfig, SegmentIndex
from oak_dashcam_shared.config import CameraConfig, CameraRole, Codec, Resolution
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CameraOut(BaseModel):
    id: str
    mxid: str
    role: CameraRole
    resolution: Resolution
    fps: int
    codec: Codec
    bitrate_kbps: int
    rotation_degrees: Literal[0, 180] = 0


class CameraCreate(BaseModel):
    id: str
    mxid: str = "auto"
    role: CameraRole
    resolution: Resolution = Resolution.R_1080P
    fps: int = Field(default=30, ge=1, le=60)
    codec: Codec = Codec.H265
    bitrate_kbps: int = Field(default=8000, ge=500, le=50000)
    rotation_degrees: Literal[0, 180] = 0


class CameraUpdate(BaseModel):
    # Everything except `id` — you can't rename a camera in place; delete and
    # recreate if you want a new id.
    mxid: str
    role: CameraRole
    resolution: Resolution
    fps: int = Field(ge=1, le=60)
    codec: Codec
    bitrate_kbps: int = Field(ge=500, le=50000)
    rotation_degrees: Literal[0, 180] = 0


class MutationResult(BaseModel):
    camera: CameraOut
    restart_required: Literal[True] = True


class SegmentOut(BaseModel):
    id: str  # relative path doubles as a stable identifier
    camera_id: str
    path: str
    started_at: str
    duration_s: float
    size_bytes: int
    codec: str
    protected: bool


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def _default_discovery_base_url() -> str:
    """Where the capture service's discovery sidecar lives.

    In the production docker stack the webapp talks to capture by
    service name over the compose network (`http://capture:8081`). In
    local dev without docker, `localhost:8081` is fine. Override via
    `OAK_DASHCAM_DISCOVERY_URL` if the deployment topology differs.
    """
    return os.environ.get("OAK_DASHCAM_DISCOVERY_URL", "http://capture:8081")


def create_app(
    config: DashcamConfig,
    *,
    camera_store: CameraStore | None = None,
    segment_index: SegmentIndex | None = None,
    frontend_dist: Path | None = None,
    discovery_base_url: str | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `camera_store` and `segment_index` default to ones pointing at
    `{storage.root}/dashcam.db` so a production invocation is just
    `create_app(config)`. Tests inject in-memory stores pointing at a
    temporary DB path.

    If `frontend_dist` points at a built Vite bundle directory, it's
    mounted at `/` so one container can serve both `/api/...` and the SPA.
    Docker builds set this; local dev leaves it `None` and runs the Vite
    dev server on its own port with a proxy to the backend.

    `discovery_base_url` overrides where the `/api/discovery/*` proxy
    forwards to. Defaults to `http://capture:8081` (the docker compose
    service name) or the `OAK_DASHCAM_DISCOVERY_URL` env var.
    """
    db_path = config.storage.root / "dashcam.db"
    store = camera_store if camera_store is not None else CameraStore(db_path)
    index = segment_index if segment_index is not None else SegmentIndex(db_path)
    discovery_url = (
        discovery_base_url if discovery_base_url is not None else _default_discovery_base_url()
    )

    app = FastAPI(title="oak-dashcam", version="0.0.0")

    # Local-only deployment: allow any origin so the Vite dev server can hit
    # the API from a different port. Tighten if this ever goes public.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ----------------------------------------------------------- healthz

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ----------------------------------------------------------- cameras

    @app.get("/api/cameras", response_model=list[CameraOut])
    async def list_cameras() -> list[CameraOut]:
        return [_camera_to_out(c) for c in store.list_all()]

    @app.get("/api/cameras/{camera_id}", response_model=CameraOut)
    async def get_camera(camera_id: str) -> CameraOut:
        cam = store.get(camera_id)
        if cam is None:
            raise HTTPException(status_code=404, detail=f"camera {camera_id!r} not found")
        return _camera_to_out(cam)

    @app.post("/api/cameras", response_model=MutationResult, status_code=201)
    async def create_camera(body: CameraCreate) -> MutationResult:
        if store.get(body.id) is not None:
            raise HTTPException(status_code=409, detail=f"camera {body.id!r} already exists")
        try:
            cam = CameraConfig(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.insert(cam)
        return MutationResult(camera=_camera_to_out(cam))

    @app.put("/api/cameras/{camera_id}", response_model=MutationResult)
    async def update_camera(camera_id: str, body: CameraUpdate) -> MutationResult:
        if store.get(camera_id) is None:
            raise HTTPException(status_code=404, detail=f"camera {camera_id!r} not found")
        try:
            cam = CameraConfig(id=camera_id, **body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.update(cam)
        return MutationResult(camera=_camera_to_out(cam))

    @app.delete("/api/cameras/{camera_id}")
    async def delete_camera(camera_id: str) -> dict[str, object]:
        if not store.delete(camera_id):
            raise HTTPException(status_code=404, detail=f"camera {camera_id!r} not found")
        return {"deleted": camera_id, "restart_required": True}

    # ---------------------------------------------------------- segments

    @app.get("/api/segments", response_model=list[SegmentOut])
    async def list_segments(
        camera: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=2000),
        before: datetime | None = Query(default=None),
    ) -> list[SegmentOut]:
        records = (
            index.list_by_camera(camera, limit=limit, before=before)
            if camera is not None
            else index.list_all(limit=limit, before=before)
        )
        return [
            SegmentOut(
                id=r.path,
                camera_id=r.camera_id,
                path=r.path,
                started_at=r.started_at.isoformat(),
                duration_s=r.duration_s,
                size_bytes=r.size_bytes,
                codec=r.codec,
                protected=r.protected,
            )
            for r in records
        ]

    @app.post("/api/segments/{segment_path:path}/protect")
    async def set_segment_protected(
        segment_path: str,
        protected: bool = Query(default=True),
    ) -> dict[str, object]:
        if not index.set_protected(segment_path, protected):
            raise HTTPException(status_code=404, detail=f"segment {segment_path!r} not found")
        return {"path": segment_path, "protected": protected}

    @app.get("/api/segments/{segment_path:path}/video")
    async def stream_segment(segment_path: str, request: Request) -> Response:
        record = index.get_by_path(segment_path)
        if record is None:
            raise HTTPException(status_code=404, detail=f"segment {segment_path!r} not found")
        abs_path = config.storage.root / record.path
        if not abs_path.is_file():
            raise HTTPException(status_code=410, detail="segment file is no longer on disk")
        return _range_response(abs_path, request)

    # ---------------------------------------------------------- discovery proxy

    @app.get("/api/discovery/cameras")
    async def discovery_cameras() -> Response:
        """Pass through to the capture service's discovery sidecar.

        Enumeration can take a few seconds per OAK (each device is
        briefly opened and closed to read its MxID), so we use a long
        timeout here. Anything shorter and adding a camera fails with
        a spurious "discovery unavailable" error.
        """
        url = f"{discovery_url}/discovery/cameras"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(url)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"discovery sidecar unreachable at {url}: {exc}",
            ) from exc
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    @app.get("/api/discovery/preview.mjpeg")
    async def discovery_preview() -> StreamingResponse:
        """Stream the MJPEG preview from capture by proxying bytes through.

        `multipart/x-mixed-replace` is an HTTP streaming pattern that
        never terminates until the client disconnects. We use httpx's
        streaming context and forward chunks as they arrive — no
        buffering, no framing changes.
        """
        url = f"{discovery_url}/discovery/preview.mjpeg"
        client = httpx.AsyncClient(timeout=None)

        async def _relay() -> AsyncIterator[bytes]:
            try:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", errors="replace")
                        log.error(
                            "discovery preview upstream returned %d: %s",
                            resp.status_code,
                            detail,
                        )
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            finally:
                await client.aclose()

        return StreamingResponse(
            _relay(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.post("/api/cameras/{camera_id}/rotate", response_model=MutationResult)
    async def rotate_camera(camera_id: str) -> MutationResult:
        """Toggle camera rotation between 0° and 180° and auto-reset.

        Simpler than asking the user to edit the camera form: one
        button, two states. We update the DB row, then kick the
        capture sidecar's reset endpoint so the supervisor restarts
        the camera and picks up the new rotation_degrees value. The
        ~5-10s gap that reset produces is the only visible downtime.
        """
        cam = store.get(camera_id)
        if cam is None:
            raise HTTPException(
                status_code=404, detail=f"camera {camera_id!r} not found"
            )
        new_rotation = 0 if cam.rotation_degrees == 180 else 180
        updated = cam.model_copy(update={"rotation_degrees": new_rotation})
        store.update(updated)

        # Fire-and-forget the reset request against capture. Failures
        # here are non-fatal — the rotation is already persisted and
        # will take effect on the next supervisor restart even if we
        # can't reach the sidecar right now.
        reset_url = f"{discovery_url}/live/{camera_id}/reset"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(reset_url)
        except httpx.RequestError as exc:
            log.warning(
                "rotation saved for %s but sidecar reset failed: %s",
                camera_id,
                exc,
            )

        return MutationResult(camera=_camera_to_out(updated))

    @app.post("/api/cameras/{camera_id}/reset")
    async def reset_camera_endpoint(camera_id: str) -> Response:
        """Proxy through to the capture sidecar's reset endpoint.

        Triggers `camera.stop()` on the running DepthAICamera, which
        exits the recording pipeline and lets the supervisor's
        restart-on-crash loop bring it back up after the normal
        backoff. Returns as soon as capture acks the request — the
        actual restart takes ~5-10 seconds during which the live
        preview tile will show the last cached frame.
        """
        url = f"{discovery_url}/live/{camera_id}/reset"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"capture sidecar unreachable at {url}: {exc}",
            ) from exc
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    @app.get("/api/live/{camera_id}/preview.mjpeg")
    async def live_preview(camera_id: str) -> StreamingResponse:
        """Proxy the live MJPEG preview of a recording camera.

        Same streaming-passthrough strategy as the discovery preview
        proxy — tail the upstream chunks as they arrive. Capture's
        dual-output pipeline means this never opens a new device,
        so it's safe and fast even for cameras that are actively
        recording.
        """
        url = f"{discovery_url}/live/{camera_id}/preview.mjpeg"
        client = httpx.AsyncClient(timeout=None)

        async def _relay_live() -> AsyncIterator[bytes]:
            try:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", errors="replace")
                        log.error(
                            "live preview upstream returned %d for %s: %s",
                            resp.status_code,
                            camera_id,
                            detail,
                        )
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            finally:
                await client.aclose()

        return StreamingResponse(
            _relay_live(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    # ---------------------------------------------------------- system

    @app.post("/api/system/shutdown")
    async def shutdown_host() -> dict[str, str]:
        """Gracefully shut down the Pi host.

        Spawns a one-shot privileged container that calls `shutdown`
        on the host via PID namespace sharing. The 3-second sleep
        gives time for the HTTP 200 response to reach the browser
        before the host goes down.

        Requires `/var/run/docker.sock` to be mounted into this
        container (see docker-compose.yml).
        """
        try:
            import docker as docker_sdk

            client = docker_sdk.from_env()
            client.containers.run(
                "alpine",
                'sh -c "sleep 3 && nsenter -t 1 -m -u -i -n -- shutdown -h now"',
                pid_mode="host",
                privileged=True,
                remove=True,
                detach=True,
            )
        except Exception as exc:
            log.exception("shutdown failed")
            raise HTTPException(
                status_code=503,
                detail=f"shutdown failed: {exc}",
            ) from exc
        return {"status": "shutting_down"}

    # Frontend static files must be mounted **last** — FastAPI matches
    # mounts before later route declarations, so a `/` mount would shadow
    # any API route registered after it. `html=True` makes StaticFiles
    # serve `index.html` for directory requests, which is enough for the
    # current no-router SPA.
    if frontend_dist is not None:
        if not frontend_dist.is_dir():
            log.warning("frontend_dist=%s does not exist; skipping static mount", frontend_dist)
        else:
            log.info("serving frontend bundle from %s", frontend_dist)
            app.mount(
                "/",
                StaticFiles(directory=frontend_dist, html=True),
                name="frontend",
            )

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _camera_to_out(cam: CameraConfig) -> CameraOut:
    return CameraOut(
        id=cam.id,
        mxid=cam.mxid,
        role=cam.role,
        resolution=cam.resolution,
        fps=cam.fps,
        codec=cam.codec,
        bitrate_kbps=cam.bitrate_kbps,
        rotation_degrees=cam.rotation_degrees,
    )


def _range_response(path: Path, request: Request) -> Response:
    """Serve a file with HTTP Range support.

    Browser `<video>` elements pretty much require range requests — without
    them you can't seek, and QuickTime will refuse to start streaming at all
    until it has the moov atom it wants. Implements the minimum that
    satisfies them (single-range `bytes=` + 206 Partial Content).
    """
    size = path.stat().st_size
    mime, _ = mimetypes.guess_type(path.name)
    content_type = mime or "application/octet-stream"

    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header is None:
        return _full_response(path, size, content_type)

    try:
        start, end = _parse_range(range_header, size)
    except ValueError:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}"},
        )

    length = end - start + 1

    def _iter_range() -> object:
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            chunk_size = 64 * 1024
            while remaining > 0:
                chunk = fh.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(
        _iter_range(),  # type: ignore[arg-type]
        status_code=206,
        media_type=content_type,
        headers=headers,
    )


def _full_response(path: Path, size: int, content_type: str) -> StreamingResponse:
    def _iter_all() -> object:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _iter_all(),  # type: ignore[arg-type]
        media_type=content_type,
        headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
    )


def _parse_range(header: str, size: int) -> tuple[int, int]:
    """Parse a `bytes=start-end` header into inclusive absolute byte offsets."""
    if not header.startswith("bytes="):
        raise ValueError("unsupported range unit")
    spec = header[len("bytes=") :].strip()
    if "," in spec:
        # Multi-range requests are rare and we don't bother supporting them.
        raise ValueError("multi-range not supported")
    if "-" not in spec:
        raise ValueError("malformed range")
    start_s, end_s = spec.split("-", 1)

    if start_s == "" and end_s != "":
        # Suffix range: last N bytes.
        suffix = int(end_s)
        if suffix <= 0:
            raise ValueError("invalid suffix length")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1

    if start < 0 or end >= size or start > end:
        raise ValueError("range out of bounds")
    return start, end


# Make sure `.mp4` is a known mime type even on minimal systems that don't
# bring any user-level mime.types file along.
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/H265", ".h265")
mimetypes.add_type("video/H264", ".h264")
