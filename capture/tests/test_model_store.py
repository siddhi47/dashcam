"""Tests for model_store.resolve_model — the S3-latest / local-cache / no-model
fallback chain. No real S3: a fake client duck-types the two boto3 calls the
module makes (`list_objects_v2`, `download_file`).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oak_dashcam_capture.model_store import resolve_model
from oak_dashcam_shared.config import DetectionConfig, S3ModelSource


class _FakeS3Client:
    """Duck-type for boto3's S3 client covering list_objects_v2 + download_file."""

    def __init__(
        self,
        objects: list[dict[str, Any]],
        *,
        page_size: int | None = None,
        fail: bool = False,
    ) -> None:
        self._objects = objects
        self._page_size = page_size if page_size is not None else len(objects) or 1
        self._fail = fail
        self.downloads: list[str] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError("simulated S3 outage")
        start = int(kwargs.get("ContinuationToken", "0"))
        page = self._objects[start : start + self._page_size]
        truncated = start + self._page_size < len(self._objects)
        result: dict[str, Any] = {"Contents": page, "IsTruncated": truncated}
        if truncated:
            result["NextContinuationToken"] = str(start + self._page_size)
        return result

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        if self._fail:
            raise RuntimeError("simulated S3 outage")
        self.downloads.append(key)
        Path(filename).write_bytes(b"x" * self._size_of(key))

    def _size_of(self, key: str) -> int:
        return next(int(o["Size"]) for o in self._objects if o["Key"] == key)


def _s3_obj(key: str, *, size: int, ts: datetime) -> dict[str, Any]:
    return {"Key": key, "Size": size, "LastModified": ts}


def _cfg(**kwargs: Any) -> DetectionConfig:
    return DetectionConfig.model_validate(kwargs)


S3 = S3ModelSource(bucket="models", prefix="dashcam/")


def test_disabled_returns_none_without_warnings(tmp_path: Path, caplog: object) -> None:
    cfg = _cfg(enabled=False)
    assert resolve_model(cfg, tmp_path) is None


def test_no_s3_no_local_warns_and_returns_none(tmp_path: Path, caplog: Any) -> None:
    with caplog.at_level(logging.WARNING):
        result = resolve_model(_cfg(), tmp_path)
    assert result is None
    assert "no s3 source is configured" in caplog.text


def test_local_only_picks_newest_by_mtime(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    old = models / "yolo-v1.tar.xz"
    new = models / "yolo-v2.tar.xz"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    assert resolve_model(_cfg(), tmp_path) == new


def test_s3_downloads_latest_by_last_modified(tmp_path: Path) -> None:
    client = _FakeS3Client(
        [
            _s3_obj("dashcam/yolo-a.tar.xz", size=10, ts=datetime(2026, 1, 1, tzinfo=UTC)),
            _s3_obj("dashcam/yolo-b.tar.xz", size=20, ts=datetime(2026, 6, 1, tzinfo=UTC)),
            _s3_obj("dashcam/README.md", size=5, ts=datetime(2026, 7, 1, tzinfo=UTC)),
        ],
        page_size=1,  # exercise pagination
    )
    cfg = _cfg(s3=S3.model_dump())
    result = resolve_model(cfg, tmp_path, s3_client=client)
    assert result == tmp_path / "models" / "yolo-b.tar.xz"
    assert client.downloads == ["dashcam/yolo-b.tar.xz"]
    assert result is not None and result.stat().st_size == 20


def test_s3_skips_download_when_cached_copy_matches(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "yolo.tar.xz").write_bytes(b"x" * 20)

    client = _FakeS3Client(
        [_s3_obj("dashcam/yolo.tar.xz", size=20, ts=datetime(2026, 1, 1, tzinfo=UTC))]
    )
    result = resolve_model(_cfg(s3=S3.model_dump()), tmp_path, s3_client=client)
    assert result == models / "yolo.tar.xz"
    assert client.downloads == []


def test_s3_redownloads_when_size_differs(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "yolo.tar.xz").write_bytes(b"x" * 5)  # stale partial copy

    client = _FakeS3Client(
        [_s3_obj("dashcam/yolo.tar.xz", size=20, ts=datetime(2026, 1, 1, tzinfo=UTC))]
    )
    result = resolve_model(_cfg(s3=S3.model_dump()), tmp_path, s3_client=client)
    assert client.downloads == ["dashcam/yolo.tar.xz"]
    assert result is not None and result.stat().st_size == 20


def test_s3_failure_falls_back_to_local_cache(tmp_path: Path, caplog: Any) -> None:
    models = tmp_path / "models"
    models.mkdir()
    cached = models / "yolo-cached.tar.xz"
    cached.write_bytes(b"cached")

    client = _FakeS3Client([], fail=True)
    with caplog.at_level(logging.WARNING):
        result = resolve_model(_cfg(s3=S3.model_dump()), tmp_path, s3_client=client)
    assert result == cached
    assert "falling back to locally cached models" in caplog.text


def test_s3_failure_without_local_cache_warns_and_returns_none(tmp_path: Path, caplog: Any) -> None:
    client = _FakeS3Client([], fail=True)
    with caplog.at_level(logging.WARNING):
        result = resolve_model(_cfg(s3=S3.model_dump()), tmp_path, s3_client=client)
    assert result is None
    assert "s3 sync failed and no model is cached locally" in caplog.text


def test_s3_empty_bucket_without_local_cache_warns_and_returns_none(
    tmp_path: Path, caplog: Any
) -> None:
    client = _FakeS3Client([])
    with caplog.at_level(logging.WARNING):
        result = resolve_model(_cfg(s3=S3.model_dump()), tmp_path, s3_client=client)
    assert result is None
    assert "recording without object detection" in caplog.text


def test_explicit_model_dir_overrides_default(tmp_path: Path) -> None:
    custom = tmp_path / "custom-models"
    custom.mkdir()
    model = custom / "yolo.tar.xz"
    model.write_bytes(b"m")

    cfg = _cfg(model_dir=str(custom))
    assert resolve_model(cfg, tmp_path) == model
