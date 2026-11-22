"""Live conversion: squeezed SD and mid-recording format changes (LIVE-13, LIVE-14).

The relay passes a stream through untouched and checks it with a local ffprobe on bytes
it already has (the panel is not contacted): the first ~1 MB at tune-in, then every
minute. It converts only when one of these applies:

- **Squeezed SD** (LIVE-13): a 16:9 picture stored as 720x576 pixels and flagged
  64:45. Samsung TVs' system player ignores the flag and shows it squeezed, in Plex
  and every other app. It is scaled to square pixels (1024x576).
- **Format change** (LIVE-14): Plex's DVR fixes the video and audio formats when a
  recording starts and keeps them for the whole recording, including back-to-back
  programmes that share its recorder and reconnects on the same channel. When the
  provider changes format (2026-11-22: HEVC + E-AC-3 HD came back as H.264 + AAC SD
  after a stall), Plex keeps writing the new data under the old labels: the rest of
  the recording cannot be played, and the next programme fails to record. The
  formats first served on a channel are locked; a connection that starts within
  ``INHERIT_S`` of the previous one on that channel inherits them. Data in other
  formats is converted back to them (video on the GPU when there is one).

Everything else, including a resolution change in the same codec, passes through.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator

from .logging import get_logger
from .relay import CHUNK, PipedFfmpeg, ProcessTracker, ffmpeg_binary

log = get_logger("convert")

START_BYTES = 1_000_000  # held back at tune-in for the first check (~3 s of SD; panels send it in their start-up burst)
START_WAIT_S = 1.5  # …or this long, whichever comes first (NFR-1: tune within 3 s)
WINDOW_BYTES = 1_500_000  # what the periodic check looks at
CHECK_EVERY_S = 60.0
RETRY_S = 5.0  # next check when the last one could not read the stream
PROBE_TIMEOUT_S = 8.0
MAX_SD_HEIGHT = 576
INHERIT_S = 60.0  # a connection this soon after the previous one on the channel continues its recording

X264 = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", "6M", "-bufsize", "12M", "-pix_fmt", "yuv420p",
    "-sc_threshold", "0",
]  # fmt: skip
X265 = ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error"]
KEYFRAMES = ["-force_key_frames", "expr:gte(t,n_forced*2)"]
CPU_VIDEO = {"h264": X264, "hevc": X265}
VAAPI_VIDEO = {"h264": "h264_vaapi", "hevc": "hevc_vaapi"}
AUDIO_ENCODERS = {"aac": "aac", "ac3": "ac3", "eac3": "eac3", "mp2": "mp2"}


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
        """Square-pixel size to convert this SD picture to, or None to leave it."""
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


@dataclass(frozen=True)
class Formats:
    """Codecs of a stream: what Plex's recorder sees."""

    video: str
    audio: str | None = None
    channels: int | None = None

    def __str__(self) -> str:
        return f"{self.video}/{self.audio or '-'}{f' {self.channels}ch' if self.channels else ''}"


@dataclass(frozen=True)
class StreamInfo:
    picture: Picture
    formats: Formats


@dataclass(frozen=True)
class Plan:
    """What the encoder does; None from :func:`plan_for` means pass through."""

    video: str | None  # codec to encode to; None: copy
    size: tuple[int, int] | None  # square-pixel size; None: keep
    audio: str | None  # codec to encode to; None: copy
    channels: int | None = None


def plan_for(info: StreamInfo, lock: Formats | None, square_sd: bool = True) -> Plan | None:
    size = info.picture.target if square_sd else None
    video = lock.video if lock else info.formats.video
    video_change = video != info.formats.video
    audio = None
    if lock and lock.audio and info.formats.audio and (lock.audio, lock.channels) != (info.formats.audio, info.formats.channels):
        audio = lock.audio
    if size is None and not video_change and audio is None:
        return None
    return Plan(video=video if (size or video_change) else None, size=size, audio=audio, channels=lock.channels if audio else None)


def served(info: StreamInfo, plan: Plan | None) -> Formats:
    """The formats Plex receives with ``plan``."""
    if plan is None:
        return info.formats
    return Formats(
        video=plan.video or info.formats.video,
        audio=plan.audio or info.formats.audio,
        channels=plan.channels if plan.audio else info.formats.channels,
    )


def encoder_args(plan: Plan, gpu: str | None) -> list[str]:
    args = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin"]
    use_gpu = gpu is not None and plan.video in VAAPI_VIDEO
    if use_gpu and plan.size is None:  # decode on the GPU too: nothing to filter on the CPU
        args += ["-hwaccel", "vaapi", "-hwaccel_device", gpu, "-hwaccel_output_format", "vaapi"]
    elif use_gpu:
        args += ["-vaapi_device", gpu]
    args += ["-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a?"]
    if plan.video is None:
        args += ["-c:v", "copy"]
    else:
        filters = []
        if plan.size:
            filters += ["bwdif=mode=send_frame:deint=interlaced", f"scale={plan.size[0]}:{plan.size[1]}:flags=lanczos", "setsar=1"]
        if use_gpu:
            if plan.size:
                filters += ["format=nv12", "hwupload"]
            height = plan.size[1] if plan.size else 1080
            rate = "3M" if height <= MAX_SD_HEIGHT else "8M"
            args += (["-vf", ",".join(filters)] if filters else []) + ["-c:v", VAAPI_VIDEO[plan.video], "-b:v", rate, "-maxrate", rate]
        else:
            args += (["-vf", ",".join(filters)] if filters else []) + CPU_VIDEO.get(plan.video, X264)
        args += KEYFRAMES
    if plan.audio:
        enc = AUDIO_ENCODERS.get(plan.audio, plan.audio)
        args += ["-c:a", enc, *(["-ac", str(plan.channels)] if plan.channels else []), "-b:a", "384k" if (plan.channels or 2) > 2 else "192k"]
    else:
        args += ["-c:a", "copy"]
    return args + ["-copyts", "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "pipe:1"]


async def probe_stream(data: bytes) -> StreamInfo | None:
    """Picture and codecs of the first video and audio stream in MPEG-TS ``data``."""
    exe = shutil.which("ffprobe")
    if not exe or not data:
        return None
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-f", "mpegts", "-i", "pipe:0",
        "-show_entries", "stream=codec_type,codec_name,width,height,sample_aspect_ratio,channels", "-of", "json",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), PROBE_TIMEOUT_S)
    except (TimeoutError, BrokenPipeError, ConnectionResetError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        return None
    try:
        streams = json.loads(out or b"{}").get("streams") or []
    except ValueError:
        return None
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width")), None)
    if video is None:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return StreamInfo(
        Picture(int(video["width"]), int(video["height"]), str(video.get("sample_aspect_ratio") or "")),
        Formats(video["codec_name"], audio.get("codec_name") if audio else None, audio.get("channels") if audio else None),
    )


def find_gpu(setting: str | None) -> str | None:
    """``auto``: the first render node that exists and can be opened; ``off``/None: none."""
    if not setting or setting == "off":
        return None
    path = "/dev/dri/renderD128" if setting == "auto" else setting
    return path if os.path.exists(path) and os.access(path, os.R_OK | os.W_OK) else None


class FormatLocks:
    """LIVE-14: the formats each channel's current recording started with."""

    def __init__(self) -> None:
        self._locks: dict[str, Formats] = {}
        self._live: dict[str, int] = {}
        self._ended: dict[str, float] = {}

    def begin(self, channel: str) -> Formats | None:
        """A new connection; returns the formats it must keep, if it continues one."""
        inherit = self._live.get(channel, 0) > 0 or time.monotonic() - self._ended.get(channel, -1e9) <= INHERIT_S
        self._live[channel] = self._live.get(channel, 0) + 1
        if not inherit:
            self._locks.pop(channel, None)
        return self._locks.get(channel)

    def lock(self, channel: str, formats: Formats) -> None:
        self._locks.setdefault(channel, formats)

    def end(self, channel: str) -> None:
        self._live[channel] = max(0, self._live.get(channel, 0) - 1)
        if not self._live[channel]:
            self._ended[channel] = time.monotonic()


class LiveConverter:
    """Wraps a relay byte stream; see the module docstring."""

    def __init__(
        self,
        channel: str,
        tracker: ProcessTracker | None = None,
        *,
        locks: FormatLocks | None = None,
        gpu: str | None = None,
        square_sd: bool = True,
        keep_formats: bool = True,
    ) -> None:
        self.channel = channel
        self.tracker = tracker
        self.locks = locks if keep_formats else None
        self.gpu = gpu
        self.square_sd = square_sd
        self.info: StreamInfo | None = None
        self.lock: Formats | None = None
        self.plan: Plan | None = None
        self.check_every = CHECK_EVERY_S
        self._enc: PipedFfmpeg | None = None
        self._reader: asyncio.Task | None = None
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        self._failed = False  # the encoder broke on the CPU too: plain passthrough for the rest of the stream

    @property
    def picture(self) -> Picture | None:
        return self.info.picture if self.info else None

    @property
    def active(self) -> bool:
        return self._enc is not None

    async def run(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        if self.locks is not None:
            self.lock = self.locks.begin(self.channel)
            if self.lock:
                log.info("continuing a recording; keeping its formats", extra={"channel": self.channel, "formats": str(self.lock)})
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
            if self.locks is not None:
                self.locks.end(self.channel)

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
                    await self._apply(await probe_stream(b"".join(window)))
                    pending, held = held, None
                    for c in pending:
                        await self._emit(c)
                    next_check = time.monotonic() + (self.check_every if self.info else RETRY_S)
                    continue
                if checking is not None and checking.done():
                    await self._apply(checking.result())
                    checking = None
                elif checking is None and time.monotonic() >= next_check and not self._failed:
                    checking = asyncio.get_running_loop().create_task(probe_stream(b"".join(window)))
                    next_check = time.monotonic() + (self.check_every if self.info else RETRY_S)
                await self._emit(chunk)
            if held:  # the stream ended before the first check
                for c in held:
                    await self._emit(c)
        finally:
            if checking is not None:
                checking.cancel()
            await self._stop_encoder(drain=True)
            await self._out.put(None)

    async def _apply(self, info: StreamInfo | None) -> None:
        if info is None or self._failed:
            return
        changed = info != self.info
        self.info = info
        plan = plan_for(info, self.lock, self.square_sd)
        if self.lock is None and self.locks is not None:
            self.lock = served(info, plan)
            self.locks.lock(self.channel, self.lock)
        if plan == self.plan:
            if changed:
                log.debug("stream", extra={"channel": self.channel, "picture": str(info.picture), "formats": str(info.formats)})
            return
        await self._stop_encoder(drain=True)
        if plan is None:
            log.info("passing the stream through", extra={"channel": self.channel, "picture": str(info.picture), "formats": str(info.formats)})
        else:
            log.info(
                "converting the stream",
                extra={"channel": self.channel, "picture": str(info.picture), "formats": str(info.formats),
                       "to": str(served(info, plan)), "size": f"{plan.size[0]}x{plan.size[1]}" if plan.size else None,
                       "gpu": bool(self.gpu and plan.video)},
            )  # fmt: skip
            await self._start_encoder(plan)
        self.plan = plan

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
            plan = self.plan
            await self._stop_encoder(drain=True)
            if self.gpu and plan is not None and plan.video:
                log.warning("GPU encoder failed; converting on the CPU", extra={"channel": self.channel, "error": enc.error_text()})
                self.gpu = None
                await self._start_encoder(plan)
                await self._emit(chunk)
                return
            log.warning("encoder failed; passing the stream through", extra={"channel": self.channel, "error": enc.error_text()})
            self._failed = True
            self.plan = None
            await self._out.put(chunk)

    async def _start_encoder(self, plan: Plan) -> None:
        enc = PipedFfmpeg(encoder_args(plan, self.gpu), self.tracker)
        await enc.start()
        self._enc = enc
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

