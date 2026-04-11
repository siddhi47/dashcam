from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
from oak_dashcam_capture.camera import EncodedFrame
from oak_dashcam_capture.sinks import FfmpegMp4Sink, RawBitstreamSink
from oak_dashcam_shared.config import Codec


def _frame(data: bytes = b"hello", *, keyframe: bool = True) -> EncodedFrame:
    return EncodedFrame(data=data, pts_us=0, keyframe=keyframe)


# ---------------------------------------------------------------------------
# RawBitstreamSink
# ---------------------------------------------------------------------------


def test_raw_sink_extension_matches_codec() -> None:
    assert RawBitstreamSink(Codec.H265).extension == "h265"
    assert RawBitstreamSink(Codec.H264).extension == "h264"


def test_raw_sink_writes_concatenated_bytes(tmp_path: Path) -> None:
    sink = RawBitstreamSink(Codec.H265)
    out = tmp_path / "seg.h265"
    sink.open(out)
    sink.write(_frame(b"AAA"))
    sink.write(_frame(b"BBBB"))
    sink.close()
    assert out.read_bytes() == b"AAABBBB"


def test_raw_sink_creates_parent_directories(tmp_path: Path) -> None:
    sink = RawBitstreamSink(Codec.H265)
    out = tmp_path / "nested" / "dir" / "seg.h265"
    sink.open(out)
    sink.write(_frame(b"X"))
    sink.close()
    assert out.exists()


def test_raw_sink_double_open_raises(tmp_path: Path) -> None:
    sink = RawBitstreamSink(Codec.H265)
    sink.open(tmp_path / "a.h265")
    with pytest.raises(RuntimeError, match="already open"):
        sink.open(tmp_path / "b.h265")
    sink.close()


def test_raw_sink_write_before_open_raises() -> None:
    sink = RawBitstreamSink(Codec.H265)
    with pytest.raises(RuntimeError, match="before open"):
        sink.write(_frame())


def test_raw_sink_close_is_idempotent(tmp_path: Path) -> None:
    sink = RawBitstreamSink(Codec.H265)
    sink.open(tmp_path / "a.h265")
    sink.close()
    sink.close()  # second close is a no-op, must not raise


# ---------------------------------------------------------------------------
# FfmpegMp4Sink (subprocess mocked)
# ---------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.closed:
            raise BrokenPipeError
        self.buffer.extend(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class _FakePopen:
    instances: ClassVar[list[_FakePopen]] = []

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        type(self).instances.append(self)
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdin: _FakeStdin | None = _FakeStdin()
        self.stderr: io.BytesIO | None = io.BytesIO()
        self.returncode = 0
        self.killed = False
        self._final_path: Path | None = None
        # Mimic ffmpeg by creating the target file on "successful" run.
        # The command ends with `-y <path>`.
        if "-y" in cmd:
            idx = cmd.index("-y")
            self._final_path = Path(cmd[idx + 1])

    def wait(self, timeout: float | None = None) -> int:
        # Simulate ffmpeg finishing: create the output file so the sink's
        # atomic rename has something to rename.
        if self._final_path is not None and self.returncode == 0:
            self._final_path.parent.mkdir(parents=True, exist_ok=True)
            self._final_path.write_bytes(b"FAKE_MP4_BYTES")
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    _FakePopen.instances = []
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_ffmpeg_sink_spawns_with_expected_arguments(
    fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(tmp_path / "seg.mp4")

    assert len(fake_popen.instances) == 1
    cmd = fake_popen.instances[0].cmd
    assert cmd[0] == "ffmpeg"
    assert "hevc" in cmd
    assert "-r" in cmd and "30" in cmd
    assert "copy" in cmd  # -c copy → no re-encode
    assert "+faststart" in cmd  # moov atom at front
    # Output file is the .mp4.tmp form during writing.
    assert cmd[-1].endswith(".mp4.tmp")
    sink.close()


def test_ffmpeg_sink_uses_h264_format_for_h264_codec(
    fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    sink = FfmpegMp4Sink(Codec.H264, fps=60)
    sink.open(tmp_path / "seg.mp4")
    cmd = fake_popen.instances[0].cmd
    assert "h264" in cmd
    assert "hevc" not in cmd
    sink.close()


def test_ffmpeg_sink_passes_explicit_output_format(
    fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    # The temp file name ends in `.mp4.tmp`, which ffmpeg cannot infer a
    # muxer from — regression guard for "Unable to choose an output format".
    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(tmp_path / "seg.mp4")
    cmd = fake_popen.instances[0].cmd
    # Two `-f` flags: one for the input (`hevc`/`h264`), one for output (`mp4`).
    f_indices = [i for i, arg in enumerate(cmd) if arg == "-f"]
    assert len(f_indices) == 2
    assert cmd[f_indices[1] + 1] == "mp4"
    sink.close()


def test_ffmpeg_sink_starts_new_session_to_isolate_from_sigint(
    fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    # Regression guard: without `start_new_session=True`, a Ctrl-C on the
    # parent terminal hits the ffmpeg subprocess too, and ffmpeg exits
    # without writing the moov atom, leaving every .mp4.tmp unplayable.
    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(tmp_path / "seg.mp4")
    kwargs = fake_popen.instances[0].kwargs
    assert kwargs.get("start_new_session") is True
    sink.close()


def test_ffmpeg_sink_pipes_frame_data_to_stdin(
    fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(tmp_path / "seg.mp4")
    sink.write(_frame(b"first"))
    sink.write(_frame(b"second"))
    sink.close()

    assert fake_popen.instances[0].stdin is not None
    assert bytes(fake_popen.instances[0].stdin.buffer) == b"firstsecond"


def test_ffmpeg_sink_renames_temp_to_final_on_success(
    fake_popen: type[_FakePopen], tmp_path: Path
) -> None:
    final = tmp_path / "seg.mp4"
    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(final)
    sink.write(_frame(b"x"))
    sink.close()

    assert final.exists()
    assert not final.with_suffix(".mp4.tmp").exists()


def test_ffmpeg_sink_leaves_temp_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakePopen.instances = []

    class _FailingPopen(_FakePopen):
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            super().__init__(cmd, **kwargs)
            self.stderr = io.BytesIO(b"[hevc @ 0x1234] invalid NAL unit size\n")

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 1
            if self._final_path is not None:
                self._final_path.parent.mkdir(parents=True, exist_ok=True)
                self._final_path.write_bytes(b"partial")
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", _FailingPopen)

    final = tmp_path / "seg.mp4"
    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(final)
    sink.close()

    assert not final.exists()
    assert final.with_suffix(".mp4.tmp").exists()


def test_ffmpeg_sink_kills_on_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakePopen.instances = []

    class _HangingPopen(_FakePopen):
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            super().__init__(cmd, **kwargs)
            self._first_call = True

        def wait(self, timeout: float | None = None) -> int:
            if self._first_call and timeout is not None:
                self._first_call = False
                raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", _HangingPopen)

    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(tmp_path / "seg.mp4")
    sink.close()

    proc = _HangingPopen.instances[0]
    assert proc.killed


def test_ffmpeg_sink_drops_frames_silently_after_broken_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakePopen.instances = []

    class _BrokenStdin(_FakeStdin):
        def write(self, data: bytes) -> int:
            raise BrokenPipeError

    class _BrokenPipePopen(_FakePopen):
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            super().__init__(cmd, **kwargs)
            self.stdin = _BrokenStdin()

    monkeypatch.setattr(subprocess, "Popen", _BrokenPipePopen)

    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(tmp_path / "seg.mp4")
    # First write breaks the pipe; subsequent writes must be silent no-ops,
    # not raise, and not continue trying to write to the dead process.
    sink.write(_frame(b"aaa"))
    sink.write(_frame(b"bbb"))
    sink.write(_frame(b"ccc"))
    sink.close()


def test_ffmpeg_sink_write_before_open_raises() -> None:
    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    with pytest.raises(RuntimeError, match="before open"):
        sink.write(_frame())


def test_ffmpeg_sink_close_is_idempotent(fake_popen: type[_FakePopen], tmp_path: Path) -> None:
    sink = FfmpegMp4Sink(Codec.H265, fps=30)
    sink.open(tmp_path / "seg.mp4")
    sink.close()
    sink.close()  # must not raise or spawn another process
    assert len(fake_popen.instances) == 1
