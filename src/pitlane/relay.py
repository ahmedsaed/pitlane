"""Stream relay (LIVE-3, LIVE-4, LIVE-6, LIVE-7, LIVE-8, LIVE-9; docs/tuner.md).

One ffmpeg child per active stream, in its own process group. Every child is
owned by an object whose ``aclose()`` sends SIGTERM to the group, waits 3 s,
then sends SIGKILL, and is always called from a ``finally`` block.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from .channels import LogicalChannel, Source
from .connections import ConnectionLimiter, Lease
from .logging import correlation, get_logger, new_correlation_id
from .metrics import Metrics
from .providers.base import OpenedInput, ProviderError, check_opened, diagnose_http_source
from .state import State, now_iso

log = get_logger("relay")

CHUNK = 64 * 1024
KILL_GRACE = 3.0

TRANSCODE_PROFILES: dict[str, list[str]] = {
    # LIVE-8, docs/tuner.md example "720p".
    "720p": [
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", "3500k", "-maxrate", "4000k", "-bufsize", "8000k",
        "-vf", "scale=-2:720", "-c:a", "aac", "-b:a", "160k",
    ],
}  # fmt: skip


def ffmpeg_binary() -> str:
    return os.environ.get("PITLANE_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def build_ffmpeg_args(
    kind: str,
    opened: OpenedInput,
    *,
    transcode: str | None = None,
    loop: bool = False,
    ffmpeg: str | None = None,
) -> list[str]:
    """Build the relay command line (docs/tuner.md "Relay pipeline")."""
    opened = check_opened(opened)
    src = opened.url_or_path
    args = [ffmpeg or ffmpeg_binary(), "-hide_banner", "-loglevel", "warning", "-nostdin"]
    is_http = src.lower().startswith(("http://", "https://"))
    if kind == "file":
        if is_http or not os.path.isabs(src):
            raise ProviderError("file sources must be absolute local paths")
        args += ["-re"]
        if loop:
            args += ["-stream_loop", "-1"]
    else:
        if not is_http:
            raise ProviderError("network sources must be http(s) URLs")
        headers = dict(opened.headers)
        ua = next((headers.pop(k) for k in list(headers) if k.lower() == "user-agent"), None)
        if headers:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        if ua:
            args += ["-user_agent", ua]
        args += ["-rw_timeout", "10000000", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    args += opened.ffmpeg_input_args
    args += ["-i", src, "-map", "0:v:0?", "-map", "0:a?"]
    if transcode:
        if transcode not in TRANSCODE_PROFILES:
            raise ProviderError(f"unknown transcode profile {transcode!r}")
        args += TRANSCODE_PROFILES[transcode]
    else:
        args += ["-c", "copy"]
    args += ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "pipe:1"]
    return args


# --------------------------------------------------------------------------- process ownership


class ProcessTracker:
    """Every live ffmpeg child, so shutdown and tests can verify none are orphaned (LIVE-9, NFR-4)."""

    def __init__(self, metrics: Metrics | None = None) -> None:
        self.live: set["FfmpegInput"] = set()
        self.metrics = metrics

    def add(self, p: "FfmpegInput") -> None:
        self.live.add(p)
        if self.metrics:
            self.metrics.ffmpeg_running.set(len(self.live))

    def discard(self, p: "FfmpegInput") -> None:
        self.live.discard(p)
        if self.metrics:
            self.metrics.ffmpeg_running.set(len(self.live))

    async def kill_all(self) -> None:
        await asyncio.gather(*(p.aclose() for p in list(self.live)), return_exceptions=True)


class InputError(Exception):
    pass


class FfmpegInput:
    """An ffmpeg child whose stdout is MPEG-TS. Use ``async with`` or call ``aclose()``."""

    def __init__(self, args: list[str], tracker: ProcessTracker | None = None) -> None:
        self.args = args
        self.tracker = tracker
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self._stderr_task: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group → whole group can be killed
        )
        if self.tracker:
            self.tracker.add(self)
        self._stderr_task = asyncio.get_running_loop().create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                self.stderr_tail.append(text)
                log.debug("ffmpeg", extra={"line": text})

    async def __aenter__(self) -> "FfmpegInput":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def read(self, n: int = CHUNK) -> bytes:
        assert self.proc and self.proc.stdout
        return await self.proc.stdout.read(n)

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode if self.proc else None

    async def wait_exit(self, timeout: float = 2.0) -> int | None:
        if not self.proc:
            return None
        try:
            return await asyncio.wait_for(self.proc.wait(), timeout)
        except TimeoutError:
            return None

    def error_text(self) -> str:
        return " | ".join(list(self.stderr_tail)[-3:]) or f"ffmpeg exited with {self.returncode}"

    def _signal_group(self, sig: int) -> None:
        assert self.proc
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                self.proc.send_signal(sig)
            except ProcessLookupError:
                pass

    async def aclose(self) -> None:
        """SIGTERM the process group, wait up to 3 s, then SIGKILL (architecture.md)."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.proc is not None:
                if self.proc.returncode is None:
                    # Nobody reads stdout any more; closing it unblocks an ffmpeg stuck
                    # writing to a full pipe, so SIGTERM ends it at once instead of after
                    # KILL_GRACE (frees a LIVE-10 connection slot ~3 s sooner).
                    transport = getattr(self.proc, "_transport", None)
                    stdout = transport.get_pipe_transport(1) if transport is not None else None
                    if stdout is not None:
                        stdout.close()
                    self._signal_group(signal.SIGTERM)
                    try:
                        await asyncio.wait_for(asyncio.shield(self.proc.wait()), KILL_GRACE)
                    except TimeoutError:
                        self._signal_group(signal.SIGKILL)
                        await self.proc.wait()
                else:
                    # Leader exited; make sure nothing else in its group survives.
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                if self._stderr_task:
                    self._stderr_task.cancel()
                    try:
                        await self._stderr_task
                    except (asyncio.CancelledError, Exception):
                        pass
                # Close pipe transports so file descriptors are released (NFR-4).
                transport = getattr(self.proc, "_transport", None)
                if transport is not None:
                    transport.close()
        finally:
            if self.tracker:
                self.tracker.discard(self)


class HttpInput:
    """Direct HTTP passthrough for ``tuner`` sources (no ffmpeg, docs/tuner.md)."""

    def __init__(self, client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> None:
        self.client = client
        self.url = url
        self.headers = headers
        self._cm: Any = None
        self._resp: httpx.Response | None = None
        self._it: AsyncIterator[bytes] | None = None
        self.returncode: int | None = None
        self._err = ""

    async def start(self) -> None:
        self._cm = self.client.stream("GET", self.url, headers=self.headers, timeout=httpx.Timeout(10, read=None))
        self._resp = await self._cm.__aenter__()
        if self._resp.status_code != 200:
            self._err = f"HTTP {self._resp.status_code}"
            self.returncode = 1
            raise InputError(self._err)
        self._it = self._resp.aiter_raw(CHUNK)

    async def read(self, n: int = CHUNK) -> bytes:
        assert self._it is not None
        try:
            return await self._it.__anext__()
        except StopAsyncIteration:
            self.returncode = 0
            return b""
        except httpx.HTTPError as e:
            self._err = str(e) or e.__class__.__name__
            self.returncode = 1
            return b""

    async def wait_exit(self, timeout: float = 0) -> int | None:
        return self.returncode

    def error_text(self) -> str:
        return self._err or "upstream closed"

    async def aclose(self) -> None:
        if self._cm is not None:
            cm, self._cm = self._cm, None
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass


# --------------------------------------------------------------------------- tuner slots (LIVE-4)


class TunerPool:
    def __init__(self, count: int) -> None:
        self.count = count
        self.in_use: dict[str, "StreamSession"] = {}

    def try_acquire(self, session: "StreamSession") -> bool:
        if len(self.in_use) >= self.count:
            return False
        self.in_use[session.id] = session
        return True

    def release(self, session: "StreamSession") -> None:
        self.in_use.pop(session.id, None)


# --------------------------------------------------------------------------- stream session


@dataclass
class RelayHooks:
    """Callbacks into the rest of the app (health state, provider lookup)."""

    get_provider: Callable[[str], Any]
    mark_source: Callable[[Source, str, str | None], None]
    reprobe: Callable[[Source], None]
    http_client: httpx.AsyncClient | None = None
    # LIVE-6: synchronous probe used to re-check offline sources at tune time;
    # it records the result in the source's health state.
    probe: Callable[[Source], Awaitable[Any]] | None = None
    # LIVE-10: per-provider connection limits (None = unlimited)
    limiter: ConnectionLimiter | None = None
    # docs/vod.md "Play now": builds the (unstarted) input for a ``vod`` source
    open_vod: Callable[[Any], Awaitable[Any]] | None = None
    # True when the picked title plays from the library (no provider connection needed)
    vod_is_local: Callable[[], bool] | None = None


class TuneFailed(Exception):
    pass


@dataclass
class StreamSession:
    channel: LogicalChannel
    sources: list[Source]
    hooks: RelayHooks
    metrics: Metrics
    tracker: ProcessTracker
    state: State | None
    tune_timeout: float
    stall_timeout: float
    client_addr: str = ""
    id: str = field(default_factory=lambda: new_correlation_id("stream"))
    bytes_sent: int = 0
    failovers: int = 0
    current: Source | None = None
    started_at: float = field(default_factory=time.monotonic)
    _input: Any = None
    _idx: int = -1
    _closing: set = field(default_factory=set)
    _lease: Lease | None = None
    _opened: OpenedInput | None = None
    _deferred: set = field(default_factory=set)  # source ids put back because their provider was busy
    offline_sources: list[Source] = field(default_factory=list)  # LIVE-6 last resort
    _restart: bool = False  # play channel: a new title was picked
    _stopped: bool = False  # the operator freed the provider's connection

    def stop(self) -> None:
        """End this stream: close the input; :meth:`chunks` then ends the response."""
        self._stopped = True
        if self._input is not None:
            asyncio.get_running_loop().create_task(self._input.aclose())

    def request_restart(self) -> None:
        """Play channel: close the input; :meth:`chunks` reopens the source with the new title."""
        self._restart = True
        if self._input is not None:
            asyncio.get_running_loop().create_task(self._input.aclose())

    async def _open_input(self, src: Source) -> Any:
        if src.kind == "vod":
            if self.hooks.open_vod is None:
                raise InputError("play channel not available")
            self._opened = None
            inp = await self.hooks.open_vod(self.tracker)
            await inp.start()
            return inp
        provider = self.hooks.get_provider(src.provider)
        if provider is None:
            raise InputError("provider not running")
        opened = check_opened(await provider.open(src.locator))
        self._opened = opened
        if src.kind == "tuner" and not self.channel.transcode:
            if self.hooks.http_client is None:
                raise InputError("no HTTP client for tuner passthrough")
            inp: Any = HttpInput(self.hooks.http_client, opened.url_or_path, opened.headers)
        else:
            args = build_ffmpeg_args(
                "ts" if src.kind == "tuner" else src.kind, opened, transcode=self.channel.transcode, loop=self.channel.loop
            )
            inp = FfmpegInput(args, self.tracker)
        await inp.start()
        return inp

    async def _close_input(self, background: bool = False) -> None:
        """Close the current input. With ``background`` the old process is retired in a
        tracked task, so failover does not wait for its SIGTERM grace period. The
        connection lease is released only once the input is closed (LIVE-10)."""
        inp, self._input = self._input, None
        lease, self._lease = self._lease, None
        if inp is None:
            if lease is not None:
                lease.release()
            return
        if background:
            if lease is not None:
                lease.closing()
            t = asyncio.get_running_loop().create_task(self._retire(inp, lease))
            self._closing.add(t)
            t.add_done_callback(self._closing.discard)
        else:
            await self._retire(inp, lease)

    @staticmethod
    async def _retire(inp: Any, lease: Lease | None) -> None:
        try:
            await inp.aclose()
        finally:
            if lease is not None:
                lease.release()

    async def _acquire(self, src: Source, deadline: float | None) -> Lease | None | bool:
        """LIVE-10: a connection slot for ``src``. Returns ``False`` when the source was
        put back at the end of the list because its provider is busy streaming."""
        limiter = self.hooks.limiter
        if limiter is None:
            return None
        if src.kind == "vod" and self.hooks.vod_is_local is not None and self.hooks.vod_is_local():
            return None  # a downloaded title plays from disk: no connection to the provider
        if limiter.held_until(src.provider) is not None:
            log.info("provider connection freed by the operator; source skipped", extra={"channel": self.channel.id, "source": src.id})
            return False
        if limiter.limit(src.provider) is None:
            return None
        wait = self.stall_timeout if deadline is None else max(0.0, deadline - time.monotonic())
        retry = src.id in self._deferred
        lease = await limiter.acquire_stream(src.provider, wait, wait_for_streams=retry)
        if lease is not None:
            return lease
        if not retry:
            # Try the other sources first; come back and wait for a slot after them.
            self._deferred.add(src.id)
            self.sources.append(src)
            log.info("provider connections in use; trying other sources first",
                     extra={"channel": self.channel.id, "source": src.id, "provider": src.provider})  # fmt: skip
        else:
            log.warning("no free provider connection", extra={"channel": self.channel.id, "source": src.id, "provider": src.provider})
        return False

    async def _fail(self, src: Source, state: str, reason: str) -> None:
        await self._close_input(background=True)
        if src.kind == "vod":  # the play channel's state is the picked title, not source health
            log.warning("play channel could not start", extra={"channel": self.channel.id, "error": reason})
            return
        self.hooks.mark_source(src, state, reason)
        self.hooks.reprobe(src)  # docs/tuner.md: reprobe immediately

    async def _first_bytes(self, deadline: float | None) -> bytes:
        """Advance through sources until one produces bytes. ``deadline`` applies to tune-in only."""
        while True:
            self._idx += 1
            if self._idx >= len(self.sources):
                raise TuneFailed("all sources exhausted")
            src = self.sources[self._idx]
            lease = await self._acquire(src, deadline)
            if lease is False:
                continue
            self._lease = lease
            prev = self.current
            self.current = src
            if prev is not None:
                self.failovers += 1
                self.metrics.failovers.labels(channel=self.channel.id).inc()
                log.warning(
                    "failover",
                    extra={"channel": self.channel.id, "from_source": prev.id, "to_source": src.id, "failovers": self.failovers},
                )
            limit = self.stall_timeout
            if deadline is not None:
                limit = min(limit, deadline - time.monotonic())
                if limit <= 0:
                    await self._close_input()
                    raise TuneFailed("tune timeout")
            try:
                self._input = await asyncio.wait_for(self._open_input(src), limit)
                data = await asyncio.wait_for(self._input.read(CHUNK), max(0.05, limit - 0.0))
            except TimeoutError:
                await self._fail(src, "degraded", f"no data within {limit:.1f}s")
                continue
            except (ProviderError, InputError, OSError, httpx.HTTPError) as e:
                await self._fail(src, "offline", str(e) or e.__class__.__name__)
                continue
            if not data:
                code = await self._input.wait_exit()
                error = self._input.error_text() if code else "ended without data"
                if code and "Invalid data found" in error:
                    error = await self._diagnose(src, deadline) or error
                await self._fail(src, "offline", error)
                continue
            log.info("source started", extra={"channel": self.channel.id, "source": src.id, "label": src.label})
            return data

    async def _diagnose(self, src: Source, deadline: float | None) -> str | None:
        """ffmpeg got an answer that is not media (a captive portal, an error page).
        One short GET says what it was. Runs while the source still holds its
        connection slot (LIVE-10), and only for this failure."""
        opened = self._opened
        if self.hooks.http_client is None or opened is None or not opened.url_or_path.lower().startswith(("http://", "https://")):
            return None
        timeout = 5.0 if deadline is None else min(5.0, deadline - time.monotonic())
        if timeout <= 0.5:
            return None
        found = await diagnose_http_source(self.hooks.http_client, opened.url_or_path, opened.headers, timeout)
        if found:
            log.warning("source returned non-media", extra={"channel": self.channel.id, "source": src.id, "error": found})
        return found

    async def tune(self) -> bytes:
        """Start the first working source and return its first chunk (≤ tune_timeout)."""
        deadline = self.started_at + self.tune_timeout
        try:
            data = await self._first_bytes(deadline)
        except TuneFailed:
            data = await self._tune_offline(deadline)
        self.metrics.tune_seconds.observe(time.monotonic() - self.started_at)
        return data

    async def _tune_offline(self, deadline: float) -> bytes:
        """LIVE-6: when no online/degraded/unprobed source is left, re-probe the
        ``offline`` sources in priority order within ``tune_timeout`` and start the
        first one that passes. Raises :class:`TuneFailed` only if none does."""
        if self.hooks.probe is None:
            raise TuneFailed("no usable sources")
        for src in self.offline_sources:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = await asyncio.wait_for(self.hooks.probe(src), remaining)
            except TimeoutError:
                break
            # None: not probed (health checks off, or connection limit); opening it is the probe.
            if result is not None and getattr(result, "state", "offline") not in ("online", "degraded"):
                continue
            log.info("retrying offline source", extra={"channel": self.channel.id, "source": src.id, "probed": result is not None})
            self.sources.append(src)
            self._idx = len(self.sources) - 2
            try:
                return await self._first_bytes(deadline)
            except TuneFailed:
                continue
        raise TuneFailed("no usable sources (offline sources re-probed)")

    async def chunks(self) -> AsyncIterator[bytes]:
        """Subsequent chunks with stall detection and failover (LIVE-7)."""
        while True:
            assert self._input is not None and self.current is not None
            src = self.current
            try:
                data = await asyncio.wait_for(self._input.read(CHUNK), self.stall_timeout)
            except TimeoutError:
                if src.kind == "vod":  # a title has no backup; restarting it from the top would be worse
                    log.warning("play channel stalled; ending stream", extra={"channel": self.channel.id, "stall_timeout": self.stall_timeout})
                    return
                log.warning("source stalled", extra={"channel": self.channel.id, "source": src.id, "stall_timeout": self.stall_timeout})
                await self._fail(src, "degraded", f"stalled for {self.stall_timeout:g}s")
                data = None
            if data and not self._stopped:
                yield data
                continue
            if self._stopped:
                log.info("stream stopped by the operator", extra={"channel": self.channel.id})
                return
            if data is not None and self._restart:  # play channel: switch to the newly picked title
                self._restart = False
                await self._close_input()
                self._idx, self.current = -1, None
                try:
                    data = await self._first_bytes(None)
                except TuneFailed:
                    return
                yield data
                continue
            if data is not None and src.kind == "vod":  # the title ended (or failed): end the stream
                code = await self._input.wait_exit()
                if code:
                    log.warning("play channel stopped", extra={"channel": self.channel.id, "error": self._input.error_text()})
                else:
                    log.info("play channel: title finished", extra={"channel": self.channel.id})
                return
            if data is not None:  # EOF
                code = await self._input.wait_exit()
                if code:
                    await self._fail(src, "offline", self._input.error_text())
                else:
                    log.info("source ended", extra={"channel": self.channel.id, "source": src.id})
                    await self._close_input()
            try:
                data = await self._first_bytes(None)
            except TuneFailed:
                log.warning("all sources exhausted; ending stream", extra={"channel": self.channel.id})
                return
            yield data

    def record_start(self) -> None:
        if self.state:
            self.state.execute(
                "INSERT INTO stream_runs(id, channel_id, source_id, client_addr, started_at) VALUES(?,?,?,?,?)",
                (self.id, self.channel.id, self.current.id if self.current else None, self.client_addr, now_iso()),
            )

    def record_end(self) -> None:
        if self.state:
            self.state.execute(
                "UPDATE stream_runs SET ended_at=?, bytes=?, failovers=?, source_id=? WHERE id=?",
                (now_iso(), self.bytes_sent, self.failovers, self.current.id if self.current else None, self.id),
            )

    async def aclose(self) -> None:
        await self._close_input()
        if self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)


class RelayResponse:
    """ASGI response that streams a :class:`StreamSession`.

    It listens for ``http.disconnect`` itself, so the slot is freed and ffmpeg
    is killed as soon as the connection closes, not when a later write fails
    (docs/tuner.md "Tuner slots", LIVE-9).
    """

    def __init__(self, session: StreamSession, pool: TunerPool, on_done: Callable[[], Awaitable[None]] | None = None):
        self.session = session
        self.pool = pool
        self.on_done = on_done

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        s = self.session
        with correlation(s.id):
            log.info("stream requested", extra={"channel": s.channel.id, "client": s.client_addr})
            pump = asyncio.ensure_future(self._pump(send))
            listen = asyncio.ensure_future(self._wait_disconnect(receive))
            try:
                done, _ = await asyncio.wait({pump, listen}, return_when=asyncio.FIRST_COMPLETED)
                if listen in done and not pump.done():
                    log.info("client disconnected", extra={"channel": s.channel.id, "bytes": s.bytes_sent})
                    self.pool.release(s)  # free the slot immediately
            finally:
                for t in (pump, listen):
                    t.cancel()
                self.pool.release(s)
                try:
                    await asyncio.shield(self._cleanup(pump, listen))
                except asyncio.CancelledError:
                    pass

    async def _cleanup(self, pump: asyncio.Future, listen: asyncio.Future) -> None:
        for t in (pump, listen):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.session.aclose()
        self.session.metrics.streams_active.set(len(self.pool.in_use))
        self.session.record_end()
        log.info(
            "stream ended",
            extra={"channel": self.session.channel.id, "bytes": self.session.bytes_sent, "failovers": self.session.failovers},
        )
        if self.on_done:
            await self.on_done()

    @staticmethod
    async def _wait_disconnect(receive: Callable) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return

    async def _pump(self, send: Callable) -> None:
        s = self.session
        try:
            first = await s.tune()
        except TuneFailed as e:
            log.warning("tune failed", extra={"channel": s.channel.id, "reason": str(e)})
            s.metrics.tuner_rejections.inc()
            body = b"No source could start\n"
            await send({"type": "http.response.start", "status": 503, "headers": [
                (b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})  # fmt: skip
            await send({"type": "http.response.body", "body": body})
            return
        s.record_start()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"video/mp2t"), (b"cache-control", b"no-cache")],
            }
        )
        await send({"type": "http.response.body", "body": first, "more_body": True})
        s.bytes_sent += len(first)
        async for chunk in s.chunks():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
            s.bytes_sent += len(chunk)
        await send({"type": "http.response.body", "body": b"", "more_body": False})
