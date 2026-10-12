"""Requests, downloads, follows and retention for movies and shows (docs/vod.md §5).

Downloads share the provider's connection limit at the lowest priority
(VOD-4): a live tune cancels the running download, which goes back to the
queue and resumes from its ``.part`` file with an HTTP ``Range`` request.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from ..logging import correlation, get_logger, new_correlation_id
from ..providers.base import ProviderError, describe_non_media, looks_like_page
from ..replays.plex import PlexClient
from ..state import now_iso
from ..xmltv import session_window
from . import catalog
from .names import clean_title, episode_relpath, movie_relpath, parse_year, year_from_date

if TYPE_CHECKING:
    from ..app import App

log = get_logger("vod")

PROGRESS_EVERY_S = 2.0
MAX_ATTEMPTS = 3
PORTAL_PAUSE_S = 1800  # the ISP quota page usually lasts a while
DETAILS_MAX_AGE_S = 30 * 86400
ACTIVE = ("queued", "running")


class PortalPage(Exception):
    """The panel answered with a web page (ISP quota/captive portal or a panel error page)."""


class Incomplete(Exception):
    pass


def _secs(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _media(info: dict[str, Any]) -> dict[str, Any]:
    """Size/quality fields shared by movie and episode details."""
    video = info.get("video") if isinstance(info.get("video"), dict) else {}
    audio = info.get("audio") if isinstance(info.get("audio"), dict) else {}
    dur = _secs(info.get("duration_secs"))
    if dur is None and isinstance(info.get("duration"), str) and info["duration"].count(":") == 2:
        h, m, s = info["duration"].split(":")
        dur = int(h) * 3600 + int(m) * 60 + int(float(s))
    bitrate = _secs(info.get("bitrate"))  # kbit/s
    return {
        "duration_s": dur,
        "bitrate": bitrate,
        "width": _secs(video.get("width")),
        "height": _secs(video.get("height")),
        "vcodec": video.get("codec_name"),
        "acodec": audio.get("codec_name"),
        "size": int(bitrate * 1000 / 8 * dur) if bitrate and dur else None,
    }


class VodManager:
    def __init__(self, app: "App") -> None:
        self.app = app
        self._tasks: list[asyncio.Task] = []
        self._wake = asyncio.Event()
        self._retry_after: dict[str, float] = {}
        self._catalog_failed_at = 0.0
        self.paused_until = 0.0
        self.blocked: str | None = None  # why the queue is waiting, for the UI
        self.current: dict[str, Any] | None = None  # running download: key, done, total, rate
        self._machine_id: str | None = None

    # ------------------------------------------------------------------ config/wiring
    @property
    def cfg(self):  # type: ignore[no-untyped-def]
        return self.app.cfg.vod

    @property
    def state(self):  # type: ignore[no-untyped-def]
        return self.app.state

    @property
    def provider(self) -> Any:
        p = self.app.registry.get(self.cfg.provider) if self.app.registry else None
        if p is None or "vod" not in getattr(p, "capabilities", set()):
            raise ProviderError(f'vod provider "{self.cfg.provider}" is not running')
        return p

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.app.cfg.epg.timezone)

    def plex(self, kind: str) -> PlexClient:
        lib = self.cfg.movies if kind == "movie" else self.cfg.shows
        return PlexClient(self.app.http, self.app.cfg.plex.url or "", self.app.cfg.plex.token or "", lib.section_id)

    def root(self, kind: str) -> Path:
        return Path(self.cfg.movies.root if kind == "movie" else self.cfg.shows.root)

    async def start(self, background: bool = True) -> None:
        for k in ("movie", "episode"):
            try:
                self.root(k).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log.error("vod library folder unavailable", extra={"path": str(self.root(k)), "error": str(e)})
        # A download interrupted by a restart resumes from its .part file.
        self.state.execute("UPDATE vod_items SET status='queued' WHERE status='running'")
        if background:
            # A restart between a finished download and its Plex scan would leave it unscanned.
            for row in self.state.query("SELECT key FROM vod_items WHERE status='done' AND match_state IS NULL"):
                self._spawn(self._after_download(row["key"]), f"vod-plex-{row['key']}")
            self._spawn(self._catalog_loop(), "vod-catalog")
            self._spawn(self._worker(), "vod-downloads")
            self._spawn(self._retention_loop(), "vod-retention")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    def _spawn(self, coro: Any, name: str) -> None:
        self._tasks.append(asyncio.get_running_loop().create_task(coro, name=name))

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ catalogue (VOD-1)
    async def refresh_catalog(self, force: bool = False) -> dict[str, Any] | None:
        if not force and not catalog.is_due(self.state, self.cfg.catalog_refresh.total_seconds()):
            return None
        doc = await self.provider.vod_catalog()
        stats = await asyncio.to_thread(catalog.apply_catalog, self.state, doc)
        await self.check_follows(stats["changed_series"])
        return stats

    async def _catalog_loop(self) -> None:
        while True:
            if time.time() - self._catalog_failed_at > 3600:
                try:
                    await self.refresh_catalog()
                except Exception as e:
                    self._catalog_failed_at = time.time()
                    log.warning("vod catalogue refresh failed", extra={"error": str(e)})
            await asyncio.sleep(600)

    # ------------------------------------------------------------------ details (VOD-3)
    def _cached(self, kind: str, item_id: int, max_age_s: float | None) -> dict[str, Any] | None:
        row = self.state.one("SELECT doc, fetched_at FROM vod_details WHERE kind=? AND id=?", (kind, item_id))
        if not row:
            return None
        if max_age_s is not None:
            age = (datetime.fromisoformat(now_iso()) - datetime.fromisoformat(row["fetched_at"])).total_seconds()
            if age > max_age_s:
                return None
        return json.loads(row["doc"])

    def _store(self, kind: str, item_id: int, doc: dict[str, Any]) -> None:
        self.state.execute(
            "INSERT INTO vod_details(kind, id, doc, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(kind, id) DO UPDATE SET doc=excluded.doc, fetched_at=excluded.fetched_at",
            (kind, item_id, json.dumps(doc), now_iso()),
        )

    async def movie(self, vod_id: int, fetch: bool = True) -> dict[str, Any] | None:
        """Movie details (one ``get_vod_info`` the first time, then cached)."""
        doc = self._cached("movie", vod_id, DETAILS_MAX_AGE_S)
        if doc is None:
            if not fetch:
                return None
            doc = await self.provider.vod_info(vod_id)
            self._store("movie", vod_id, doc)
        info = doc.get("info") or {}
        md = doc.get("movie_data") or {}
        row = self.state.one("SELECT * FROM vod_titles WHERE id=?", (vod_id,)) or {}
        name = str(info.get("name") or md.get("name") or row.get("name") or f"Movie {vod_id}")
        year = year_from_date(info.get("releasedate")) or parse_year(str(md.get("name") or "")) or row.get("year")
        tmdb = str(info.get("tmdb_id") or "").strip() or None
        if tmdb in ("0",):
            tmdb = None
        backdrop = info.get("backdrop_path")
        return {
            "id": vod_id,
            "title": clean_title(name),
            "year": year,
            "tmdb": tmdb,
            "plot": info.get("plot") or info.get("description"),
            "cast": info.get("cast") or info.get("actors"),
            "director": info.get("director"),
            "genre": info.get("genre"),
            "rating": info.get("rating") or row.get("rating"),
            "poster": info.get("movie_image") or info.get("cover_big") or row.get("poster"),
            "backdrop": backdrop[0] if isinstance(backdrop, list) and backdrop else None,
            "ext": str(md.get("container_extension") or row.get("ext") or "mkv").lower(),
            "category": row.get("category"),
            "cam": bool(row.get("cam")),
            **_media(info),
        }

    async def series(self, series_id: int, fetch: bool = True, force: bool = False) -> dict[str, Any] | None:
        """Series details with seasons and episodes (one ``get_series_info``, cached until the
        catalogue says the series changed)."""
        row = self.state.one("SELECT * FROM vod_series WHERE id=?", (series_id,)) or {}
        doc = None if force else self._cached("series", series_id, None)
        if doc is not None and row.get("last_modified") and int(doc.get("_last_modified") or 0) < int(row["last_modified"]):
            doc = None  # changed on the panel since we fetched it
        if doc is None:
            if not fetch:
                return None
            doc = await self.provider.series_info(series_id)
            doc["_last_modified"] = row.get("last_modified") or 0
            self._store("series", series_id, doc)
        info = doc.get("info") or {}
        name = str(info.get("name") or row.get("name") or f"Series {series_id}")
        year = year_from_date(info.get("releaseDate")) or row.get("year") or parse_year(name)
        override = self.state.one("SELECT tmdb FROM vod_series_ids WHERE series_id=?", (series_id,))
        seasons: dict[int, list[dict[str, Any]]] = {}
        eps = doc.get("episodes") or {}
        if isinstance(eps, list):  # some panels send a list of seasons
            eps = {str(i + 1): v for i, v in enumerate(eps)}
        for skey, lst in eps.items():
            for e in lst or []:
                if not isinstance(e, dict) or e.get("id") in (None, ""):
                    continue
                season = _secs(e.get("season")) or _secs(skey) or 1
                ei = e.get("info") if isinstance(e.get("info"), dict) else {}
                seasons.setdefault(season, []).append({
                    "id": str(e["id"]),
                    "season": season,
                    "episode": _secs(e.get("episode_num")) or len(seasons.get(season, [])) + 1,
                    "title": str(e.get("title") or ""),
                    "ext": str(e.get("container_extension") or "mkv").lower(),
                    "added": _secs(e.get("added")) or 0,
                    "plot": ei.get("plot"),
                    **_media(ei),
                })  # fmt: skip
        for lst in seasons.values():
            lst.sort(key=lambda e: e["episode"])
        return {
            "id": series_id,
            "title": clean_title(name),
            "year": year,
            "tmdb": override["tmdb"] if override else None,
            "plot": info.get("plot") or row.get("plot"),
            "cast": info.get("cast"),
            "genre": info.get("genre") or row.get("genre"),
            "rating": info.get("rating") or row.get("rating"),
            "poster": info.get("cover") or row.get("poster"),
            "category": row.get("category"),
            "seasons": dict(sorted(seasons.items())),
        }

    # ------------------------------------------------------------------ requests (VOD-2)
    def _queue(self, key: str, kind: str, panel_id: str, title: str, ext: str, target: Path, expected: int | None,
               series_id: int | None = None, season: int | None = None, episode: int | None = None,
               status: str = "queued", note: str | None = None) -> bool:  # fmt: skip
        row = self.state.one("SELECT status FROM vod_items WHERE key=?", (key,))
        if row and row["status"] not in ("cancelled", "failed", "deleted"):
            if row["status"] == "paused" and status == "queued":
                self._set(key, status="queued", attempts=0, last_error=None)  # a request resumes a partial copy
                self.wake()
                return True
            return False
        self.state.execute(
            "INSERT INTO vod_items(key, kind, panel_id, series_id, season, episode, title, ext, target, expected_size, "
            "bytes_done, status, attempts, last_error, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,0,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET status=excluded.status, attempts=0, last_error=excluded.last_error, target=excluded.target, "
            "expected_size=excluded.expected_size, updated_at=excluded.updated_at",
            (key, kind, panel_id, series_id, season, episode, title, ext, str(target), expected, status, note, now_iso(), now_iso()),
        )
        self._retry_after.pop(key, None)
        self.wake()
        return True

    async def request_movie(self, vod_id: int) -> bool:
        m = await self.movie(vod_id)
        assert m is not None
        target = self.root("movie") / movie_relpath(m["title"], m["year"], m["tmdb"], m["ext"])
        label = f"{m['title']} ({m['year']})" if m["year"] else m["title"]
        queued = self._queue(f"movie:{vod_id}", "movie", str(vod_id), label, m["ext"], target, m["size"])
        if queued:
            log.info("vod requested", extra={"title": label})
        return queued

    async def request_episodes(self, series_id: int, episode_ids: list[str]) -> int:
        s = await self.series(series_id)
        assert s is not None
        wanted = set(episode_ids)
        n = 0
        for eps in s["seasons"].values():
            for e in eps:
                if e["id"] not in wanted:
                    continue
                target = self.root("episode") / episode_relpath(s["title"], s["year"], e["season"], e["episode"], e["ext"], s["tmdb"])
                label = f"{s['title']} S{e['season']:02d}E{e['episode']:02d}"
                n += self._queue(f"ep:{e['id']}", "episode", e["id"], label, e["ext"], target, e["size"], series_id, e["season"], e["episode"])
        if n:
            log.info("vod requested", extra={"title": s["title"], "episodes": n})
        return n

    async def follow(self, series_id: int, from_season: int | None = None) -> None:
        """New episodes (added to the panel after now) of ``from_season`` and later are requested."""
        s = await self.series(series_id)
        row = self.state.one("SELECT last_modified FROM vod_series WHERE id=?", (series_id,))
        self.state.execute(
            "INSERT INTO vod_follows(series_id, from_season, last_modified, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(series_id) DO UPDATE SET from_season=excluded.from_season",
            (series_id, from_season or (max(s["seasons"]) if s and s["seasons"] else 1), row["last_modified"] if row else 0, now_iso()),
        )

    def unfollow(self, series_id: int) -> None:
        self.state.execute("DELETE FROM vod_follows WHERE series_id=?", (series_id,))

    def set_series_tmdb(self, series_id: int, tmdb: str | None) -> None:
        tmdb = (tmdb or "").strip()
        if tmdb and not tmdb.isdigit():
            raise ValueError("TMDB ID must be a number")
        if tmdb:
            self.state.execute(
                "INSERT INTO vod_series_ids(series_id, tmdb) VALUES(?,?) ON CONFLICT(series_id) DO UPDATE SET tmdb=excluded.tmdb",
                (series_id, tmdb),
            )
        else:
            self.state.execute("DELETE FROM vod_series_ids WHERE series_id=?", (series_id,))

    async def check_follows(self, changed: list[int]) -> int:
        """§5.4: one ``get_series_info`` per followed series that changed on the panel."""
        follows = {r["series_id"]: r for r in self.state.query("SELECT * FROM vod_follows")}
        n = 0
        for sid in [s for s in changed if s in follows]:
            f = follows[sid]
            try:
                s = await self.series(sid, force=True)
            except ProviderError as e:
                log.warning("followed series check failed", extra={"series": sid, "error": str(e)})
                continue
            since = datetime.fromisoformat(f["created_at"]).timestamp()
            new = [
                e["id"]
                for season, eps in (s["seasons"] if s else {}).items()
                if season >= (f["from_season"] or 1)
                for e in eps
                if e["added"] >= since and not self.state.one("SELECT 1 FROM vod_items WHERE key=?", (f"ep:{e['id']}",))
            ]
            if new:
                n += await self.request_episodes(sid, new)
            row = self.state.one("SELECT last_modified FROM vod_series WHERE id=?", (sid,))
            self.state.execute("UPDATE vod_follows SET last_modified=? WHERE series_id=?", (row["last_modified"] if row else 0, sid))
        return n

    # ------------------------------------------------------------------ play now (docs/vod.md)
    def selection(self) -> dict[str, Any] | None:
        raw = self.state.get("vod.play")
        return json.loads(raw) if raw else None

    async def play_movie(self, vod_id: int) -> dict[str, Any]:
        m = await self.movie(vod_id)
        assert m is not None
        label = f"{m['title']} ({m['year']})" if m["year"] else m["title"]
        target = self.root("movie") / movie_relpath(m["title"], m["year"], m["tmdb"], m["ext"])
        return self._select({"key": f"movie:{vod_id}", "kind": "movie", "panel_id": str(vod_id), "ext": m["ext"],
                             "title": label, "target": str(target), "expected": m["size"]})  # fmt: skip

    async def play_episode(self, series_id: int, episode_id: str) -> dict[str, Any]:
        s = await self.series(series_id)
        assert s is not None
        for eps in s["seasons"].values():
            for e in eps:
                if e["id"] == episode_id:
                    target = self.root("episode") / episode_relpath(s["title"], s["year"], e["season"], e["episode"], e["ext"], s["tmdb"])
                    return self._select({
                        "key": f"ep:{e['id']}", "kind": "episode", "panel_id": e["id"], "ext": e["ext"],
                        "title": f"{s['title']} S{e['season']:02d}E{e['episode']:02d}", "target": str(target),
                        "expected": e["size"], "series_id": series_id, "season": e["season"], "episode": e["episode"],
                    })  # fmt: skip
        raise ProviderError(f"episode {episode_id} is not on the panel")

    def _select(self, sel: dict[str, Any]) -> dict[str, Any]:
        """Put ``sel`` on the play channel; a stream already on it switches to the new title."""
        from ..config import PLAY_CHANNEL_ID

        self.state.set("vod.play", json.dumps(sel))
        switched = 0
        for session in list(self.app.pool.in_use.values()):
            if session.channel.id == PLAY_CHANNEL_ID:
                session.request_restart()
                switched += 1
        log.info("play now", extra={"title": sel["title"], "switched": switched})
        return {**sel, "switched": switched}

    def playing(self) -> dict[str, Any] | None:
        from ..config import PLAY_CHANNEL_ID

        sel = self.selection()
        pc = self.cfg.play_channel
        if sel is None or pc is None:
            return None
        active = any(s.channel.id == PLAY_CHANNEL_ID for s in self.app.pool.in_use.values())
        return {"key": sel["key"], "title": sel["title"], "channel": pc.guide_number, "channel_name": pc.name, "active": active}

    async def open_play(self, tracker: Any) -> Any:
        """The relay input for the play channel (unstarted)."""
        from ..providers.base import OpenedInput
        from ..relay import FfmpegInput, InputError, build_ffmpeg_args
        from .play import LocalFileInput, PlayInput, can_pipe

        sel = self.selection()
        if sel is None:
            raise InputError("nothing picked: use Play now in Pitlane first")
        target = Path(sel["target"])
        row = self.state.one("SELECT status FROM vod_items WHERE key=?", (sel["key"],))
        if row and row["status"] == "done" and target.exists():
            log.info("play now: from the library", extra={"title": sel["title"]})
            return LocalFileInput.for_path(target, tracker)
        provider = self.provider
        url = provider.movie_url(int(sel["panel_id"]), sel["ext"]) if sel["kind"] == "movie" else provider.episode_url(sel["panel_id"], sel["ext"])
        if not can_pipe(sel["ext"]):
            # MP4 may keep its index at the end: ffmpeg reads the URL itself (with seeks); no copy is kept.
            return FfmpegInput(build_ffmpeg_args("ts", OpenedInput(url, dict(provider.headers), ["-re"])), tracker)
        save = self.cfg.play_channel is not None and self.cfg.play_channel.save
        if save:
            self._queue(sel["key"], sel["kind"], sel["panel_id"], sel["title"], sel["ext"], target, sel.get("expected"),
                        sel.get("series_id"), sel.get("season"), sel.get("episode"), status="paused", note="saving while watching")  # fmt: skip
        item = self.state.one("SELECT * FROM vod_items WHERE key=?", (sel["key"],)) or {"key": sel["key"], "target": str(target)}
        return PlayInput(self, item, url, dict(provider.headers), tracker, target if save else None)

    def keep_watched(self, item: dict[str, Any], watch: Path | None, received: int, total: int | None, complete: bool) -> None:
        """After playback: a complete copy goes into the library, a partial one replaces a shorter ``.part``."""
        from .play import replace_part

        if watch is None or not watch.exists():
            return
        key = item["key"]
        target = Path(item["target"])
        part = Path(str(target) + ".part")
        size = watch.stat().st_size
        if complete and total and size == total and not target.exists():
            os.replace(watch, target)
            part.unlink(missing_ok=True)
            self._set(key, status="done", bytes_done=size, total_size=size, last_error=None)
            log.info("play now: watched to the end, kept in the library", extra={"title": item.get("title")})
            self._spawn(self._after_download(key), f"vod-plex-{key}")
            return
        replace_part(watch, part)
        done = part.stat().st_size if part.exists() else 0
        row = self.state.one("SELECT status FROM vod_items WHERE key=?", (key,))
        if row and row["status"] not in ("done", "deleted"):
            note = "partly saved while watching; Resume downloads the rest" if row["status"] == "paused" else None
            self._set(key, bytes_done=done, total_size=total, last_error=note)

    # ------------------------------------------------------------------ queue actions
    def item_action(self, key: str, action: str) -> None:
        row = self.state.one("SELECT * FROM vod_items WHERE key=?", (key,))
        if not row:
            raise KeyError(key)
        if action == "pause" and row["status"] in ACTIVE:
            self._set(key, status="paused")
            if self.current and self.current["key"] == key and self.current.get("task"):
                self.current["task"].cancel()
        elif action in ("resume", "retry") and row["status"] in ("paused", "failed", "cancelled"):
            self._set(key, status="queued", attempts=0, last_error=None)
            self._retry_after.pop(key, None)
        elif action == "cancel" and row["status"] not in ("done", "deleted"):
            self._set(key, status="cancelled")
            if self.current and self.current["key"] == key and self.current.get("task"):
                self.current["task"].cancel()
            part = Path(row["target"] + ".part")
            if not Path(row["target"]).exists():
                self._delete_files(part, self.root("movie" if row["kind"] == "movie" else "episode"))  # and empty folders
        else:
            raise ValueError(f"cannot {action} an item that is {row['status']}")
        self.wake()

    def _set(self, key: str, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.state.execute(f"UPDATE vod_items SET {cols} WHERE key=?", (*fields.values(), key))

    # ------------------------------------------------------------------ usage and gates (VOD-6)
    def _today(self) -> str:
        return datetime.now(self.tz).strftime("%Y-%m-%d")

    def _add_usage(self, n: int) -> None:
        self.state.execute(
            "INSERT INTO vod_usage(day, bytes) VALUES(?, ?) ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes",
            (self._today(), n),
        )

    def usage(self) -> dict[str, int]:
        today = self._today()
        day = self.state.one("SELECT bytes FROM vod_usage WHERE day=?", (today,))
        month = self.state.one("SELECT COALESCE(SUM(bytes), 0) AS b FROM vod_usage WHERE day LIKE ?", (today[:7] + "%",))
        return {"day": day["bytes"] if day else 0, "month": month["b"] if month else 0}

    def free_space(self, kind: str) -> int | None:
        try:
            return shutil.disk_usage(self.root(kind)).free
        except OSError:
            return None

    def _in_window(self, now: datetime) -> bool:
        w = self.cfg.download_window
        if not w:
            return True
        a, b = w.split("-")
        t = now.astimezone(self.tz).strftime("%H:%M")
        return a <= t < b if a <= b else (t >= a or t < b)

    def _near_session(self, now: datetime) -> bool:
        quiet = self.cfg.quiet_around_sessions
        if not quiet:
            return False
        for s in self.app.calendar.sessions():
            start, stop = session_window(s, self.app.cfg)
            if start - quiet <= now <= stop:
                return True
        return False

    def gate(self, item: dict[str, Any] | None = None) -> str | None:
        """Why downloads must wait right now, or ``None``."""
        now = self.app.clock()
        if self.paused_until > time.time():
            until = datetime.fromtimestamp(self.paused_until, self.tz).strftime("%H:%M")
            return f"the panel answered with a web page (ISP quota page?); paused until {until}"
        if not self._in_window(now):
            return f"outside the download window {self.cfg.download_window}"
        if self._near_session(now):
            return "an F1 session is on or about to start"
        use = self.usage()
        if self.cfg.max_gb_per_day and use["day"] >= self.cfg.max_gb_per_day * 1e9:
            return "daily data cap reached"
        if self.cfg.max_gb_per_month and use["month"] >= self.cfg.max_gb_per_month * 1e9:
            return "monthly data cap reached"
        if item is not None:
            free = self.free_space(item["kind"])
            need = max(0, (item["total_size"] or item["expected_size"] or 0) - (item["bytes_done"] or 0))
            if free is not None and free - need < self.cfg.min_free_space:
                return "not enough free disk space"
        return None

    # ------------------------------------------------------------------ downloader (VOD-4, VOD-5)
    def next_item(self) -> dict[str, Any] | None:
        now = time.time()
        for row in self.state.query("SELECT * FROM vod_items WHERE status='queued' ORDER BY created_at, key"):
            if self._retry_after.get(row["key"], 0) <= now:
                return row
        return None

    async def _sleep_or_wake(self, seconds: float) -> None:
        """Wait up to ``seconds``; a :meth:`wake` before or during the wait ends it."""
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            pass
        self._wake.clear()

    async def _worker(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vod download worker failed")
                await asyncio.sleep(30)

    async def run_once(self, slot_timeout: float = 60.0) -> str:
        """One pass: wait for an item, the gates and a connection, then download it.
        Returns what happened (``idle``, ``blocked``, ``busy``, ``done``, ``error``, ``preempted``)."""
        item = self.next_item()
        if item is None:
            self.blocked = None
            await self._sleep_or_wake(60)
            return "idle"
        reason = self.gate(item)
        if reason:
            self.blocked = reason
            await self._sleep_or_wake(60)
            return "blocked"
        try:
            provider = self.provider
        except ProviderError as e:
            self.blocked = str(e)
            await self._sleep_or_wake(60)
            return "blocked"
        held = self.app.connections.held_until(provider.name)
        if held is not None:
            until = "until you resume it" if held == float("inf") else "until " + datetime.fromtimestamp(held, self.tz).strftime("%H:%M")
            self.blocked = f"the IPTV connection is freed for you {until}"
            await self._sleep_or_wake(30)
            return "blocked"
        lease = await self.app.connections.download_slot(provider.name, slot_timeout)
        if lease is None:
            self.blocked = "the connection is in use (live TV)"
            return "busy"
        self.blocked = None
        task = asyncio.get_running_loop().create_task(self._download(item, provider), name=f"vod-{item['key']}")
        lease.task = task
        self.current = {"key": item["key"], "title": item["title"], "done": item["bytes_done"], "total": item["total_size"], "rate": 0, "task": task}
        try:
            return await task
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                raise  # shutdown
            row = self.state.one("SELECT status FROM vod_items WHERE key=?", (item["key"],))
            if row and row["status"] == "running":
                self._set(item["key"], status="queued")
                log.info("vod download paused for a live stream", extra={"title": item["title"]})
                self.blocked = "paused for live TV; resumes when the connection is free"
            return "preempted"
        finally:
            self.current = None
            lease.release()

    async def _download(self, item: dict[str, Any], provider: Any) -> str:
        key = item["key"]
        with correlation(new_correlation_id("vod")):
            self._set(key, status="running")
            try:
                await self._fetch(item, provider)
            except asyncio.CancelledError:
                raise
            except PortalPage as e:
                self.paused_until = time.time() + PORTAL_PAUSE_S
                self._set(key, status="queued", last_error=str(e))
                log.warning("vod downloads paused: the panel answered with a web page", extra={"error": str(e)})
                return "error"
            except Exception as e:
                attempts = (item["attempts"] or 0) + 1
                err = str(e) or e.__class__.__name__
                if attempts >= MAX_ATTEMPTS:
                    self._set(key, status="failed", attempts=attempts, last_error=err)
                    log.warning("vod download failed", extra={"title": item["title"], "error": err})
                else:
                    self._set(key, status="queued", attempts=attempts, last_error=err)
                    self._retry_after[key] = time.time() + 60 * 5**attempts
                    log.info("vod download will retry", extra={"title": item["title"], "attempt": attempts, "error": err})
                return "error"
            self._set(key, status="done", last_error=None)
            log.info("vod download finished", extra={"title": item["title"]})
            self._spawn(self._after_download(key), f"vod-plex-{key}")
            return "done"

    async def _fetch(self, item: dict[str, Any], provider: Any) -> None:
        url = provider.movie_url(int(item["panel_id"]), item["ext"]) if item["kind"] == "movie" else provider.episode_url(item["panel_id"], item["ext"])
        target = Path(item["target"])
        part = Path(item["target"] + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        headers = dict(provider.headers)
        if offset:
            headers["Range"] = f"bytes={offset}-"
        timeout = httpx.Timeout(30.0, read=60.0)
        async with self.app.http.raw.stream("GET", url, headers=headers, timeout=timeout) as resp:
            if resp.status_code == 416 and offset:
                total = offset  # the .part already holds the whole file
            else:
                ctype = resp.headers.get("content-type", "")
                if resp.status_code not in (200, 206) or "html" in ctype or "json" in ctype:
                    head = b""
                    async for chunk in resp.aiter_bytes():
                        head += chunk
                        if len(head) >= 4096:
                            break
                    if resp.status_code in (200, 206) or looks_like_page(resp, head):
                        raise PortalPage(describe_non_media(url, resp, head))
                    raise RuntimeError(f"HTTP {resp.status_code}")
                if resp.status_code == 200 and offset:
                    self.state.set("vod.range_supported", "0")
                    log.info("panel ignored the Range header; download restarts", extra={"title": item["title"]})
                    offset = 0
                elif resp.status_code == 206:
                    self.state.set("vod.range_supported", "1")
                total = _total(resp, offset)
                if total:
                    self._set(item["key"], total_size=total)
                await self._write(item, resp, part, offset, total)
        done = part.stat().st_size
        if total and done < total:
            raise Incomplete(f"connection closed at {done} of {total} bytes")
        if not await _playable(part):
            part.unlink(missing_ok=True)
            raise RuntimeError("downloaded file is not a readable video")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)
        self._set(item["key"], bytes_done=done, total_size=total or done)

    async def _write(self, item: dict[str, Any], resp: httpx.Response, part: Path, offset: int, total: int | None) -> None:
        key = item["key"]
        rate = self.cfg.max_rate
        done = offset
        t0 = time.monotonic()
        sent_since = 0
        last_flush = t0
        unflushed = 0
        with open(part, "ab" if offset else "wb") as f:
            try:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
                    done += len(chunk)
                    unflushed += len(chunk)
                    sent_since += len(chunk)
                    now = time.monotonic()
                    if self.current and self.current["key"] == key:
                        self.current["done"] = done
                    if rate:
                        ahead = sent_since / rate - (now - t0)
                        if ahead > 0:
                            await asyncio.sleep(ahead)
                    if now - last_flush >= PROGRESS_EVERY_S:
                        self._progress(key, done, total, unflushed, now - last_flush)
                        last_flush, unflushed = now, 0
            finally:
                f.flush()
                self._progress(key, done, total, unflushed, max(1e-6, time.monotonic() - last_flush))

    def _progress(self, key: str, done: int, total: int | None, delta: int, dt: float) -> None:
        self._set(key, bytes_done=done)
        if delta:
            self._add_usage(delta)
            self.app.metrics.vod_bytes.inc(delta)
        if self.current and self.current["key"] == key:
            self.current.update(done=done, total=total, rate=int(delta / dt) if dt > 0 else 0)

    # ------------------------------------------------------------------ Plex (VOD-7, VOD-8)
    async def machine_id(self) -> str | None:
        if self._machine_id is None:
            try:
                self._machine_id = await self.plex("movie").machine_id()
            except Exception:
                return None
        return self._machine_id

    async def _after_download(self, key: str, timeout: float = 300.0) -> None:
        row = self.state.one("SELECT * FROM vod_items WHERE key=?", (key,))
        if not row:
            return
        kind = "movie" if row["kind"] == "movie" else "episode"
        plex = self.plex(kind)
        target = Path(row["target"])
        try:
            await plex.scan_folder(str(target.parent))
            params = {} if kind == "movie" else {"season.index": row["season"], "episode.index": row["episode"]}
            type_ = 1 if kind == "movie" else 4
            found = await plex.wait_for_file(type_, target.name, params, timeout=min(60.0, timeout))
            if found is None:
                # Observed with a new library: folder scans are ignored until its first full scan.
                log.info("plex folder scan found nothing; scanning the whole library", extra={"title": row["title"]})
                await plex.scan_section()
                found = await plex.wait_for_file(type_, target.name, params, timeout=timeout)
        except Exception as e:
            log.warning("plex scan after download failed", extra={"title": row["title"], "error": str(e)})
            return
        if found is None:
            self._set(key, match_state="not found")
            log.warning("plex did not pick up the download", extra={"title": row["title"]})
            return
        state = "matched" if found.matched else "unmatched"
        self._set(key, plex_rating_key=found.rating_key, match_state=state)
        if state == "unmatched":
            log.warning("plex could not match a download; use Fix Match", extra={"title": row["title"]})
        if self.cfg.notify_ready and self.app.notifier is not None:
            lib = "IPTV Movies" if kind == "movie" else "IPTV Shows"
            self.app.notifier.notify(f"Ready to watch: {row['title']}", f"{row['title']} is in {lib}.")

    # ------------------------------------------------------------------ retention (VOD-9)
    async def retention_once(self) -> list[dict[str, Any]]:
        """Note when Plex first reports an item watched; delete it ``delete_after_watched`` later."""
        acted: list[dict[str, Any]] = []
        after = self.cfg.delete_after_watched
        now = datetime.fromisoformat(now_iso())
        for row in self.state.query("SELECT * FROM vod_items WHERE status='done' AND plex_rating_key IS NOT NULL"):
            kind = "movie" if row["kind"] == "movie" else "episode"
            if not row["watched_at"]:
                try:
                    views = await self.plex(kind).view_count(row["plex_rating_key"])
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404 and not Path(row["target"]).exists():
                        self._set(row["key"], status="deleted")
                    continue
                except httpx.HTTPError:
                    continue
                if views > 0:
                    self._set(row["key"], watched_at=now_iso())
                continue
            if after is None or now - datetime.fromisoformat(row["watched_at"]) < after:
                continue
            if self.cfg.dry_run_retention:
                if row["last_error"] != "retention dry-run: would delete":
                    self._set(row["key"], last_error="retention dry-run: would delete")
                    log.info("vod retention (dry-run) would delete", extra={"title": row["title"]})
                acted.append({**row, "dry_run": True})
                continue
            self._delete_files(Path(row["target"]), self.root(kind))
            self._set(row["key"], status="deleted", last_error=None)
            log.info("vod retention deleted", extra={"title": row["title"]})
            try:
                await self.plex(kind).scan_folder(str(Path(row["target"]).parent))
            except httpx.HTTPError:
                pass
            acted.append({**row, "dry_run": False})
        return acted

    @staticmethod
    def _delete_files(target: Path, root: Path) -> None:
        target.unlink(missing_ok=True)
        d = target.parent
        while d != root and root in d.parents:
            try:
                d.rmdir()  # only when empty
            except OSError:
                break
            d = d.parent

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self.retention_once()
            except Exception:
                log.exception("vod retention failed")

    # ------------------------------------------------------------------ UI helpers
    def summary(self) -> str:
        q = self.state.one("SELECT COUNT(*) n FROM vod_items WHERE status IN ('queued','running')")
        n = q["n"] if q else 0
        if self.current and self.current.get("total"):
            pct = int(100 * (self.current["done"] or 0) / self.current["total"])
            return f"VOD: downloading {pct}%" + (f", {n - 1} more queued" if n > 1 else "")
        if n:
            return f"VOD: {n} queued" + (f" ({self.blocked})" if self.blocked else "")
        return "VOD: idle"


def _total(resp: httpx.Response, offset: int) -> int | None:
    cr = resp.headers.get("content-range", "")
    if "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
        return int(cr.rsplit("/", 1)[1])
    cl = resp.headers.get("content-length")
    if cl and cl.isdigit():
        return int(cl) + (offset if resp.status_code == 206 else 0)
    return None


async def _playable(path: Path) -> bool:
    """ffprobe can read the container and finds a duration (skipped without ffprobe)."""
    exe = shutil.which("ffprobe")
    if not exe:
        return True
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-show_entries", "format=duration", "-of", "json", "--", str(path),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 120)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    try:
        return float(json.loads(out or b"{}").get("format", {}).get("duration") or 0) > 0
    except (ValueError, TypeError):
        return False


def fmt_size(n: int | None) -> str:
    if not n:
        return "?"
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def fmt_duration(s: int | None) -> str:
    if not s:
        return ""
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m"

