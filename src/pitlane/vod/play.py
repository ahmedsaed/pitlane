""""Play now": stream the picked movie or episode on the play channel (docs/vod.md).

The panel is built for watching: one connection, served at about playback
speed. :class:`PlayInput` reads that one response and feeds it to ffmpeg
(remux to MPEG-TS for the tuner) and, with ``save``, to a file, so a title
that is watched to the end is also in the library without another request.

- A title already in the library plays from the local file (no panel request).
- MP4 files may keep their index at the end, which a pipe cannot reach; they
  are played by ffmpeg straight from the URL and not saved.
- When playback stops early, the saved part replaces a shorter download
  ``.part``, so the downloader can resume from there if asked.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..logging import get_logger
from ..providers.base import describe_non_media, looks_like_page
from ..relay import CHUNK, FfmpegInput, InputError, ProcessTracker, ffmpeg_binary

if TYPE_CHECKING:
    from .manager import VodManager

log = get_logger("vod.play")

PIPE_FORMATS = {"mkv", "webm", "ts", "avi", "flv", "mpg", "mpeg"}


# Plex's Live TV path for desktop apps (HTTP, direct-stream audio, Matroska; e.g. Plex for
# Linux) starts reading ~0.8 s into the recording and gives up if it cannot learn the stream's
# parameters within a couple of seconds [verified 2026-10-12, PMS 1.43.4]:
# - AAC copied out of a file (or re-encoded to AAC) fails: "Error parsing AAC extradata".
#   AC3 carries its settings in every frame, so audio is converted to AC3 (cheap).
# - Video is copied untouched, with the codec headers repeated at every keyframe. It starts
#   when the title has a keyframe every ~2 s, as broadcasts and many episodes do. Titles with
#   long gaps between keyframes (films often have ~10 s) fail in the desktop app ("dimensions
#   not set") but play in Plex Web. Those are re-encoded (x264, keyframe every 2 s, about one
#   CPU core for 1080p). ``reencode: auto`` (default) decides per title from the keyframes in
#   the first ~6 MB, which the panel sends in its start-up burst anyway; true/false force it.
# - Output is paced at playback speed after a 3 s start-up burst, like a channel.
PACE = ["-readrate", "1", "-readrate_initial_burst", "3"]
AUDIO = ["-c:a", "ac3", "-b:a", "384k"]  # keeps 5.1 when the source has it
VIDEO_X264 = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p",
    "-force_key_frames", "expr:gte(t,n_forced*2)", "-sc_threshold", "0",
]  # fmt: skip
VIDEO_BSF = {
    "h264": "h264_mp4toannexb,dump_extra=freq=keyframe",
    "hevc": "hevc_mp4toannexb,dump_extra=freq=keyframe",
}


MAX_GAP_S = 3.0  # longest keyframe gap that Plex desktop apps still start (2 s works; 10 s does not)
PROBE_BYTES = 6_000_000
PROBE_WAIT_S = 6.0


async def keyframe_gap(path: Path, seconds: int = 30) -> float | None:
    """Longest gap between video keyframes in the first ``seconds`` of ``path`` (packet
    flags only, nothing is decoded). ``None`` when there is too little video to tell."""
    import shutil

    exe = shutil.which("ffprobe")
    if not exe:
        return None
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-select_streams", "v:0", "-read_intervals", f"%+{seconds}",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    pts: list[float] = []
    keys: list[float] = []
    for line in out.decode(errors="replace").splitlines():
        t, _, flags = line.partition(",")
        try:
            v = float(t)
        except ValueError:
            continue
        pts.append(v)
        if "K" in flags:
            keys.append(v)
    if not keys or not pts:
        return None
    gaps = [b - a for a, b in zip(keys, keys[1:])]
    tail = max(pts) - keys[-1]  # time since the last keyframe: a lower bound for the next gap
    longest = max([*gaps, tail])
    if longest <= MAX_GAP_S and max(pts) - keys[0] < MAX_GAP_S + 0.5:
        return None  # not enough video seen to rule out a long gap
    return longest


def needs_reencode(gap: float | None) -> bool:
    return gap is None or gap > MAX_GAP_S


def video_args(reencode: bool, vcodec: str | None) -> list[str]:
    if reencode:
        return VIDEO_X264
    bsf = VIDEO_BSF.get((vcodec or "").lower().replace("h265", "hevc"))
    return ["-c:v", "copy", *(["-bsf:v", bsf] if bsf else [])]


def play_args(src: str, reencode: bool = False, vcodec: str | None = None) -> list[str]:
    return [
        ffmpeg_binary(), "-hide_banner", "-loglevel", "warning", *PACE, "-i", src,
        "-map", "0:v:0?", "-map", "0:a:0?", *video_args(reencode, vcodec), *AUDIO,
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "pipe:1",
    ]  # fmt: skip


def pipe_args(reencode: bool = False, vcodec: str | None = None) -> list[str]:
    return play_args("pipe:0", reencode, vcodec)


class _PipedFfmpeg(FfmpegInput):
    """FfmpegInput whose stdin is a pipe we write the panel's bytes into."""

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if self.tracker:
            self.tracker.add(self)
        self._stderr_task = asyncio.get_running_loop().create_task(self._drain_stderr())


class PlayInput:
    """Relay input for the play channel: same interface as FfmpegInput."""

    def __init__(self, mgr: "VodManager", item: dict[str, Any], url: str, headers: dict[str, str],
                 tracker: ProcessTracker | None, save_to: Path | None, reencode: Any = "auto",
                 vcodec: str | None = None) -> None:  # fmt: skip
        self.mgr = mgr
        self.item = item
        self.url = url
        self.headers = headers
        self.tracker = tracker
        self.save_to = save_to  # final library path, or None
        self.reencode = reencode  # True / False / "auto"
        self.vcodec = vcodec
        self.ff: _PipedFfmpeg | None = None
        self._prefix = b""
        self._chunks: Any = None
        self.stderr_tail: deque[str] = deque(maxlen=5)
        self._cm: Any = None
        self._resp: httpx.Response | None = None
        self._feeder: asyncio.Task | None = None
        self._file: Any = None
        self._watch: Path | None = None
        self.received = 0
        self.total: int | None = None
        self.complete = False

    async def start(self) -> None:
        self._cm = self.mgr.app.http.raw.stream("GET", self.url, headers=self.headers, timeout=httpx.Timeout(30.0, read=60.0))
        resp = await self._cm.__aenter__()
        self._resp = resp
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200 or "html" in ctype or "json" in ctype:
            head = b""
            async for chunk in resp.aiter_bytes():
                head += chunk
                if len(head) >= 4096:
                    break
            await self._close_http()
            if resp.status_code == 200 or looks_like_page(resp, head):
                raise InputError(describe_non_media(self.url, resp, head))
            raise InputError(f"HTTP {resp.status_code}")
        cl = resp.headers.get("content-length")
        self.total = int(cl) if cl and cl.isdigit() else None
        self._chunks = resp.aiter_bytes()
        reencode = await self._decide()
        self.ff = _PipedFfmpeg(pipe_args(reencode, self.vcodec), self.tracker)
        if self.save_to is not None:
            self._watch = Path(str(self.save_to) + ".watch")
            self._watch.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._watch, "wb")
        await self.ff.start()
        self._feeder = asyncio.get_running_loop().create_task(self._feed(), name="vod-play-feed")

    async def _decide(self) -> bool:
        """Copy or re-encode the video (``reencode: auto``): look at the keyframes in the
        start-up burst, which is read anyway; the answer is remembered per title."""
        if isinstance(self.reencode, bool):
            return self.reencode
        key = self.item["key"]
        known = self.mgr.known_gap(key)
        if known is not None:
            return needs_reencode(known if known >= 0 else None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROBE_WAIT_S
        try:
            while len(self._prefix) < PROBE_BYTES and loop.time() < deadline:
                chunk = await asyncio.wait_for(self._chunks.__anext__(), max(0.1, deadline - loop.time()))
                self._prefix += chunk
        except (StopAsyncIteration, TimeoutError):
            pass
        tmp = self.mgr.app.data_dir / "tmp" / f"probe-{os.getpid()}-{id(self)}.bin"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_bytes(self._prefix)
            gap = await keyframe_gap(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        self.mgr.remember_gap(key, gap)
        return needs_reencode(gap)

    async def _feed(self) -> None:
        assert self._resp is not None and self.ff is not None and self.ff.proc and self.ff.proc.stdin
        stdin = self.ff.proc.stdin
        unflushed = 0

        async def chunks():  # the probed start-up bytes first, then the rest of the response
            if self._prefix:
                yield self._prefix
                self._prefix = b""
            async for chunk in self._chunks:
                yield chunk

        try:
            async for chunk in chunks():
                if self._file is not None:
                    self._file.write(chunk)
                self.received += len(chunk)
                unflushed += len(chunk)
                if unflushed >= 4_000_000:
                    self.mgr._add_usage(unflushed)
                    unflushed = 0
                stdin.write(chunk)
                await stdin.drain()
            self.complete = self.total is None or self.received >= self.total
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg went away (stream closed)
        except httpx.HTTPError as e:
            self.stderr_tail.append(f"panel connection: {e.__class__.__name__}")
        finally:
            if unflushed:
                self.mgr._add_usage(unflushed)
            try:
                stdin.close()
            except Exception:
                pass

    async def read(self, n: int = CHUNK) -> bytes:
        assert self.ff is not None
        return await self.ff.read(n)

    @property
    def returncode(self) -> int | None:
        return self.ff.returncode if self.ff else None

    async def wait_exit(self, timeout: float = 2.0) -> int | None:
        return await self.ff.wait_exit(timeout) if self.ff else None

    def error_text(self) -> str:
        return " | ".join([*self.stderr_tail, *([self.ff.error_text()] if self.ff else [])])

    async def _close_http(self) -> None:
        if self._cm is not None:
            cm, self._cm = self._cm, None
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def aclose(self) -> None:
        if self._feeder is not None and not self._feeder.done():
            self._feeder.cancel()
            try:
                await self._feeder
            except (asyncio.CancelledError, Exception):
                pass
        await self._close_http()
        if self.ff is not None:
            await self.ff.aclose()
        if self._file is not None:
            self._file.close()
            self._file = None
            self.mgr.keep_watched(self.item, self._watch, self.received, self.total, self.complete)


class LocalFileInput(FfmpegInput):
    """A title already in the library: played from disk at real time (no panel request)."""

    @classmethod
    def for_path(cls, path: Path, tracker: ProcessTracker | None, reencode: bool = False, vcodec: str | None = None) -> "LocalFileInput":
        return cls(play_args(str(path), reencode, vcodec), tracker)


def can_pipe(ext: str) -> bool:
    return ext.lower().lstrip(".") in PIPE_FORMATS


def replace_part(watch: Path, part: Path) -> None:
    """Keep the longer of the two prefixes of the same file."""
    have = part.stat().st_size if part.exists() else 0
    if watch.stat().st_size > have:
        os.replace(watch, part)
    else:
        watch.unlink(missing_ok=True)
