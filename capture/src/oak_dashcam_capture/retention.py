"""Loop-delete retention for recorded segments.

Runs as a background task alongside the camera supervisors. On a fixed
interval it scans `{storage.root}/{camera_id}/{date}/` for segment files,
sums their sizes, and if the total exceeds `retention_gb` it deletes the
oldest files until it's back under the limit.

Two kinds of files are protected from deletion:

1. **The most recent segment per camera** — so we never race with ffmpeg
   finishing the currently-open file.
2. **Incident clips** — any segment flagged `protected=1` in the
   `SegmentIndex`. This is how the "mark as incident" feature from
   CLAUDE.md survives loop-delete.

When retention deletes a file from disk, it also deletes the matching row
from the index (if an index is attached) so the database doesn't keep
pointing at a file that no longer exists.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from oak_dashcam_shared.segment_index import SegmentIndex

log = logging.getLogger(__name__)


_SEGMENT_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".h265", ".h264"})


@dataclass(frozen=True, slots=True)
class _Segment:
    path: Path
    size: int
    mtime: float
    camera_id: str
    relative: str


class RetentionManager:
    """Enforces a total-disk-usage budget over segment files."""

    def __init__(
        self,
        root: Path,
        max_bytes: int,
        *,
        scan_interval_s: float = 30.0,
        index: SegmentIndex | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if scan_interval_s <= 0:
            raise ValueError("scan_interval_s must be positive")
        self._root = root
        self._max_bytes = max_bytes
        self._scan_interval = scan_interval_s
        self._index = index
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        """Periodic scan loop. Blocks until `stop()` is called."""
        log.info(
            "retention watching %s (limit %.1f GB, scan every %.0fs)",
            self._root,
            self._max_bytes / (1024**3),
            self._scan_interval,
        )
        while not self._stop_event.is_set():
            try:
                await asyncio.to_thread(self.enforce)
            except Exception:
                log.exception("retention scan failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._scan_interval,
                )
                return
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop_event.set()

    def enforce(self) -> None:
        """One synchronous scan-and-delete pass.

        Exposed as a public method so tests can drive it directly without
        spinning up an event loop or waiting out the scan interval.
        """
        segments = self._collect_segments()
        total_bytes = sum(s.size for s in segments)
        if total_bytes <= self._max_bytes:
            return

        # Sort oldest first, then compute the set of protected paths.
        segments.sort(key=lambda s: s.mtime)
        protected = self._latest_per_camera(segments)
        protected.update(self._index_protected_paths())

        deleted_bytes = 0
        deleted_count = 0
        remaining = total_bytes
        for seg in segments:
            if remaining <= self._max_bytes:
                break
            if seg.path in protected:
                continue
            try:
                seg.path.unlink()
            except OSError:
                log.exception("failed to delete %s", seg.path)
                continue
            deleted_bytes += seg.size
            deleted_count += 1
            remaining -= seg.size
            self._forget_in_index(seg.relative)

        if deleted_count:
            log.info(
                "retention: deleted %d segment(s), %.1f MB — %.1f MB / %.1f MB remaining",
                deleted_count,
                deleted_bytes / (1024**2),
                remaining / (1024**2),
                self._max_bytes / (1024**2),
            )

        if remaining > self._max_bytes:
            log.warning(
                "retention: still over limit after deleting everything deletable "
                "(%.1f MB > %.1f MB); latest-per-camera and protected clips cannot be removed",
                remaining / (1024**2),
                self._max_bytes / (1024**2),
            )

    def _index_protected_paths(self) -> set[Path]:
        """Absolute paths of all incident-marked clips, from the index."""
        if self._index is None:
            return set()
        try:
            relative_paths = self._index.protected_paths()
        except Exception:
            log.exception("retention: failed to load protected paths from index")
            return set()
        return {self._root / rel for rel in relative_paths}

    def _forget_in_index(self, relative: str) -> None:
        if self._index is None:
            return
        try:
            self._index.delete_by_path(relative)
        except Exception:
            log.exception("retention: failed to drop index row for %s", relative)

    def _collect_segments(self) -> list[_Segment]:
        result: list[_Segment] = []
        if not self._root.exists():
            return result
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in _SEGMENT_EXTENSIONS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            try:
                rel = path.relative_to(self._root)
            except ValueError:
                continue
            if len(rel.parts) < 2:
                # Must live under `{camera_id}/...` — skip loose files at
                # the storage root, they aren't ours.
                continue
            result.append(
                _Segment(
                    path=path,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    camera_id=rel.parts[0],
                    relative=str(rel),
                )
            )
        return result

    @staticmethod
    def _latest_per_camera(segments: list[_Segment]) -> set[Path]:
        latest: dict[str, _Segment] = {}
        for seg in segments:
            current = latest.get(seg.camera_id)
            if current is None or seg.mtime > current.mtime:
                latest[seg.camera_id] = seg
        return {s.path for s in latest.values()}
