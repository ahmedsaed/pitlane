"""Provider interface (PROV-1, docs/providers.md) and shared helpers.

Providers yield live :class:`SourceSpec` objects and/or replay
:class:`ReplayItemSpec` objects. They never execute remote code (PROV-5);
everything they hand to ffmpeg passes :func:`validate_input_args` and
:func:`validate_headers`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol, runtime_checkable
from urllib.parse import urljoin

import httpx

from ..http import FetchClient, FetchPolicy
from ..models import Event, SessionType

if TYPE_CHECKING:
    pass

SourceKind = Literal["hls", "ts", "file", "tuner"]
SourceState = Literal["online", "degraded", "offline", "unknown"]


class ProviderError(Exception):
    pass


@dataclass
class SourceSpec:
    channel: str
    locator: str
    kind: SourceKind
    label: str
    priority: int = 100
    # Set by providers that auto-create channels (m3u ``map_unmatched``).
    auto_channel_name: str | None = None


@dataclass
class OpenedInput:
    url_or_path: str
    headers: dict[str, str] = field(default_factory=dict)
    ffmpeg_input_args: list[str] = field(default_factory=list)


@dataclass
class ProbeResult:
    state: Literal["online", "degraded", "offline"]
    latency_ms: int | None
    error: str | None
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayItemSpec:
    provider_item_id: str
    session_type: SessionType | None
    title: str
    locator: str
    kind: Literal["hls", "file"]


@dataclass
class ProviderContext:
    http: FetchClient
    policy: FetchPolicy
    logger: logging.Logger
    cache_dir: Path
    resolve_secret: Callable[[str], str]
    probe_timeout: float = 6.0
    degraded_latency_ms: int = 2500
    calendar: Any = None  # pitlane.calendar.jolpica.Calendar, for replay providers


@runtime_checkable
class Provider(Protocol):
    name: str
    capabilities: set[str]

    async def start(self, cfg: Any, ctx: ProviderContext) -> None: ...
    async def stop(self) -> None: ...


def source_id(provider: str, locator: str) -> str:
    """``Source.id`` = hash of provider + locator (architecture.md)."""
    return hashlib.sha256(f"{provider}\0{locator}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- ffmpeg argument allow-list

_FLAG_ONLY = {"-re"}
_WITH_VALUE = {"-rw_timeout", "-live_start_index", "-user_agent", "-headers"}
_RECONNECT_RE = re.compile(r"^-reconnect[a-z_]*$")
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def validate_input_args(args: list[str]) -> list[str]:
    """Allow-list check for ``OpenedInput.ffmpeg_input_args`` (docs/providers.md).

    Allowed: ``-re``, ``-rw_timeout``, ``-reconnect*``, ``-live_start_index``,
    ``-user_agent``, ``-headers``. Anything else (output options, ``-f lavfi``,
    ``-i`` …) raises :class:`ProviderError`.
    """
    out: list[str] = []
    i = 0
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ProviderError("ffmpeg_input_args must be a list of strings")
    while i < len(args):
        opt = args[i]
        if opt in _FLAG_ONLY:
            out.append(opt)
            i += 1
            continue
        if opt in _WITH_VALUE or _RECONNECT_RE.match(opt):
            if i + 1 >= len(args):
                raise ProviderError(f"ffmpeg option {opt} needs a value")
            val = args[i + 1]
            if val.startswith("-") and not _NUMERIC_RE.match(val):
                raise ProviderError(f"ffmpeg option {opt} has an invalid value {val!r}")
            if "\x00" in val:
                raise ProviderError("NUL byte in ffmpeg argument")
            if opt in ("-rw_timeout", "-live_start_index") and not _NUMERIC_RE.match(val):
                raise ProviderError(f"ffmpeg option {opt} needs a number")
            if opt == "-user_agent" and ("\r" in val or "\n" in val):
                raise ProviderError("line break in user agent")
            out += [opt, val]
            i += 2
            continue
        raise ProviderError(f"ffmpeg option {opt!r} is not allowed")
    return out


_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


def validate_headers(headers: dict[str, str]) -> dict[str, str]:
    """Plain HTTP headers are allowed as a generic feature (clean-room.md §5); reject injection."""
    out = {}
    for k, v in (headers or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ProviderError("header names and values must be strings")
        if not _HEADER_NAME_RE.match(k):
            raise ProviderError(f"invalid header name {k!r}")
        if any(c in v for c in "\r\n\x00"):
            raise ProviderError(f"invalid characters in header {k}")
        out[k] = v
    return out


# --------------------------------------------------------------------------- default probe


def _first_uri(playlist: str) -> str | None:
    for line in playlist.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


async def probe_hls(http: httpx.AsyncClient, url: str, headers: dict[str, str], timeout: float) -> tuple[dict, None]:
    """Fetch the playlist (following a master playlist to its first variant, RFC 8216
    §4.3.4.2) and then one media segment; each must answer HTTP 200."""
    info: dict[str, Any] = {}
    resp = await http.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise ProviderError(f"playlist HTTP {resp.status_code}")
    text = resp.text
    if not text.lstrip("﻿").startswith("#EXTM3U"):
        raise ProviderError("not an HLS playlist")
    base = str(resp.url)
    if "#EXT-X-STREAM-INF" in text:
        variant = _first_uri(text)
        if not variant:
            raise ProviderError("master playlist has no variants")
        base = urljoin(base, variant)
        resp = await http.get(base, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise ProviderError(f"variant playlist HTTP {resp.status_code}")
        text = resp.text
        info["variant"] = "first"
    seg = _first_uri(text)
    if not seg:
        raise ProviderError("media playlist has no segments")
    async with http.stream("GET", urljoin(base, seg), headers=headers, timeout=timeout) as sresp:
        if sresp.status_code != 200:
            raise ProviderError(f"segment HTTP {sresp.status_code}")
        async for _ in sresp.aiter_bytes():
            break
    return info, None


def check_ts_sync(data: bytes, packets: int = 100) -> bool:
    """MPEG-TS packets are 188 bytes, each starting with sync byte 0x47 (ISO/IEC 13818-1)."""
    for off in range(188):
        if off + (packets - 1) * 188 >= len(data):
            return False
        if all(data[off + k * 188] == 0x47 for k in range(packets)):
            return True
    return False


async def probe_ts(http: httpx.AsyncClient, url: str, headers: dict[str, str], timeout: float) -> dict:
    need = 188 * 100
    buf = bytearray()
    async with http.stream("GET", url, headers=headers, timeout=timeout) as resp:
        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}")
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) >= need + 188:
                break
    if len(buf) < need:
        raise ProviderError(f"short read ({len(buf)} bytes)")
    if not check_ts_sync(bytes(buf[: need + 187])):
        raise ProviderError("no MPEG-TS sync bytes")
    return {}


async def ffprobe_file(path: str, timeout: float) -> dict:
    if not Path(path).is_file():
        raise ProviderError("file does not exist")
    exe = shutil.which("ffprobe")
    if not exe:
        raise ProviderError("ffprobe not installed")
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "compact", "--", path,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProviderError("ffprobe timed out") from None
    if proc.returncode != 0:
        raise ProviderError(f"ffprobe failed: {err.decode(errors='replace').strip()[:200]}")
    return {"streams": out.decode(errors="replace").strip().splitlines()}


async def default_probe(kind: SourceKind, opened: OpenedInput, ctx: ProviderContext) -> ProbeResult:
    """Generic probe for any source kind (docs/providers.md "Default probe")."""
    t0 = time.monotonic()
    timeout = ctx.probe_timeout
    try:
        async with asyncio.timeout(timeout):
            if kind == "hls":
                info, _ = await probe_hls(ctx.http.raw, opened.url_or_path, opened.headers, timeout)
            elif kind in ("ts", "tuner"):
                info = await probe_ts(ctx.http.raw, opened.url_or_path, opened.headers, timeout)
            elif kind == "file":
                info = await ffprobe_file(opened.url_or_path, timeout)
            else:
                raise ProviderError(f"unknown kind {kind}")
    except TimeoutError:
        return ProbeResult("offline", None, f"timeout after {timeout:g}s")
    except (ProviderError, httpx.HTTPError, OSError) as e:
        return ProbeResult("offline", None, str(e) or e.__class__.__name__)
    latency = int((time.monotonic() - t0) * 1000)
    state: Literal["online", "degraded"] = "degraded" if latency > ctx.degraded_latency_ms else "online"
    return ProbeResult(state, latency, None, info)


class BaseProvider:
    """Convenience base class; built-ins and plugins may subclass it."""

    name: str = ""
    capabilities: set[str] = set()
    ctx: ProviderContext

    def __init__(self, name: str) -> None:
        self.name = name

    async def start(self, cfg: Any, ctx: ProviderContext) -> None:
        self.cfg = cfg
        self.ctx = ctx

    async def stop(self) -> None:
        return None

    async def refresh(self) -> None:
        """Reload provider data (playlists, lineups). Default: nothing."""
        return None

    # live
    async def list_sources(self) -> list[SourceSpec]:
        return []

    async def open(self, locator: str) -> OpenedInput:
        raise ProviderError(f"{self.name}: live not supported")

    def kind_of(self, locator: str) -> SourceKind:
        raise ProviderError("unknown locator")

    async def probe(self, locator: str) -> ProbeResult:
        try:
            opened = check_opened(await self.open(locator))
        except ProviderError as e:
            return ProbeResult("offline", None, str(e))
        return await default_probe(self.kind_of(locator), opened, self.ctx)

    # replay
    async def find_replays(self, event: Event) -> list[ReplayItemSpec]:
        return []

    async def open_replay(self, locator: str) -> OpenedInput:
        raise ProviderError(f"{self.name}: replay not supported")


def check_opened(opened: OpenedInput) -> OpenedInput:
    """Validate everything a provider wants passed to ffmpeg (PROV-5, allow-list)."""
    return OpenedInput(
        url_or_path=opened.url_or_path,
        headers=validate_headers(opened.headers),
        ffmpeg_input_args=validate_input_args(list(opened.ffmpeg_input_args)),
    )
