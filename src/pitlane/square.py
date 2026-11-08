"""Square pixels for squeezed SD (LIVE-13; docs/tuner.md "Anamorphic SD").

Some feeds drop to PAL SD: a 16:9 picture stored as 720x576 pixels and flagged to be
shown at 16:9 (sample aspect ratio 64:45). Samsung TVs' system player ignores that
flag and shows the picture squeezed to ~5:4, in Plex and in every other app.

The relay passes the stream through untouched and checks its picture with a local
ffprobe (on bytes it already has; the panel is not contacted): on the first ~1 MB,
then every minute. While the picture is squeezed SD the bytes go through a local
x264 encoder that scales it to square pixels (720x576 becomes 1024x576); audio is
copied. HD and square-pixel SD are never re-encoded.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator

from .logging import get_logger
from .relay import CHUNK, PipedFfmpeg, ProcessTracker, ffmpeg_binary

log = get_logger("square")

START_BYTES = 1_000_000  # held back at tune-in for the first check (~3 s of SD; panels send it in their start-up burst)
START_WAIT_S = 1.5  # …or this long, whichever comes first (NFR-1: tune within 3 s)
WINDOW_BYTES = 1_500_000  # what the periodic check looks at
CHECK_EVERY_S = 60.0
RETRY_S = 5.0  # next check when the last one could not read the picture
PROBE_TIMEOUT_S = 8.0
MAX_SD_HEIGHT = 576

SQUARE_X264 = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", "6M", "-bufsize", "12M", "-pix_fmt", "yuv420p",
    "-force_key_frames", "expr:gte(t,n_forced*2)", "-sc_threshold", "0",
]  # fmt: skip


@dataclass(frozen=True)
class Picture:
    width: int
    height: int
    sar: str  # "64:45"; "1:1", "0:1" or "" when square or unknown

    def _sar(self) -> tuple[int, int] | None:
        num, _, den = self.sar.partition(":")
        try:
            n, d = int(num), int(den)
        except ValueError:
            return None
        return (n, d) if n > 0 and d > 0 and n != d else None

    @property
    def target(self) -> tuple[int, int] | None:
        """Square-pixel size to convert this SD picture to, or None to pass it through."""
        if self.height > MAX_SD_HEIGHT:
            return None
        sar = self._sar()
        if sar is None:  # square pixels (or no flag): shown as stored everywhere
            return None
        width = int(round(self.width * sar[0] / sar[1] / 2)) * 2
        return None if width == self.width else (width, self.height)

    @property
    def squeezed_sd(self) -> bool:
        return self.target is not None

    def __str__(self) -> str:
        return f"{self.width}x{self.height} SAR {self.sar or '?'}"


async def probe_picture(data: bytes) -> Picture | None:
    """Size and sample aspect ratio of the first video stream in MPEG-TS ``data``."""
    exe = shutil.which("ffprobe")
    if not exe or not data:
        return None
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,sample_aspect_ratio", "-of", "csv=p=0",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), PROBE_TIMEOUT_S)
    except (TimeoutError, BrokenPipeError, ConnectionResetError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        return None
    for line in out.decode(errors="replace").splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return Picture(int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else "")
    return None


def square_args(width: int, height: int) -> list[str]:
    vf = f"bwdif=mode=send_frame:deint=interlaced,scale={width}:{height}:flags=lanczos,setsar=1"
    return [
        ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0", "-map", "0:a?", "-vf", vf, *SQUARE_X264, "-c:a", "copy", "-copyts",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "pipe:1",
    ]  # fmt: skip


class SquarePixels:
    """Wraps a relay byte stream; see the module docstring."""

    def __init__(self, channel: str, tracker: ProcessTracker | None = None) -> None:
        self.channel = channel
        self.tracker = tracker
        self.picture: Picture | None = None
        self._enc: PipedFfmpeg | None = None
        self._size: tuple[int, int] | None = None  # what the running encoder outputs
        self._reader: asyncio.Task | None = None
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        self._failed = False  # the encoder broke once: plain passthrough for the rest of the stream

    @property
    def active(self) -> bool:
        return self._enc is not None

    async def run(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        feeder = asyncio.get_running_loop().create_task(self._feed(chunks))
        try:
            while True:
                item = await self._out.get()
                if item is None:
                    break
                yield item
            await feeder  # re-raise what ended the upstream
        finally:
            feeder.cancel()
            try:
                await feeder
            except (asyncio.CancelledError, Exception):
                pass
            await self._stop_encoder(drain=False)

    async def _feed(self, chunks: AsyncIterator[bytes]) -> None:
        window: deque[bytes] = deque()
        window_len = 0
        held: list[bytes] | None = []  # tune-in bytes waiting for the first check
        started = time.monotonic()
        checking: asyncio.Task | None = None
        next_check = 0.0
        try:
            async for chunk in chunks:
                window.append(chunk)
                window_len += len(chunk)
                while window_len - len(window[0]) >= WINDOW_BYTES:
                    window_len -= len(window.popleft())
                if held is not None:
                    held.append(chunk)
                    if window_len < START_BYTES and time.monotonic() - started < START_WAIT_S:
                        continue
                    await self._apply(await probe_picture(b"".join(window)))
                    pending, held = held, None
                    for c in pending:
                        await self._emit(c)
                    next_check = time.monotonic() + (CHECK_EVERY_S if self.picture else RETRY_S)
                    continue
                if checking is not None and checking.done():
                    await self._apply(checking.result())
                    checking = None
                elif checking is None and time.monotonic() >= next_check and not self._failed:
                    checking = asyncio.get_running_loop().create_task(probe_picture(b"".join(window)))
                    next_check = time.monotonic() + (CHECK_EVERY_S if self.picture else RETRY_S)
                await self._emit(chunk)
            if held:  # the stream ended before the first check
                for c in held:
                    await self._emit(c)
        finally:
            if checking is not None:
                checking.cancel()
            await self._stop_encoder(drain=True)
            await self._out.put(None)

    async def _apply(self, pic: Picture | None) -> None:
        if pic is None or self._failed:
            return
        changed = pic != self.picture
        self.picture = pic
        target = pic.target
        if target is not None and target != self._size:
            log.info("squeezed SD picture; converting to square pixels", extra={"channel": self.channel, "picture": str(pic), "to": f"{target[0]}x{target[1]}"})
            await self._stop_encoder(drain=True)
            await self._start_encoder(target)
        elif target is None and self._enc is not None:
            log.info("picture no longer squeezed SD; passing through", extra={"channel": self.channel, "picture": str(pic)})
            await self._stop_encoder(drain=True)
        elif changed:
            log.debug("picture", extra={"channel": self.channel, "picture": str(pic)})

    async def _emit(self, chunk: bytes) -> None:
        enc = self._enc
        if enc is None:
            await self._out.put(chunk)
            return
        try:
            assert enc.proc and enc.proc.stdin
            enc.proc.stdin.write(chunk)
            await enc.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, AssertionError):
            log.warning("square-pixel encoder failed; passing the stream through", extra={"channel": self.channel, "error": enc.error_text()})
            self._failed = True
            await self._stop_encoder(drain=True)
            await self._out.put(chunk)

    async def _start_encoder(self, size: tuple[int, int]) -> None:
        enc = PipedFfmpeg(square_args(*size), self.tracker)
        await enc.start()
        self._enc, self._size = enc, size
        self._reader = asyncio.get_running_loop().create_task(self._read(enc))

    async def _read(self, enc: PipedFfmpeg) -> None:
        while True:
            data = await enc.read(CHUNK)
            if not data:
                return
            await self._out.put(data)

    async def _stop_encoder(self, drain: bool) -> None:
        enc, reader = self._enc, self._reader
        self._enc = self._reader = None
        self._size = None
        if enc is None:
            return
        try:
            if drain and enc.proc and enc.proc.stdin and reader is not None:
                enc.proc.stdin.close()  # let it flush what it has
                try:
                    await asyncio.wait_for(asyncio.shield(reader), 10)
                except (TimeoutError, Exception):
                    pass
        finally:
            if reader is not None:
                reader.cancel()
            await enc.aclose()
