from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class Resolution(StrEnum):
    R_720P = "720p"
    R_1080P = "1080p"
    R_4K = "4k"


class Codec(StrEnum):
    H264 = "h264"
    H265 = "h265"


class CameraRole(StrEnum):
    FRONT = "front"
    REAR = "rear"
    CABIN = "cabin"
    LEFT = "left"
    RIGHT = "right"


class StorageConfig(BaseModel):
    root: Path = Field(default=Path("/data/dashcam"))
    retention_gb: int = Field(default=200, ge=1)
    segment_seconds: int = Field(default=60, ge=5, le=600)


class CameraConfig(BaseModel):
    id: str
    mxid: str = "auto"
    role: CameraRole
    resolution: Resolution = Resolution.R_1080P
    fps: int = Field(default=30, ge=1, le=60)
    codec: Codec = Codec.H265
    bitrate_kbps: int = Field(default=8000, ge=500, le=50000)
    # Mount rotation applied to both recording and live preview, in
    # degrees. Only 0 and 180 are supported — 180 handles the common
    # "camera mounted upside-down on the ceiling" case. 90°/270° would
    # require `ImageManip` rotation on the full-res video path, which
    # costs VPU cycles and risks the two-camera X_LINK_ERROR issues we
    # fixed earlier, so we deliberately don't offer them.
    rotation_degrees: Literal[0, 180] = 0

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("camera id must be a non-empty slug (alnum, -, _)")
        return v


class AuthConfig(BaseModel):
    mode: Literal["none", "basic", "token"] = "none"
    username: str | None = None
    password: str | None = None
    token: str | None = None


class WebappConfig(BaseModel):
    bind: str = "0.0.0.0:8080"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    live_preview: bool = True


class LoggingConfig(BaseModel):
    level: Literal["debug", "info", "warning", "error"] = "info"


class DashcamConfig(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    cameras: list[CameraConfig]
    webapp: WebappConfig = Field(default_factory=WebappConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("cameras")
    @classmethod
    def _unique_camera_ids(cls, v: list[CameraConfig]) -> list[CameraConfig]:
        ids = [c.id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("camera ids must be unique")
        return v


def load_config(path: str | Path) -> DashcamConfig:
    with Path(path).open("r") as fh:
        raw = yaml.safe_load(fh)
    return DashcamConfig.model_validate(raw)
