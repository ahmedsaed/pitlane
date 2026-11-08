"""LIVE-13: squeezed SD is re-encoded to square pixels; everything else passes through."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from conftest import needs_ffmpeg
from pitlane.relay import ProcessTracker
from pitlane.square import Picture, SquarePixels, probe_picture


def make_ts(path: Path, size: str, sar: str, seconds: int = 6) -> bytes:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-vf", f"setsar={sar}", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
         "-c:a", "aac", "-f", "mpegts", str(path)],
        check=True,
    )  # fmt: skip
    return path.read_bytes()


async def chunked(data: bytes, n: int = 64 * 1024):
    for i in range(0, len(data), n):
        yield data[i : i + n]
        await asyncio.sleep(0)


async def run(data: bytes) -> tuple[bytes, SquarePixels, ProcessTracker]:
    tracker = ProcessTracker()
    sq = SquarePixels("test", tracker)
    out = b"".join([c async for c in sq.run(chunked(data))])
    return out, sq, tracker


def test_picture_rule():
    assert Picture(720, 576, "64:45").target == (1024, 576)
    assert Picture(544, 576, "32:17").target == (1024, 576)
    assert Picture(720, 576, "16:15").target == (768, 576)  # flagged 4:3 stays 4:3
    assert Picture(720, 576, "1:1").target is None and Picture(720, 576, "").target is None  # square: as stored
    assert Picture(1024, 576, "0:1").target is None
    assert Picture(1440, 1080, "4:3").target is None  # HD is never re-encoded


@needs_ffmpeg
def test_squeezed_sd_becomes_square(tmp_path):
    sar = "64/45"
    data = make_ts(tmp_path / "in.ts", "720x576", sar)
    out, sq, tracker = asyncio.run(run(data))
    assert sq.picture == Picture(720, 576, sar.replace("/", ":"))
    pic = asyncio.run(probe_picture(out))
    assert pic is not None and (pic.width, pic.height) == (1024, 576) and not pic.squeezed_sd
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0"],
        input=out, capture_output=True, check=True,
    ).stdout.decode()  # fmt: skip
    assert int(frames.split()[0].strip(",")) >= 145  # all 150 frames, including those held for the first check
    assert not tracker.live


@needs_ffmpeg
@pytest.mark.parametrize("size,sar", [("720x576", "1/1"), ("1280x720", "1/1")])
def test_square_pictures_pass_through_untouched(tmp_path, size, sar):
    data = make_ts(tmp_path / "in.ts", size, sar)
    out, sq, tracker = asyncio.run(run(data))
    assert out == data
    assert sq.picture is not None and not sq.picture.squeezed_sd
    assert not tracker.live


@needs_ffmpeg
def test_not_video_passes_through(tmp_path):
    data = b"\x47" + b"\x00" * 300_000
    out, sq, _ = asyncio.run(run(data))
    assert out == data and sq.picture is None
