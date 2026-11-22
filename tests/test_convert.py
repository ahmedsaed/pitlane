"""LIVE-13 / LIVE-14: live conversion only for squeezed SD and mid-recording format changes."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from conftest import needs_ffmpeg
from pitlane.convert import (
    FormatLocks,
    Formats,
    LiveConverter,
    Picture,
    Plan,
    StreamInfo,
    encoder_args,
    plan_for,
    probe_stream,
)
from pitlane.relay import ProcessTracker


def make_ts(path: Path, size: str, sar: str, seconds: int = 6, vcodec: str = "libx264", acodec: str = "aac") -> bytes:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-vf", f"setsar={sar}", "-c:v", vcodec, *(["-preset", "ultrafast"] if vcodec == "libx264" else ["-b:v", "3M"]), "-g", "25",
         "-c:a", acodec, "-f", "mpegts", str(path)],
        check=True,
    )  # fmt: skip
    return path.read_bytes()


async def chunked(data: bytes, n: int = 64 * 1024):
    for i in range(0, len(data), n):
        yield data[i : i + n]
        await asyncio.sleep(0)


async def run(data: bytes, **kw) -> tuple[bytes, LiveConverter, ProcessTracker]:
    tracker = ProcessTracker()
    conv = LiveConverter("test", tracker, **kw)
    out = b"".join([c async for c in conv.run(chunked(data))])
    return out, conv, tracker


def frames(data: bytes) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0"],
        input=data, capture_output=True, check=True,
    ).stdout.decode()  # fmt: skip
    return int(out.split()[0].strip(","))


def test_picture_rule():
    assert Picture(720, 576, "64:45").target == (1024, 576)
    assert Picture(544, 576, "32:17").target == (1024, 576)
    assert Picture(720, 576, "16:15").target == (768, 576)  # flagged 4:3 stays 4:3
    assert Picture(720, 576, "1:1").target is None and Picture(720, 576, "").target is None  # square: as stored
    assert Picture(1024, 576, "0:1").target is None
    assert Picture(1440, 1080, "4:3").target is None  # HD is never re-encoded


SD = Picture(720, 576, "64:45")
HD = Picture(1920, 1080, "1:1")
HEVC_EAC3 = Formats("hevc", "eac3", 6)
H264_AAC = Formats("h264", "aac", 2)


def test_plan():
    # Nothing to do: passthrough (also on a new recording, whatever its formats).
    assert plan_for(StreamInfo(HD, HEVC_EAC3), None) is None
    assert plan_for(StreamInfo(HD, HEVC_EAC3), HEVC_EAC3) is None
    assert plan_for(StreamInfo(Picture(1024, 576, "1:1"), H264_AAC), H264_AAC) is None  # resolution change, same codec
    # Squeezed SD only.
    assert plan_for(StreamInfo(SD, H264_AAC), None) == Plan("h264", (1024, 576), None)
    assert plan_for(StreamInfo(SD, H264_AAC), None, square_sd=False) is None
    # 2026-11-22: a recording that started as HEVC/E-AC-3 5.1 gets H.264/AAC SD after a stall.
    assert plan_for(StreamInfo(SD, H264_AAC), HEVC_EAC3) == Plan("hevc", (1024, 576), "eac3", 6)
    assert plan_for(StreamInfo(Picture(1024, 576, "1:1"), H264_AAC), HEVC_EAC3) == Plan("hevc", None, "eac3", 6)
    # Only the audio changed: copy the video.
    assert plan_for(StreamInfo(HD, Formats("hevc", "aac", 2)), HEVC_EAC3) == Plan(None, None, "eac3", 6)


def test_encoder_args():
    gpu = encoder_args(Plan("hevc", None, "eac3", 6), "/dev/dri/renderD128")
    assert gpu[gpu.index("-hwaccel") + 1] == "vaapi"  # decode on the GPU as well
    assert gpu[gpu.index("-c:v") + 1] == "hevc_vaapi" and gpu[gpu.index("-c:a") + 1] == "eac3"
    assert gpu[gpu.index("-ac") + 1] == "6"
    sq = encoder_args(Plan("hevc", (1024, 576), None), "/dev/dri/renderD128")
    assert "-hwaccel" not in sq and "-vaapi_device" in sq  # CPU decode + filters, GPU encode
    assert sq[sq.index("-vf") + 1].endswith("setsar=1,format=nv12,hwupload")
    assert sq[sq.index("-c:a") + 1] == "copy"
    cpu = encoder_args(Plan("h264", (1024, 576), None), None)
    assert cpu[cpu.index("-c:v") + 1] == "libx264" and "-vaapi_device" not in cpu
    audio_only = encoder_args(Plan(None, None, "eac3", 6), None)
    assert audio_only[audio_only.index("-c:v") + 1] == "copy"


def test_format_locks(monkeypatch):
    import pitlane.convert as conv

    locks = FormatLocks()
    assert locks.begin("f1") is None
    locks.lock("f1", HEVC_EAC3)
    locks.lock("f1", H264_AAC)  # the first formats stay
    assert locks.begin("f1") == HEVC_EAC3  # overlapping reconnect (Plex opens the new one first)
    locks.end("f1")
    locks.end("f1")
    assert locks.begin("f1") == HEVC_EAC3  # right after the previous one ended
    locks.end("f1")
    monkeypatch.setattr(conv, "INHERIT_S", 0.0)
    time.sleep(0.01)
    assert locks.begin("f1") is None  # later: a new recording with its own formats
    assert locks.begin("other") is None


@needs_ffmpeg
def test_squeezed_sd_becomes_square(tmp_path):
    data = make_ts(tmp_path / "in.ts", "720x576", "64/45")
    out, conv, tracker = asyncio.run(run(data))
    assert conv.picture == Picture(720, 576, "64:45")
    info = asyncio.run(probe_stream(out))
    assert info is not None and (info.picture.width, info.picture.height) == (1024, 576) and not info.picture.squeezed_sd
    assert frames(out) >= 145  # all 150 frames, including those held for the first check
    assert not tracker.live


@needs_ffmpeg
@pytest.mark.parametrize("size,sar", [("720x576", "1/1"), ("1280x720", "1/1")])
def test_other_pictures_pass_through_untouched(tmp_path, size, sar):
    data = make_ts(tmp_path / "in.ts", size, sar)
    out, conv, tracker = asyncio.run(run(data, locks=FormatLocks()))
    assert out == data
    assert conv.picture is not None and not conv.picture.squeezed_sd
    assert not tracker.live


@needs_ffmpeg
def test_continued_recording_keeps_its_formats(tmp_path):
    """A reconnect that brings other codecs is converted back to the recording's."""
    locks = FormatLocks()
    locks.begin("test")
    locks.lock("test", Formats("h264", "aac", 2))  # what the recording started with
    locks.end("test")
    data = make_ts(tmp_path / "in.ts", "640x360", "1/1", vcodec="mpeg2video", acodec="mp2")
    out, conv, tracker = asyncio.run(run(data, locks=locks))
    assert conv.lock == Formats("h264", "aac", 2) and conv.plan == Plan("h264", None, "aac", 2)
    info = asyncio.run(probe_stream(out))
    assert info is not None and info.formats == Formats("h264", "aac", 2)
    assert frames(out) >= 145
    assert not tracker.live


@needs_ffmpeg
def test_new_recording_is_not_converted(tmp_path, monkeypatch):
    import pitlane.convert as conv

    monkeypatch.setattr(conv, "INHERIT_S", 0.0)
    locks = FormatLocks()
    locks.begin("test")
    locks.lock("test", Formats("h264", "aac", 2))
    locks.end("test")
    time.sleep(0.01)
    data = make_ts(tmp_path / "in.ts", "640x360", "1/1", vcodec="mpeg2video", acodec="mp2")
    out, c, _ = asyncio.run(run(data, locks=locks))
    assert out == data and c.lock == Formats("mpeg2video", "mp2", 1)  # the test tone is mono


@needs_ffmpeg
def test_not_video_passes_through(tmp_path):
    data = b"\x47" + b"\x00" * 300_000
    out, conv, _ = asyncio.run(run(data))
    assert out == data and conv.picture is None
