"""Segment sinks: pluggable backends for writing encoded frames to disk.

The `SegmentWriter` owns rotation and scheduling; a `SegmentSink` owns the
"what does a segment file actually look like on disk" question. This split
exists because the answer to that question changed — we started by writing
raw Annex-B bitstreams (diagnostic, unplayable without ffmpeg) and we now
want real MP4 files by default. Keeping the writer sink-agnostic means
there's a single, tested rotation implementation regardless of format.

Two implementations here:

* `RawBitstreamSink` — concatenates encoded NAL units into a file with the
  codec name as extension (`.h265` / `.h264`). Unplayable in most players
  without a format hint; kept because it's deterministic, has no external
  dependencies, and is what the unit tests drive.

* `FfmpegMp4Sink` — spawns one `ffmpeg` subprocess per segment, pipes the
  encoded bitstream into its stdin, and lets ffmpeg do the Annex-B → MP4
  muxing without re-encoding. Requires the `ffmpeg` binary on PATH. This
  is what real recording uses.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import IO, Protocol

from oak_dashcam_shared.config import Codec

from oak_dashcam_capture.camera import EncodedFrame

log = logging.getLogger(__name__)


class SegmentSink(Protocol):
    """Write encoded frames belonging to one segment to a file on disk."""

    extension: str

    def open(self, path: Path) -> None:
        """Begin a new segment at `path` (without the extension — the sink
        appends its own). Subsequent `write()` calls append to this segment
        until `close()` is called."""

    def write(self, frame: EncodedFrame) -> None:
        """Append one encoded frame to the current segment."""

    def close(self) -> None:
        """Finalize the current segment. Safe to call more than once."""


class RawBitstreamSink:
    """Concatenates encoded frames into a raw bitstream file.

    Produces `.h265` / `.h264` files that most players won't auto-detect,
    but that `ffmpeg -f hevc -i file` / `ffplay` can open. Useful as a
    fallback when `ffmpeg` isn't available and as a deterministic backend
    for unit tests that don't want to shell out.
    """

    def __init__(self, codec: Codec) -> None:
        self.extension = codec.value
        self._fh: object | None = None  # typed below via TYPE_CHECKING

    def open(self, path: Path) -> None:
        if self._fh is not None:
            raise RuntimeError("RawBitstreamSink already open")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("wb")
        log.info("opened raw segment %s", path)

    def write(self, frame: EncodedFrame) -> None:
        if self._fh is None:
            raise RuntimeError("RawBitstreamSink.write() called before open()")
        self._fh.write(frame.data)  # type: ignore[attr-defined]

    def close(self) -> None:
        if self._fh is None:
            return
        self._fh.close()  # type: ignore[attr-defined]
        self._fh = None


class FfmpegMp4Sink:
    """Muxes encoded Annex-B frames into an MP4 via a per-segment ffmpeg.

    Lifecycle per segment:

    1. `open(path)` spawns `ffmpeg -f <codec> -i - -c copy path.tmp`. The
       `.tmp` suffix is deliberate: the MP4 file is only valid once ffmpeg
       has written the moov atom on exit, so we keep partial files out of
       the visible filename space.
    2. `write(frame)` writes the encoded bytes straight into ffmpeg's stdin.
       No timestamp work — ffmpeg derives PTS from the `-r <fps>` flag.
    3. `close()` closes stdin, waits for ffmpeg to finalize, and atomically
       renames the temp file to the final `.mp4` path. If ffmpeg hangs, we
       SIGKILL after a timeout and leave the temp file in place for
       forensics.
    """

    extension = "mp4"

    def __init__(self, codec: Codec, fps: int) -> None:
        self._codec = codec
        self._fps = fps
        self._proc: subprocess.Popen[bytes] | None = None
        self._temp_path: Path | None = None
        self._final_path: Path | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_buffer: list[str] = []
        self._pipe_broken = False

    def open(self, path: Path) -> None:
        if self._proc is not None:
            raise RuntimeError("FfmpegMp4Sink already open")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")

        ffmpeg_format = "hevc" if self._codec == Codec.H265 else "h264"
        cmd = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-hide_banner",
            "-fflags",
            "+genpts",
            "-f",
            ffmpeg_format,
            "-r",
            str(self._fps),
            "-i",
            "-",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            # `.mp4.tmp` isn't a format ffmpeg can infer a muxer from, so
            # pass the output format explicitly rather than rely on the
            # extension.
            "-f",
            "mp4",
            "-y",
            str(temp),
        ]
        log.info("opening MP4 segment %s", path)
        # `start_new_session=True` puts ffmpeg in its own process group so
        # a Ctrl-C on our terminal doesn't propagate to it. Without this,
        # ffmpeg catches SIGINT and exits immediately without writing the
        # moov atom, leaving every .mp4.tmp file unplayable on shutdown.
        # Our `close()` still gives it a clean EOF via `stdin.close()`,
        # so it finalizes normally on intentional shutdown.
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._temp_path = temp
        self._final_path = path
        self._pipe_broken = False
        self._stderr_buffer = []
        self._stderr_thread = threading.Thread(
            target=self._pump_stderr,
            args=(self._proc.stderr,),
            name=f"ffmpeg-stderr-{path.name}",
            daemon=True,
        )
        self._stderr_thread.start()

    def _pump_stderr(self, stderr: IO[bytes] | None) -> None:
        """Log ffmpeg stderr in real time so failures surface immediately.

        Without this, a format mismatch at startup silently kills ffmpeg and
        every subsequent frame write hits `BrokenPipeError`; you only learn
        *why* ffmpeg died at segment close, up to 60 seconds later. Reading
        stderr on a background thread surfaces the actual error on the very
        first line ffmpeg prints.
        """
        if stderr is None:
            return
        for raw in iter(stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            self._stderr_buffer.append(line)
            log.error("ffmpeg: %s", line)

    def write(self, frame: EncodedFrame) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("FfmpegMp4Sink.write() called before open()")
        if self._pipe_broken:
            # ffmpeg already died; drop further frames silently until the
            # segment is closed so we don't spam the log once per frame.
            return
        try:
            self._proc.stdin.write(frame.data)
        except BrokenPipeError:
            self._pipe_broken = True
            log.error(
                "ffmpeg stdin broken after %d byte write; dropping remaining "
                "frames in this segment (see previous ffmpeg stderr lines)",
                len(frame.data),
            )

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                log.exception("error closing ffmpeg stdin")
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.error("ffmpeg did not finalize within 10s, killing")
            proc.kill()
            proc.wait()

        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)

        if proc.returncode != 0:
            log.error(
                "ffmpeg exited with code %s (%d stderr line(s) logged above)",
                proc.returncode,
                len(self._stderr_buffer),
            )

        if self._temp_path is not None and self._final_path is not None:
            if self._temp_path.exists() and proc.returncode == 0:
                self._temp_path.rename(self._final_path)
                log.info("closed MP4 segment %s", self._final_path)
            elif self._temp_path.exists():
                log.warning(
                    "leaving failed segment at %s for inspection",
                    self._temp_path,
                )

        self._proc = None
        self._temp_path = None
        self._final_path = None
        self._stderr_thread = None
        self._stderr_buffer = []
        self._pipe_broken = False
