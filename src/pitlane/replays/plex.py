"""PMS HTTP API client (REP-5, REP-6, REP-8; docs/replays.md "Plex metadata").

All PMS endpoints live in this one class so they are easy to change if PMS
behavior differs from the documentation [verify]. The token is sent in the
``X-Plex-Token`` header (never in URLs, so it cannot leak into logs).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

import httpx

from ..http import FetchClient, FetchPolicy
from ..logging import get_logger

log = get_logger("plex")


@dataclass
class PlexItem:
    rating_key: str
    title: str | None
    view_count: int
    files: list[str]
    grandparent_title: str | None = None
    guid: str | None = None

    @property
    def matched(self) -> bool:
        """Matched to an online agent (VOD-8): not a ``local://`` or ``none`` guid."""
        g = self.guid or ""
        return bool(g) and not g.startswith(("local://", "com.plexapp.agents.none"))


def poster_uri(key: str) -> str:
    """The poster URI a PMS poster key refers to: the decoded ``url`` query parameter if
    present (``/library/metadata/{rk}/file?url=upload%3A%2F%2F…``), else the unquoted key."""
    from urllib.parse import parse_qs, unquote, urlsplit

    url = parse_qs(urlsplit(key).query).get("url")
    return url[0] if url else unquote(key)


class PlexError(Exception):
    pass


class PlexClient:
    def __init__(self, http: FetchClient, url: str, token: str, section_id: int, policy: FetchPolicy | None = None):
        self.http = http
        self.url = url.rstrip("/")
        self.token = token
        self.section_id = section_id
        self.policy = policy or FetchPolicy(timeout=15, retries=2, backoff=1, rate_limit_per_sec=10)

    def _headers(self, token: str | None = None) -> dict[str, str]:
        return {"X-Plex-Token": token or self.token, "Accept": "application/json"}

    async def _req(
        self, method: str, path: str, params: dict[str, Any] | None = None, token: str | None = None,
        content: bytes | None = None, content_type: str | None = None,
    ) -> httpx.Response:  # fmt: skip
        headers = self._headers(token)
        if content_type:
            headers["Content-Type"] = content_type
        return await self.http.request(
            method,
            f"{self.url}{path}",
            policy=self.policy,
            headers=headers,
            params={k: str(v) for k, v in (params or {}).items()},
            content=content,
        )

    # ------------------------------------------------------------------ scan + lookup
    async def scan_folder(self, folder: str) -> None:
        """Targeted scan of one folder only: ``GET /library/sections/{id}/refresh?path=…``."""
        await self._req("GET", f"/library/sections/{self.section_id}/refresh", {"path": folder})

    async def scan_section(self) -> None:
        """Full scan of the library (a new library ignores folder scans until its first full scan)."""
        await self._req("GET", f"/library/sections/{self.section_id}/refresh")

    @staticmethod
    def _items(doc: dict) -> list[PlexItem]:
        out = []
        for md in (doc.get("MediaContainer") or {}).get("Metadata") or []:
            files = [p.get("file", "") for media in md.get("Media") or [] for p in media.get("Part") or []]
            out.append(
                PlexItem(
                    rating_key=str(md.get("ratingKey")),
                    title=md.get("title"),
                    view_count=int(md.get("viewCount") or 0),
                    files=files,
                    grandparent_title=md.get("grandparentTitle"),
                    guid=md.get("guid"),
                )
            )
        return out

    async def find_episode(self, season: int, episode_no: int, filename: str, show_title: str) -> PlexItem | None:
        resp = await self._req(
            "GET",
            f"/library/sections/{self.section_id}/all",
            {"type": 4, "episode.index": episode_no, "season.index": season},
        )
        items = self._items(resp.json())
        hits = [
            i
            for i in items
            if (i.grandparent_title in (None, show_title))
            and (not i.files or any(PurePath(f).name == filename for f in i.files))
        ]
        return hits[0] if len(hits) == 1 else None

    async def find_by_file(self, type_: int, filename: str, params: dict[str, Any] | None = None) -> PlexItem | None:
        """The item of ``type_`` (1 movie, 4 episode) whose media file is called ``filename``."""
        resp = await self._req("GET", f"/library/sections/{self.section_id}/all", {"type": type_, **(params or {})})
        hits = [i for i in self._items(resp.json()) if any(PurePath(f).name == filename for f in i.files)]
        return hits[0] if hits else None

    async def wait_for_file(
        self, type_: int, filename: str, params: dict[str, Any] | None = None, timeout: float = 300.0, first_delay: float = 2.0
    ) -> PlexItem | None:
        deadline = time.monotonic() + timeout
        delay = first_delay
        while True:
            try:
                item = await self.find_by_file(type_, filename, params)
                if item:
                    return item
            except httpx.HTTPError as e:
                log.warning("plex lookup failed", extra={"error": str(e)})
            if time.monotonic() + delay > deadline:
                return None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def machine_id(self) -> str | None:
        resp = await self._req("GET", "/")
        return (resp.json().get("MediaContainer") or {}).get("machineIdentifier")

    async def wait_for_episode(
        self, season: int, episode_no: int, filename: str, show_title: str, timeout: float = 300.0, first_delay: float = 1.0
    ) -> PlexItem | None:
        """Poll with backoff until the item appears (timeout 5 min)."""
        deadline = time.monotonic() + timeout
        delay = first_delay
        while True:
            try:
                item = await self.find_episode(season, episode_no, filename, show_title)
                if item:
                    return item
            except httpx.HTTPError as e:
                log.warning("plex lookup failed", extra={"error": str(e)})
            if time.monotonic() + delay > deadline:
                return None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    # ------------------------------------------------------------------ metadata
    async def set_episode_metadata(self, rating_key: str, title: str, summary: str, date: str) -> None:
        params = {
            "type": 4,
            "id": rating_key,
            "title.value": title,
            "title.locked": 1,
            "summary.value": summary,
            "summary.locked": 1,
            "originallyAvailableAt.value": date,
            "originallyAvailableAt.locked": 1,
        }
        await self._req("PUT", f"/library/sections/{self.section_id}/all", params)

    async def set_summary(self, rating_key: str, summary: str) -> None:
        params = {"type": 4, "id": rating_key, "summary.value": summary, "summary.locked": 1}
        await self._req("PUT", f"/library/sections/{self.section_id}/all", params)

    async def set_poster(self, rating_key: str, image: bytes | str) -> None:
        """Upload ``image`` (PNG/JPEG bytes, or a URL Pitlane downloads first) as the poster.

        The image is sent as the request body. Since PMS 1.43.5, ``POST …/posters?url=``
        answers 502 for an image on a local address (such as Pitlane's title cards),
        while an upload is accepted and becomes the selected ``upload://`` poster
        **[verified 2026-10-26, PMS 1.43.5]**."""
        if isinstance(image, str):
            resp = await self.http.get(image, policy=self.policy)
            data, ctype = resp.content, resp.headers.get("content-type", "image/png")
        else:
            data, ctype = image, "image/jpeg" if image[:3] == b"\xff\xd8\xff" else "image/png"
        await self._req("POST", f"/library/metadata/{rating_key}/posters", content=data, content_type=ctype)

    async def poster_selected_upload(self, rating_key: str) -> bool:
        """REP-5 step 4 verification: is an ``upload://`` poster the selected one?

        Real PMS returns keys like ``/library/metadata/5655/file?url=upload%3A%2F%2Fposters%2F…``
        (the poster URI URL-encoded in a ``url`` parameter), so keys are decoded first."""
        resp = await self._req("GET", f"/library/metadata/{rating_key}/posters")
        for md in (resp.json().get("MediaContainer") or {}).get("Metadata") or []:
            selected = md.get("selected")
            if poster_uri(str(md.get("key", ""))).startswith("upload://") and selected in (True, 1, "1", "true"):
                return True
        return False

    async def set_title(self, rating_key: str, type_: int, title: str) -> None:
        params = {"type": type_, "id": rating_key, "title.value": title, "title.locked": 1}
        await self._req("PUT", f"/library/sections/{self.section_id}/all", params)

    async def metadata(self, rating_key: str, token: str | None = None) -> PlexItem | None:
        resp = await self._req("GET", f"/library/metadata/{rating_key}", token=token)
        items = self._items(resp.json())
        return items[0] if items else None

    async def view_count(self, rating_key: str, token: str | None = None) -> int:
        item = await self.metadata(rating_key, token=token)
        return item.view_count if item else 0

    async def item_parents(self, rating_key: str) -> tuple[str | None, str | None]:
        """(showKey, seasonKey) of an episode."""
        resp = await self._req("GET", f"/library/metadata/{rating_key}")
        md = ((resp.json().get("MediaContainer") or {}).get("Metadata") or [{}])[0]
        gp = md.get("grandparentRatingKey")
        p = md.get("parentRatingKey")
        return (str(gp) if gp else None, str(p) if p else None)

    async def delete_item(self, rating_key: str) -> None:
        """REP-8: deletion goes through PMS so the library stays consistent."""
        await self._req("DELETE", f"/library/metadata/{rating_key}")
