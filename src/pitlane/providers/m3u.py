"""``m3u`` provider (PROV-2): extended M3U playlist from a file or URL."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .base import BaseProvider, OpenedInput, ProviderError, SourceKind, SourceSpec

_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


@dataclass
class M3uEntry:
    name: str
    url: str
    attrs: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> SourceKind:
        path = urlparse(self.url).path.lower()
        return "hls" if path.endswith((".m3u8", ".m3u")) else "ts"


def parse_m3u(text: str) -> list[M3uEntry]:
    """Parse ``#EXTINF`` attributes and ``#EXTVLCOPT:http-user-agent`` / ``http-referrer``."""
    entries: list[M3uEntry] = []
    pending_attrs: dict[str, str] | None = None
    pending_name = ""
    pending_headers: dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            head, _, title = line.partition(",")
            pending_attrs = {k.lower(): v for k, v in _ATTR_RE.findall(head)}
            pending_name = title.strip()
            pending_headers = {}
        elif line.startswith("#EXTVLCOPT:"):
            key, _, value = line[len("#EXTVLCOPT:") :].partition("=")
            key = key.strip().lower()
            if key == "http-user-agent":
                pending_headers["User-Agent"] = value.strip()
            elif key == "http-referrer":
                pending_headers["Referer"] = value.strip()
        elif line.startswith("#"):
            continue
        else:
            if pending_attrs is not None and urlparse(line).scheme in ("http", "https"):
                entries.append(M3uEntry(pending_name, line, pending_attrs, pending_headers))
            pending_attrs, pending_name, pending_headers = None, "", {}
    return entries


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "channel"


class M3uProvider(BaseProvider):
    capabilities = {"live"}

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.entries: dict[str, M3uEntry] = {}
        self._lock = asyncio.Lock()

    async def _load_text(self) -> str:
        if self.cfg.path:
            return await asyncio.to_thread(Path(self.cfg.path).read_text, encoding="utf-8")
        resp = await self.ctx.http.get(self.cfg.url, policy=self.ctx.policy)
        return resp.text

    async def refresh(self) -> None:
        async with self._lock:
            try:
                text = await self._load_text()
            except Exception as e:
                # Keep the previous list on failure.
                self.ctx.logger.warning("m3u refresh failed", extra={"provider": self.name, "error": str(e)})
                if not self.entries:
                    raise ProviderError(f"{self.name}: cannot load playlist: {e}") from None
                return
            self.entries = {e.url: e for e in parse_m3u(text)}

    async def list_sources(self) -> list[SourceSpec]:
        if not self.entries:
            await self.refresh()
        out: list[SourceSpec] = []
        for e in self.entries.values():
            fields = {
                "tvg-id": e.attrs.get("tvg-id", ""),
                "tvg-name": e.attrs.get("tvg-name", ""),
                "group-title": e.attrs.get("group-title", ""),
                "name": e.name,
            }
            matched = False
            for rule in self.cfg.map:
                if all(re.search(pattern, fields.get(key, "")) for key, pattern in rule.match.items()):
                    matched = True
                    out.append(
                        SourceSpec(
                            channel=rule.channel,
                            locator=e.url,
                            kind=e.kind,
                            label=e.name or fields["tvg-name"] or fields["tvg-id"],
                            priority=rule.priority if rule.priority is not None else 100,
                        )
                    )
            if not matched and self.cfg.map_unmatched:
                label = e.name or fields["tvg-name"] or fields["tvg-id"] or "channel"
                out.append(
                    SourceSpec(
                        channel=f"{slugify(self.name)}-{slugify(fields['tvg-id'] or label)}",
                        locator=e.url,
                        kind=e.kind,
                        label=label,
                        auto_channel_name=label,
                    )
                )
        return out

    def kind_of(self, locator: str) -> SourceKind:
        e = self.entries.get(locator)
        if e is None:
            raise ProviderError("unknown locator")
        return e.kind

    async def open(self, locator: str) -> OpenedInput:
        e = self.entries.get(locator)
        if e is None:
            raise ProviderError(f"{self.name}: source no longer in playlist")
        return OpenedInput(url_or_path=e.url, headers=dict(e.headers))
