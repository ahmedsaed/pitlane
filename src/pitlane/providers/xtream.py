"""``xtream`` provider: an Xtream Codes panel via ``player_api.php`` (docs/providers.md, docs/vod.md).

One provider holds one account, so live streams and VOD downloads share one
connection limit (LIVE-10, VOD-4). The panel is asked as little as possible:

- the live list (categories + streams) is cached on disk and fetched again only
  when older than ``refresh`` (default 24 h), so restarts and config reloads
  cost no requests;
- the VOD catalogue is fetched by :mod:`pitlane.vod` on its own schedule, and
  title details only when the operator opens or requests a title.

Locators never contain credentials (``live:{stream_id}``); the stream URL is
built in :meth:`open`. Error messages are passed through :func:`_clean`, which
strips URL paths (they carry the username and password).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ..http import FetchPolicy
from .base import BaseProvider, OpenedInput, ProviderError, SourceKind, SourceSpec

_URL_PATH = re.compile(r"(\bhttps?://[^/\s\"']+)/[^\s\"']*", re.IGNORECASE)
LIVE_CACHE = "live.json"
PLAY_LOCATOR = "play"  # the "Play now" channel's source; opened by pitlane.vod.play


def _clean(text: str) -> str:
    return _URL_PATH.sub(r"\1/…", text)


@dataclass
class LiveEntry:
    stream_id: int
    name: str
    category: str


class XtreamApi:
    """``player_api.php`` calls. Each call is one request; no retries on 4xx."""

    def __init__(self, provider: "XtreamProvider") -> None:
        self.p = provider

    @property
    def base(self) -> str:
        return self.p.cfg.url.rstrip("/")

    def _policy(self) -> FetchPolicy:
        pol = self.p.ctx.policy
        # Bulk lists are a few MB; a retry is another full request, so at most one.
        return FetchPolicy(
            timeout=max(pol.timeout, 60.0),
            retries=min(pol.retries, 1),
            backoff=pol.backoff,
            rate_limit_per_sec=pol.rate_limit_per_sec,
            user_agent=self.p.cfg.user_agent,
        )

    async def call(self, action: str | None = None, **params: Any) -> Any:
        q = {"username": self.p.cfg.username, "password": self.p.cfg.password}
        if action:
            q["action"] = action
        q.update({k: str(v) for k, v in params.items()})
        self.p.requests += 1
        try:
            resp = await self.p.ctx.http.get(f"{self.base}/player_api.php", policy=self._policy(), params=q)
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.p.name}: {action or 'login'} failed: {_clean(str(e))}") from None
        try:
            return resp.json()
        except ValueError:
            from .base import describe_non_media

            raise ProviderError(f"{self.p.name}: {action}: {describe_non_media(self.base, resp, resp.content[:4096])}") from None

    def media_url(self, kind: str, item_id: int | str, ext: str) -> str:
        """``/live|movie|series/{user}/{pass}/{id}.{ext}`` – carries credentials; never log it."""
        u, pw = quote(self.p.cfg.username, safe=""), quote(self.p.cfg.password, safe="")
        return f"{self.base}/{kind}/{u}/{pw}/{item_id}.{ext}"


class XtreamProvider(BaseProvider):
    capabilities = {"live"}

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.entries: dict[str, LiveEntry] = {}
        self.fetched_at: float | None = None
        self.requests = 0  # panel requests made by this instance (tests, metrics)
        self.play_channel: str | None = None  # set by the registry when vod.play_channel is on
        self._lock = asyncio.Lock()

    async def start(self, cfg: Any, ctx: Any) -> None:
        await super().start(cfg, ctx)
        self.api = XtreamApi(self)
        if cfg.vod:
            self.capabilities = {"live", "vod"}
        self._load_cache()

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.cfg.user_agent}

    # ------------------------------------------------------------------ live list
    def _cache_path(self):  # type: ignore[no-untyped-def]
        return self.ctx.cache_dir / LIVE_CACHE

    def _load_cache(self) -> None:
        try:
            doc = json.loads(self._cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if doc.get("url") != self.cfg.url or doc.get("username_hash") != _user_hash(self.cfg.username):
            return  # another panel or account: fetch fresh
        self._apply(doc.get("categories") or [], doc.get("streams") or [])
        self.fetched_at = float(doc.get("fetched_at") or 0)

    def _apply(self, categories: list[dict], streams: list[dict]) -> None:
        cats = {str(c.get("category_id")): str(c.get("category_name") or "") for c in categories if isinstance(c, dict)}
        entries: dict[str, LiveEntry] = {}
        for s in streams:
            if not isinstance(s, dict) or s.get("stream_id") in (None, ""):
                continue
            try:
                sid = int(s["stream_id"])
            except (TypeError, ValueError):
                continue
            e = LiveEntry(sid, str(s.get("name") or "").strip(), cats.get(str(s.get("category_id")), ""))
            entries[f"live:{sid}"] = e
        self.entries = entries

    def due(self) -> bool:
        return self.fetched_at is None or time.time() - self.fetched_at >= self.cfg.refresh.total_seconds()

    async def refresh(self, force: bool = False) -> None:
        async with self._lock:
            if not force and not self.due():
                return
            try:
                categories = await self.api.call("get_live_categories")
                streams = await self.api.call("get_live_streams")
                if not isinstance(categories, list) or not isinstance(streams, list):
                    raise ProviderError(f"{self.name}: unexpected answer (account expired or disabled?)")
            except ProviderError as e:
                # Keep the previous list; try again at the next refresh.
                self.ctx.logger.warning("xtream refresh failed", extra={"provider": self.name, "error": str(e)})
                self.fetched_at = time.time()
                if not self.entries:
                    raise
                return
            self._apply(categories, streams)
            self.fetched_at = time.time()
            doc = {
                "url": self.cfg.url,
                "username_hash": _user_hash(self.cfg.username),
                "fetched_at": self.fetched_at,
                "categories": categories,
                "streams": [{"stream_id": s.get("stream_id"), "name": s.get("name"), "category_id": s.get("category_id")} for s in streams if isinstance(s, dict)],
            }
            tmp = self._cache_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            tmp.replace(self._cache_path())
            self.ctx.logger.info("xtream live list refreshed", extra={"provider": self.name, "streams": len(self.entries)})

    async def list_sources(self) -> list[SourceSpec]:
        if not self.entries:
            await self.refresh(force=True)
        out: list[SourceSpec] = []
        if self.play_channel:
            out.append(SourceSpec(channel=self.play_channel, locator=PLAY_LOCATOR, kind="vod", label="Play now", priority=0))
        for loc, e in self.entries.items():
            fields = {"tvg-id": f"xc.{e.stream_id}", "name": e.name, "group-title": e.category}
            for rule in self.cfg.map:
                if all(re.search(pattern, fields.get(key, "")) for key, pattern in rule.match.items()):
                    out.append(
                        SourceSpec(
                            channel=rule.channel,
                            locator=loc,
                            kind="ts",
                            label=e.name,
                            priority=rule.priority if rule.priority is not None else 100,
                        )
                    )
        return out

    def kind_of(self, locator: str) -> SourceKind:
        if locator == PLAY_LOCATOR:
            return "vod"
        if locator not in self.entries:
            raise ProviderError("unknown locator")
        return "ts"

    async def probe(self, locator: str) -> Any:
        if locator == PLAY_LOCATOR:  # nothing to check until a title is picked
            from .base import ProbeResult

            return ProbeResult("online", None, None)
        return await super().probe(locator)

    async def open(self, locator: str) -> OpenedInput:
        e = self.entries.get(locator)
        if e is None:
            raise ProviderError(f"{self.name}: stream no longer on the panel")
        return OpenedInput(url_or_path=self.api.media_url("live", e.stream_id, "ts"), headers=self.headers)

    # ------------------------------------------------------------------ VOD (docs/vod.md)
    async def vod_catalog(self) -> dict[str, Any]:
        """Bulk lists: 4 requests (movie and series categories, movies, series)."""
        out = {
            "vod_categories": await self.api.call("get_vod_categories"),
            "movies": await self.api.call("get_vod_streams"),
            "series_categories": await self.api.call("get_series_categories"),
            "series": await self.api.call("get_series"),
        }
        for k, v in out.items():
            if not isinstance(v, list):
                raise ProviderError(f"{self.name}: {k}: unexpected answer (account expired or disabled?)")
        return out

    async def vod_info(self, vod_id: int) -> dict[str, Any]:
        doc = await self.api.call("get_vod_info", vod_id=vod_id)
        if not isinstance(doc, dict) or not doc.get("movie_data"):
            raise ProviderError(f"{self.name}: no details for movie {vod_id}")
        return doc

    async def series_info(self, series_id: int) -> dict[str, Any]:
        doc = await self.api.call("get_series_info", series_id=series_id)
        if not isinstance(doc, dict) or "episodes" not in doc:
            raise ProviderError(f"{self.name}: no details for series {series_id}")
        return doc

    def movie_url(self, vod_id: int, ext: str) -> str:
        return self.api.media_url("movie", vod_id, ext)

    def episode_url(self, episode_id: int | str, ext: str) -> str:
        return self.api.media_url("series", episode_id, ext)


def _user_hash(username: str) -> str:
    import hashlib

    return hashlib.sha256(username.encode()).hexdigest()[:12]
