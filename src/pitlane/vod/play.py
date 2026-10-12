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
from ..providers.base import OpenedInput, describe_non_media, looks_like_page
from ..relay import CHUNK, FfmpegInput, InputError, ProcessTracker, build_ffmpeg_args, ffmpeg_binary

if TYPE_CHECKING:
    from .manager import VodManager

log = get_logger("vod.play")

PIPE_FORMATS = {"mkv", "webm", "ts", "avi", "flv", "mpg", "mpeg"}


def pipe_args() -> list[str]:
    return [
        ffmpeg_binary(), "-hide_banner", "-loglevel", "warning", "-i", "pipe:0",
        "-map", "0:v:0?", "-map", "0:a?", "-c", "copy",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "pipe:1",
    ]  # fmt: skip


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
                 tracker: ProcessTracker | None, save_to: Path | None) -> None:  # fmt: skip
        self.mgr = mgr
        self.item = item
        self.url = url
        self.headers = headers
        self.tracker = tracker
        self.save_to = save_to  # final library path, or None
        self.ff = _PipedFfmpeg(pipe_args(), tracker)
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
        if self.save_to is not None:
            self._watch = Path(str(self.save_to) + ".watch")
            self._watch.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._watch, "wb")
        await self.ff.start()
        self._feeder = asyncio.get_running_loop().create_task(self._feed(), name="vod-play-feed")

    async def _feed(self) -> None:
        assert self._resp is not None and self.ff.proc and self.ff.proc.stdin
        stdin = self.ff.proc.stdin
        unflushed = 0
        try:
            async for chunk in self._resp.aiter_bytes():
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
        return await self.ff.read(n)

    @property
    def returncode(self) -> int | None:
        return self.ff.returncode

    async def wait_exit(self, timeout: float = 2.0) -> int | None:
        return await self.ff.wait_exit(timeout)

    def error_text(self) -> str:
        return " | ".join([*self.stderr_tail, self.ff.error_text()])

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
        await self.ff.aclose()
        if self._file is not None:
            self._file.close()
            self._file = None
            self.mgr.keep_watched(self.item, self._watch, self.received, self.total, self.complete)


class LocalFileInput(FfmpegInput):
    """A title already in the library: played from disk at real time (no panel request)."""

    @classmethod
    def for_path(cls, path: Path, tracker: ProcessTracker | None) -> "LocalFileInput":
        return cls(build_ffmpeg_args("file", OpenedInput(str(path))), tracker)


def can_pipe(ext: str) -> bool:
    return ext.lower().lstrip(".") in PIPE_FORMATS


def replace_part(watch: Path, part: Path) -> None:
    """Keep the longer of the two prefixes of the same file."""
    have = part.stat().st_size if part.exists() else 0
    if watch.stat().st_size > have:
        os.replace(watch, part)
    else:
        watch.unlink(missing_ok=True)
