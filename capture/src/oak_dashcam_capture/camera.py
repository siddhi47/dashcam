from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EncodedFrame:
    """A single encoded video frame emitted by a camera.

    `data` is the raw encoded bitstream (H.264/H.265 NAL units) ready to be
    muxed into a container — never re-encoded on the Pi CPU. `pts_us` is a
    monotonic presentation timestamp in microseconds, relative to the camera's
    stream start. `keyframe` marks IDR frames so segment writers can split on
    clean boundaries.
    """

    data: bytes
    pts_us: int
    keyframe: bool


@runtime_checkable
class Camera(Protocol):
    """Abstract camera producing a stream of encoded frames.

    Implementations must be safe to `start()` / `stop()` exactly once per
    instance. `frames()` returns an async iterator that terminates cleanly
    after `stop()` is called.
    """

    @property
    def camera_id(self) -> str: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def frames(self) -> AsyncIterator[EncodedFrame]: ...
