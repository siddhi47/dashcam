"""Resolve the YOLO detection model to run, syncing from S3 when configured.

The detection model is a DepthAI NNArchive — a `.tar.xz` bundle holding
the compiled MyriadX blob plus the decoding config (anchors, labels,
input size). Archives live in an S3 bucket (or any S3-compatible store)
and are cached locally under `model_dir` so the Pi can boot and run
detection without network access.

Resolution order (see `resolve_model`):

1. Detection disabled in config → no model, no warnings.
2. S3 source configured → list the bucket, pick the newest archive by
   `LastModified`, download it unless an identical copy (same key, same
   size) is already cached. Any S3 failure — no credentials, no
   network, bad bucket — logs a warning and falls through to (3); it
   must never prevent recording from starting.
3. Newest locally cached archive, by mtime.
4. Nothing found → warn (with a message that says *why*: no S3
   configured vs. S3 failed) and return None; capture runs without
   detection.

boto3 is imported lazily inside the sync function so the capture
service doesn't pay the import cost (or require the dependency to work)
when detection is disabled or S3 is unconfigured.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from oak_dashcam_shared.config import DetectionConfig, S3ModelSource

log = logging.getLogger(__name__)

# NNArchive files as produced by Luxonis tooling (blobconverter / hub
# exports) are .tar.xz archives. Anything else in the bucket prefix
# (readme, checksums, old .blob files) is ignored.
_MODEL_SUFFIX = ".tar.xz"


def resolve_model(
    cfg: DetectionConfig,
    storage_root: Path,
    *,
    s3_client: Any | None = None,
) -> Path | None:
    """Return the path of the NNArchive to load, or None to run without detection.

    `s3_client` lets tests inject a fake; production leaves it None and
    a real boto3 client is built from the config + ambient AWS
    credentials.
    """
    if not cfg.enabled:
        log.info("detection disabled in config; recording without object detection")
        return None

    model_dir = cfg.model_dir if cfg.model_dir is not None else storage_root / "models"
    model_dir.mkdir(parents=True, exist_ok=True)

    s3_failed = False
    if cfg.s3 is not None:
        try:
            _sync_latest_from_s3(cfg.s3, model_dir, s3_client=s3_client)
        except Exception as exc:
            s3_failed = True
            log.warning(
                "detection model sync from s3://%s/%s failed (%s); "
                "falling back to locally cached models in %s",
                cfg.s3.bucket,
                cfg.s3.prefix,
                exc,
                model_dir,
            )

    local = _latest_local_model(model_dir)
    if local is not None:
        log.info("detection model resolved: %s", local)
        return local

    if cfg.s3 is None:
        log.warning(
            "detection enabled but no model found locally in %s and no s3 source "
            "is configured; recording without object detection "
            "(set detection.s3 in dashcam.yaml or drop an NNArchive %s file into %s)",
            model_dir,
            _MODEL_SUFFIX,
            model_dir,
        )
    elif s3_failed:
        log.warning(
            "detection enabled but s3 sync failed and no model is cached locally "
            "in %s; recording without object detection",
            model_dir,
        )
    else:
        log.warning(
            "detection enabled but s3://%s/%s contains no %s archives and no model "
            "is cached locally in %s; recording without object detection",
            cfg.s3.bucket,
            cfg.s3.prefix,
            _MODEL_SUFFIX,
            model_dir,
        )
    return None


def _latest_local_model(model_dir: Path) -> Path | None:
    """Newest cached archive by mtime — download time, which tracks S3 recency."""
    candidates = [p for p in model_dir.glob(f"*{_MODEL_SUFFIX}") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _build_s3_client(source: S3ModelSource) -> Any:
    import boto3

    return boto3.client(
        "s3",
        region_name=source.region,
        endpoint_url=source.endpoint_url,
    )


def _sync_latest_from_s3(
    source: S3ModelSource,
    model_dir: Path,
    *,
    s3_client: Any | None = None,
) -> None:
    """Download the newest model archive from S3 into `model_dir` if needed."""
    client = s3_client if s3_client is not None else _build_s3_client(source)

    latest: dict[str, Any] | None = None
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": source.bucket, "Prefix": source.prefix}
        if token is not None:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(_MODEL_SUFFIX):
                continue
            if latest is None or obj["LastModified"] > latest["LastModified"]:
                latest = obj
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")

    if latest is None:
        log.warning(
            "no %s model archives found under s3://%s/%s",
            _MODEL_SUFFIX,
            source.bucket,
            source.prefix,
        )
        return

    key: str = latest["Key"]
    target = model_dir / Path(key).name

    # Same key + same byte size → assume unchanged. ETag comparison would
    # be stricter but is unreliable for multipart uploads; key+size is
    # plenty for "did someone upload a new model" purposes.
    if target.is_file() and target.stat().st_size == latest["Size"]:
        log.info("detection model %s already cached at %s; skipping download", key, target)
        return

    log.info(
        "downloading detection model s3://%s/%s (%d bytes) -> %s",
        source.bucket,
        key,
        latest["Size"],
        target,
    )
    # Download to a temp name and rename so a crash mid-download never
    # leaves a truncated archive where `_latest_local_model` would find it.
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        client.download_file(source.bucket, key, str(tmp))
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
