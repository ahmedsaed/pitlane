"""A fake Plex Media Server (HTTP stub) that records scan and metadata calls (SPEC §6).

It implements only the documented endpoints Pitlane uses. A targeted scan
"discovers" media files in the scanned folder (read from the local disk) and
assigns rating keys; episode/season indexes are read from ``SxxxxEyyyy``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

EP = re.compile(r"S(\d{4})E(\d{4})")


@dataclass
class Episode:
    rating_key: str
    file: str
    season: int
    index: int
    title: str = ""
    summary: str = ""
    view_count: int = 0
    locked: set[str] = field(default_factory=set)
    posters: list[str] = field(default_factory=list)  # every upload URL received
    poster_list: list[dict] = field(default_factory=list)  # what GET …/posters returns


class FakePMS:
    SHOW_KEY = "100"

    def __init__(self, token: str, section_id: int = 7) -> None:
        self.token = token
        self.section_id = section_id
        self.calls: list[dict] = []
        self.episodes: dict[str, Episode] = {}
        self.titles: dict[str, str] = {}
        self.deleted: list[str] = []
        self.user_views: dict[str, dict[str, int]] = {}  # token → ratingKey → viewCount
        self._next = 1000
        # Simulates PMS discarding uploads made while it still processes a new item:
        # the next N uploads return 200, are recorded, but are not kept.
        self.discard_uploads = 0
        self.app = Starlette(
            routes=[
                Route("/library/sections/{sid}/refresh", self.refresh, methods=["GET"]),
                Route("/library/sections/{sid}/all", self.all_get, methods=["GET"]),
                Route("/library/sections/{sid}/all", self.all_put, methods=["PUT"]),
                Route("/library/metadata/{rk}/posters", self.posters, methods=["POST"]),
                Route("/library/metadata/{rk}/posters", self.poster_list, methods=["GET"]),
                Route("/library/metadata/{rk}", self.metadata, methods=["GET"]),
                Route("/library/metadata/{rk}", self.delete, methods=["DELETE"]),
            ]
        )

    def _record(self, request: Request) -> JSONResponse | None:
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.query_params),
                "token": request.headers.get("x-plex-token"),
            }
        )
        if request.headers.get("x-plex-token") not in {self.token, *self.user_views}:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    @staticmethod
    def _poster_key(rk: str, uri: str) -> str:
        """Real PMS format: the poster URI URL-encoded inside a ``url`` parameter."""
        from urllib.parse import quote

        return f"/library/metadata/{rk}/file?url={quote(uri, safe='')}"

    def _season_key(self, season: int) -> str:
        return f"2{season}"

    def _md(self, e: Episode, view_count: int | None = None) -> dict:
        return {
            "ratingKey": e.rating_key,
            "type": "episode",
            "title": e.title,
            "summary": e.summary,
            "index": e.index,
            "parentIndex": e.season,
            "grandparentTitle": "Formula 1",
            "grandparentRatingKey": self.SHOW_KEY,
            "parentRatingKey": self._season_key(e.season),
            "viewCount": e.view_count if view_count is None else view_count,
            "Media": [{"Part": [{"file": e.file}]}],
        }

    async def refresh(self, request: Request):
        if err := self._record(request):
            return err
        folder = Path(request.query_params.get("path", ""))
        if folder.is_dir():
            for f in sorted(folder.iterdir()):
                m = EP.search(f.name)
                if not f.is_file() or not m or any(e.file == str(f) for e in self.episodes.values()):
                    continue
                self._next += 1
                rk = str(self._next)
                ep = Episode(rk, str(f), int(m.group(1)), int(m.group(2)), title=f.stem)
                ep.poster_list = [{"key": self._poster_key(rk, f"media://2/{rk}.bundle/Contents/Thumbnails/thumb1.jpg"), "selected": True}]
                self.episodes[rk] = ep
        return Response(status_code=200)

    async def all_get(self, request: Request):
        if err := self._record(request):
            return err
        q = request.query_params
        hits = [
            self._md(e)
            for e in self.episodes.values()
            if str(e.index) == q.get("episode.index") and str(e.season) == q.get("season.index")
        ]
        return JSONResponse({"MediaContainer": {"size": len(hits), "Metadata": hits}})

    async def all_put(self, request: Request):
        if err := self._record(request):
            return err
        q = request.query_params
        rk = q.get("id", "")
        if rk in self.episodes:
            e = self.episodes[rk]
            if "title.value" in q:
                e.title = q["title.value"]
            if "summary.value" in q:
                e.summary = q["summary.value"]
            e.locked |= {k.split(".")[0] for k in q if k.endswith(".locked") and q[k] == "1"}
        elif "title.value" in q:
            self.titles[rk] = q["title.value"]
        return Response(status_code=200)

    async def posters(self, request: Request):
        if err := self._record(request):
            return err
        rk = request.path_params["rk"]
        if request.query_params.get("url"):
            # PMS 1.43.5: a poster by URL on a local address is refused [verified 2026-10-26]
            return Response(status_code=502)
        body = await request.body()
        if rk in self.episodes:
            e = self.episodes[rk]
            if not body.startswith(b"\x89PNG"):
                return Response(status_code=400)
            e.posters.append(f"png:{len(body)}")
            if self.discard_uploads > 0:
                self.discard_uploads -= 1  # 200, card "fetched", but not kept
            else:
                for entry in e.poster_list:
                    entry["selected"] = False
                uri = f"upload://posters/seasons/{e.season}/episodes/{e.index}/{len(e.posters)}"
                e.poster_list.append({"key": self._poster_key(rk, uri), "selected": True})
        return Response(status_code=200)

    async def poster_list(self, request: Request):
        if err := self._record(request):
            return err
        e = self.episodes.get(request.path_params["rk"])
        if e is None:
            return JSONResponse({"MediaContainer": {"size": 0}}, status_code=404)
        return JSONResponse({"MediaContainer": {"size": len(e.poster_list), "Metadata": e.poster_list}})

    async def metadata(self, request: Request):
        if err := self._record(request):
            return err
        rk = request.path_params["rk"]
        e = self.episodes.get(rk)
        if e is None:
            return JSONResponse({"MediaContainer": {"size": 0}}, status_code=404)
        tok = request.headers.get("x-plex-token")
        vc = self.user_views.get(tok, {}).get(rk, 0) if tok in self.user_views else None
        return JSONResponse({"MediaContainer": {"size": 1, "Metadata": [self._md(e, vc)]}})

    async def delete(self, request: Request):
        if err := self._record(request):
            return err
        rk = request.path_params["rk"]
        self.episodes.pop(rk, None)
        self.deleted.append(rk)
        return Response(status_code=200)

    def written_strings(self) -> list[str]:
        """Every string value Pitlane wrote to PMS."""
        out = []
        for c in self.calls:
            if c["method"] in ("PUT", "POST"):
                out += [v for k, v in c["params"].items() if k.endswith(".value") or k == "url"]
        return out
