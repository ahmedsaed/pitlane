"""Replay pipeline: inbox watcher → matcher → filer → Plex (REP-1 … REP-8).

Each processed file runs under its own correlation ID (OPS-5).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..logging import correlation, get_logger, new_correlation_id
from ..models import SessionType
from ..state import now_iso
from ..xmltv import session_window
from .filer import Filer, sha256_async
from .matcher import Match, Matcher, NoMatch, ffprobe_tags
from .plex import PlexClient
from .spoilers import RESULT_KIND, episode_title, results_text, revealed_summary, safe_summary

if TYPE_CHECKING:
    from ..app import App

log = get_logger("replays")

MIN_SIZE = 5 * 1024 * 1024
IGNORED_SUFFIXES = (".part", ".tmp")
SPOILER_SYNC = timedelta(minutes=10)
# REP-5 step 4: re-upload backoff when PMS discarded the card (gives up after ~10 min).
POSTER_RETRY_DELAYS = (30.0, 60.0, 120.0, 240.0)


@dataclass
class _Seen:
    size: int
    mtime: float
    stable_since: float


def is_ignored(path: Path, inbox: Path) -> bool:
    """docs/replays.md: names starting with '.', .part/.tmp, and anything under a dot-dir (.grab/, .dl/)."""
    try:
        rel = path.relative_to(inbox)
    except ValueError:
        return True
    if any(part.startswith(".") for part in rel.parts):
        return True
    return path.name.lower().endswith(IGNORED_SUFFIXES)


class ReplayPipeline:
    def __init__(self, app: "App", probe=ffprobe_tags, plex_poll_timeout: float = 300.0) -> None:
        self.app = app
        self.probe = probe
        self.plex_poll_timeout = plex_poll_timeout
        self._seen: dict[str, _Seen] = {}
        self._busy: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._jobs: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._download_sem: asyncio.Semaphore | None = None
        self.poster_retry_delays: tuple[float, ...] = POSTER_RETRY_DELAYS
        self.poster_tasks: set[asyncio.Task] = set()
        self.reconfigure()

    # ------------------------------------------------------------------ config
    def reconfigure(self) -> None:
        cfg = self.app.cfg
        r = cfg.replays
        assert r is not None
        self.rcfg = r
        self.series_title = next(iter(cfg.series.values())).title if cfg.series else "Formula 1"
        self.inboxes = [Path(p) for p in r.inboxes]
        self.review_dir = Path(r.needs_review_dir)
        self.library_root = Path(r.library_root)
        self.settle = r.settle_time.total_seconds()
        self.filer = Filer(
            self.app.state,
            self.library_root,
            series_title=self.series_title,
            file_mode=r.file_mode,
            supplementary_mode=r.supplementary_mode,
            remux_to=r.remux_to,
        )
        self.matcher = Matcher(
            lambda: self.app.calendar,
            lambda s: session_window(s, self.app.cfg),
            series_title=self.series_title,
            probe=self.probe,
            assignment_fn=self.app.state.assignment,
        )
        p = cfg.plex
        self.plex = PlexClient(self.app.http, p.url, p.token, p.library_section_id) if p.url and p.token else None
        self._download_sem = asyncio.Semaphore(r.downloads.concurrency)

    # ------------------------------------------------------------------ lifecycle
    async def start(self, background: bool = True) -> None:
        for d in [*self.inboxes, self.review_dir, self.library_root]:
            d.mkdir(parents=True, exist_ok=True)
        # REP-4: downloads interrupted by a restart are queued again.
        self.app.state.execute("UPDATE replay_jobs SET status='queued' WHERE status='running'")
        if background:
            loop = asyncio.get_running_loop()
            self._tasks += [
                loop.create_task(self._scan_loop(), name="inbox-scan"),
                loop.create_task(self._watch_loop(), name="inbox-watch"),
                loop.create_task(self._spoiler_loop(), name="spoiler-sync"),
                loop.create_task(self._download_loop(), name="replay-poll"),
            ]

    async def stop(self) -> None:
        for t in [*self._tasks, *self._jobs, *self.poster_tasks]:
            t.cancel()
        for t in [*self._tasks, *self._jobs, *self.poster_tasks]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    # ------------------------------------------------------------------ watching (REP-1)
    def _excluded(self, p: Path) -> bool:
        for ex in (self.review_dir, self.library_root):
            try:
                p.relative_to(ex)
                return True
            except ValueError:
                pass
        return False

    def _candidates(self) -> list[tuple[Path, Path]]:
        out = []
        for inbox in self.inboxes:
            if not inbox.is_dir():
                continue
            for root, dirs, files in os.walk(inbox):
                rp = Path(root)
                dirs[:] = [d for d in dirs if not d.startswith(".") and not self._excluded(rp / d)]
                for f in files:
                    p = rp / f
                    if not is_ignored(p, inbox) and not self._excluded(p):
                        out.append((inbox, p))
        return out

    def _is_download_owned(self, p: Path) -> bool:
        return self.app.state.one("SELECT 1 AS x FROM replay_jobs WHERE status='running' AND locator=?", (str(p),)) is not None

    async def scan_once(self, now: float | None = None) -> list[Path]:
        """One pass: returns files that became ready (size+mtime stable for settle_time)."""
        now = time.monotonic() if now is None else now
        ready: list[Path] = []
        present = set()
        for _, p in await asyncio.to_thread(self._candidates):
            key = str(p)
            present.add(key)
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < MIN_SIZE:
                continue
            seen = self._seen.get(key)
            if seen is None or seen.size != st.st_size or seen.mtime != st.st_mtime:
                self._seen[key] = _Seen(st.st_size, st.st_mtime, now)
                if self.settle > 0:
                    continue
                seen = self._seen[key]
            if now - seen.stable_since < self.settle or key in self._busy or self._is_download_owned(p):
                continue
            if self._already_done(p, st):
                continue
            ready.append(p)
        for k in list(self._seen):
            if k not in present:
                del self._seen[k]
        return ready

    def _already_done(self, p: Path, st: os.stat_result) -> bool:
        row = self.app.state.one("SELECT size, mtime FROM inbox_seen WHERE path=?", (str(p),))
        return bool(row and row["size"] == st.st_size and row["mtime"] == st.st_mtime)

    def _mark_done(self, p: Path, st: os.stat_result | None, outcome: str) -> None:
        if st is None:
            return
        self.app.state.execute(
            "INSERT INTO inbox_seen(path, size, mtime, outcome, at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, outcome=excluded.outcome, at=excluded.at",
            (str(p), st.st_size, st.st_mtime, outcome, now_iso()),
        )

    async def _scan_loop(self) -> None:
        interval = max(1.0, min(10.0, self.settle / 3 if self.settle else 2.0))
        while True:
            try:
                for p in await self.scan_once():
                    self._spawn_job(p)
            except Exception:
                log.exception("inbox scan failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), interval)
            except TimeoutError:
                pass

    async def _watch_loop(self) -> None:
        """watchfiles only wakes the scanner early; the scanner decides readiness."""
        try:
            from watchfiles import awatch
        except ImportError:  # pragma: no cover
            return
        dirs = [str(d) for d in self.inboxes if d.is_dir()]
        if not dirs:
            return
        try:
            async for _ in awatch(*dirs, recursive=True):
                self._wake.set()
        except Exception as e:  # pragma: no cover - e.g. inotify limits
            log.warning("file watcher unavailable; polling only", extra={"error": str(e)})

    def _spawn_job(self, p: Path) -> None:
        key = str(p)
        if key in self._busy:
            return
        self._busy.add(key)
        t = asyncio.get_running_loop().create_task(self.process_file(p))
        self._jobs.add(t)

        def done(task: asyncio.Task) -> None:
            self._jobs.discard(task)
            self._busy.discard(key)

        t.add_done_callback(done)

    # ------------------------------------------------------------------ processing
    async def process_file(self, path: Path) -> dict[str, Any]:
        """Match, file and tag one file. Returns a small result dict (also used by tests)."""
        with correlation(new_correlation_id("replay")):
            try:
                st = path.stat()
            except OSError as e:
                return {"outcome": "error", "error": str(e)}
            log.info("processing file", extra={"file": path.name})
            sha = await sha256_async(path)
            m = await self.matcher.match(path, sha256=sha)
            if isinstance(m, NoMatch):
                dest = await self._to_review(path, m.reason, m.candidates, sha)
                return {"outcome": "needs_review", "reason": m.reason, "candidates": m.candidates, "path": str(dest)}
            try:
                res = await self.filer.file(path, m, sha256=sha)
            except Exception as e:
                log.exception("filing failed")
                return {"outcome": "error", "error": str(e)}
            if res.outcome in ("conflict", "duplicate"):
                # REP-3: for "duplicate", res.target is the existing library path (the candidate).
                dest = await self._to_review(path, res.outcome, [str(res.target)], sha)
                log.warning(
                    "not filed",
                    extra={"file": path.name, "reason": res.outcome, "episode": m.episode_no, "session": m.session.id if m.session else f"{m.event.id}-SUPP", "existing": str(res.target)},
                )
                return {"outcome": "needs_review", "reason": res.outcome, "path": str(dest), "episode_no": m.episode_no}
            if path.exists():
                self._mark_done(path, st, res.outcome)  # hardlink/copy: don't re-hash next scan
            log.info(
                "filed" if res.outcome == "filed" else "already filed",
                extra={"file": path.name, "target": str(res.target), "step": m.step, "episode": res.episode_no},
            )
            if res.outcome == "filed":
                self.app.metrics.replay_filed.inc()
                if self.plex:
                    await self.apply_plex(res.target, m, res.episode_no)
            return {"outcome": res.outcome, "target": str(res.target), "step": m.step, "episode_no": res.episode_no}

    async def _to_review(self, path: Path, reason: str, candidates: list[str], sha: str | None) -> Path:
        self.review_dir.mkdir(parents=True, exist_ok=True)
        dest = self.review_dir / path.name
        n = 1
        while dest.exists():
            dest = self.review_dir / f"{path.stem} ({n}){path.suffix}"
            n += 1
        await asyncio.to_thread(shutil.move, str(path), str(dest))
        self.app.state.add_review(str(dest), reason, candidates, sha)
        self.app.metrics.replay_review.inc()
        log.warning("needs review", extra={"file": path.name, "reason": reason, "candidates": candidates})
        return dest

    async def assign(self, review_path: str, session_id: str, title: str | None = None) -> dict[str, Any]:
        """UI action: assign a needs-review file to a session; saved as a rule on the file hash."""
        row = self.app.state.one("SELECT * FROM review_items WHERE path=?", (review_path,))
        p = Path(review_path)
        if row is None or not p.exists():
            return {"outcome": "error", "error": "unknown review item"}
        sha = row["sha256"] or await sha256_async(p)
        self.app.state.save_assignment(sha, session_id, title)
        self.app.state.execute("DELETE FROM review_items WHERE path=?", (review_path,))
        result = await self.process_file(p)
        if result["outcome"] in ("filed", "already_filed"):
            self.app.state.execute("UPDATE review_items SET resolved_session_id=? WHERE path=?", (session_id, review_path))
        return result

    # ------------------------------------------------------------------ Plex (REP-5)
    async def apply_plex(self, target: Path, m: Match, episode_no: int | None) -> None:
        assert self.plex is not None
        try:
            await self.plex.scan_folder(str(target.parent))  # targeted scan of that folder only
            if episode_no is None:
                return  # extras: no episode metadata to write
            item = await self.plex.wait_for_episode(
                m.event.season, episode_no, target.name, self.series_title, timeout=self.plex_poll_timeout
            )
            if item is None:
                log.warning("item did not appear in Plex", extra={"file": target.name})
                return
            title = episode_title(m.event, m.title)
            summary = safe_summary(m.event, m.session)
            spoiler_state = "hidden"
            if not self.app.cfg.spoiler.enabled:
                results = await self._results(m.event, m.session_type)
                if results:
                    summary = revealed_summary(summary, results)
                    spoiler_state = "revealed"
            when = (m.session.starts_at if m.session else (m.event.sessions[-1].starts_at if m.event.sessions else None))
            date = when.date().isoformat() if when else ""
            await self.plex.set_episode_metadata(item.rating_key, title, summary, date)
            # Optional thumbnail: a spoiler-safe title card served by Pitlane, verified and
            # retried in the background so other files are not held up (REP-5 step 4).
            card = f"{self.app.base_url()}/static/cards/{m.event.id}/{episode_no}.png"
            await self.plex.set_poster(item.rating_key, card)
            self._spawn_poster_check(item.rating_key, card, target.name)
            self.app.state.execute(
                "UPDATE library_items SET plex_rating_key=?, metadata_applied_at=?, spoiler_state=? WHERE path=?",
                (item.rating_key, now_iso(), spoiler_state, str(target)),
            )
            await self._ensure_show_metadata(item.rating_key, m.event.season)
        except Exception as e:
            log.error("plex metadata failed", extra={"file": target.name, "error": str(e)})

    def _spawn_poster_check(self, rating_key: str, card_url: str, name: str) -> None:
        t = asyncio.get_running_loop().create_task(self.ensure_poster(rating_key, card_url, name))
        self.poster_tasks.add(t)
        t.add_done_callback(self.poster_tasks.discard)

    async def ensure_poster(self, rating_key: str, card_url: str, name: str = "") -> bool:
        """REP-5 step 4: PMS may discard an upload made while it still processes a new
        item. Verify that an ``upload://`` poster is selected; otherwise re-upload with
        backoff 30/60/120/240 s, then give up with a warning. Logs success too."""
        assert self.plex is not None
        with correlation(new_correlation_id("poster")):
            attempt = 0
            while True:
                try:
                    if await self.plex.poster_selected_upload(rating_key):
                        log.info("poster verified", extra={"file": name, "rating_key": rating_key, "uploads": attempt + 1})
                        return True
                except Exception as e:
                    log.warning("poster check failed", extra={"file": name, "error": str(e)})
                if attempt >= len(self.poster_retry_delays):
                    log.warning(
                        "poster not kept by Plex; giving up",
                        extra={"file": name, "rating_key": rating_key, "uploads": attempt + 1},
                    )
                    return False
                await asyncio.sleep(self.poster_retry_delays[attempt])
                attempt += 1
                try:
                    await self.plex.set_poster(rating_key, card_url)
                except Exception as e:
                    log.warning("poster re-upload failed", extra={"file": name, "error": str(e)})

    async def _ensure_show_metadata(self, rating_key: str, season: int) -> None:
        """Show and season, once: show title, season title "YYYY Season", season poster."""
        key = f"plex_season_done:{season}"
        if self.app.state.get(key):
            return
        assert self.plex is not None
        show_key, season_key = await self.plex.item_parents(rating_key)
        if show_key and not self.app.state.get("plex_show_done"):
            await self.plex.set_title(show_key, 2, self.series_title)
            self.app.state.set("plex_show_done", "1")
        if season_key:
            await self.plex.set_title(season_key, 3, f"{season} Season")
            poster = self.app.cfg.plex.season_poster
            if poster:
                await self.plex.set_poster(season_key, poster)
        self.app.state.set(key, "1")

    async def _results(self, event: Any, session_type: SessionType) -> str | None:
        if session_type not in RESULT_KIND:
            return None
        kind, _ = RESULT_KIND[session_type]
        try:
            doc = await self.app.jolpica.results(event.season, event.round, kind)
        except Exception as e:
            log.warning("results fetch failed", extra={"error": str(e)})
            return None
        return results_text(session_type, doc)

    # ------------------------------------------------------------------ spoiler sync (REP-6)
    async def spoiler_sync_once(self) -> int:
        """Reveal results for items the spoiler user has watched. Returns number revealed."""
        if not self.plex or not self.app.cfg.spoiler.enabled or not self.app.cfg.spoiler.reveal_after_watch:
            return 0
        token = self.app.cfg.plex.spoiler_token or None
        revealed = 0
        rows = self.app.state.query(
            "SELECT * FROM library_items WHERE spoiler_state='hidden' AND plex_rating_key IS NOT NULL"
        )
        for row in rows:
            st = SessionType(row["session_type"])
            if st not in RESULT_KIND:
                continue
            try:
                watched = await self.plex.view_count(row["plex_rating_key"], token=token) > 0
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    await self._relink_missing(row)
                else:
                    log.warning("viewCount read failed", extra={"error": str(e)})
                continue
            except Exception as e:
                log.warning("viewCount read failed", extra={"error": f"{type(e).__name__}: {e}"})
                continue
            if not watched:
                continue
            session = self.app.calendar.session(row["session_id"])
            event = self.app.calendar.event(session.event_id) if session else None
            if event is None:
                continue
            results = await self._results(event, st)
            if not results:
                continue
            await self.plex.set_summary(row["plex_rating_key"], revealed_summary(safe_summary(event, session), results))
            self.app.state.execute("UPDATE library_items SET spoiler_state='revealed' WHERE path=?", (row["path"],))
            revealed += 1
        return revealed

    async def _relink_missing(self, row: Any) -> None:
        """The Plex item behind a library row is gone (404). If the file is gone too, the
        row is dropped; if Plex re-added the file under a new key, the row follows it;
        otherwise the key is cleared so the item is not polled again."""
        path = Path(row["path"])
        if not await asyncio.to_thread(path.exists):
            self.app.state.execute("DELETE FROM library_items WHERE path=?", (row["path"],))
            log.info("library file removed; no longer tracked", extra={"file": path.name})
            return
        item = None
        if row["episode_no"] is not None:
            try:
                item = await self.plex.find_episode(row["season"], row["episode_no"], path.name, self.series_title)
            except Exception as e:
                log.warning("plex lookup failed", extra={"file": path.name, "error": str(e)})
                return  # try again next round
        new_key = item.rating_key if item else None
        self.app.state.execute("UPDATE library_items SET plex_rating_key=? WHERE path=?", (new_key, row["path"]))
        if new_key:
            log.info("plex item re-linked", extra={"file": path.name, "old_key": row["plex_rating_key"], "new_key": new_key})
        else:
            log.warning("plex item missing; spoiler reveal paused for this file", extra={"file": path.name})

    async def _spoiler_loop(self) -> None:
        while True:
            await asyncio.sleep(SPOILER_SYNC.total_seconds())
            try:
                await self.spoiler_sync_once()
            except Exception:
                log.exception("spoiler sync failed")

    # ------------------------------------------------------------------ downloads (REP-4)
    def _in_window(self, now: datetime) -> bool:
        w = self.rcfg.downloads.window
        if not w:
            return True
        a, b = w.split("-")
        t = now.astimezone().strftime("%H:%M")
        return a <= t < b if a <= b else (t >= a or t < b)

    def _poll_events(self, now: datetime) -> list[Any]:
        """Events from first session end until ``until_after_race`` after the race."""
        out = []
        for ev in self.app.calendar.events:
            if not ev.sessions:
                continue
            first_end = ev.sessions[0].starts_at + timedelta(minutes=ev.sessions[0].nominal_minutes)
            race = ev.session(SessionType.RACE) or ev.sessions[-1]
            if first_end <= now <= race.starts_at + self.rcfg.poll.until_after_race:
                out.append(ev)
        return out

    async def poll_providers_once(self, now: datetime | None = None) -> int:
        now = now or self.app.clock()
        queued = 0
        for provider in self.app.registry.replay():
            for ev in self._poll_events(now):
                try:
                    items = await provider.find_replays(ev)
                except Exception as e:
                    log.warning("find_replays failed", extra={"provider": provider.name, "error": str(e)})
                    continue
                for it in items:
                    st = it.session_type.value if it.session_type else ""
                    key = f"{ev.id}|{st}|{provider.name}|{it.provider_item_id}"
                    if self.app.state.one("SELECT 1 AS x FROM replay_jobs WHERE key=?", (key,)):
                        continue  # de-duplicated
                    if it.session_type and it.session_type != SessionType.SUPP and not self.rcfg.downloads.prefer_provider_over_dvr:
                        if self.app.state.one("SELECT 1 AS x FROM library_items WHERE session_id=?", (f"{ev.id}-{it.session_type.value}",)):
                            continue
                    self.app.state.execute(
                        "INSERT INTO replay_jobs(key, event_id, session_type, provider, provider_item_id, title, locator, kind, status, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (key, ev.id, st, provider.name, it.provider_item_id, it.title, it.locator, it.kind, "queued", now_iso()),
                    )
                    queued += 1
        return queued

    async def run_downloads_once(self) -> None:
        if not self._in_window(self.app.clock()):
            return
        jobs = self.app.state.query("SELECT * FROM replay_jobs WHERE status='queued' ORDER BY updated_at")
        await asyncio.gather(*(self._download(j) for j in jobs))

    async def _download(self, job: dict[str, Any]) -> None:
        from ..providers.base import check_opened
        from ..relay import ffmpeg_binary

        assert self._download_sem is not None
        async with self._download_sem:
            with correlation(new_correlation_id("replay")):
                inbox = self.inboxes[0]
                dl_dir = inbox / ".dl"
                dl_dir.mkdir(parents=True, exist_ok=True)
                safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in job["key"])[:150]
                part = dl_dir / f"{safe_key}.ts.part"
                provider = self.app.registry.get(job["provider"])
                for attempt in range(job["attempts"], 4):
                    self.app.state.execute(
                        "UPDATE replay_jobs SET status='running', attempts=?, updated_at=? WHERE key=?",
                        (attempt + 1, now_iso(), job["key"]),
                    )
                    err = None
                    try:
                        if provider is None:
                            raise RuntimeError("provider not running")
                        opened = check_opened(await provider.open_replay(job["locator"]))
                        args = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
                        if opened.headers:
                            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in opened.headers.items())]
                        args += opened.ffmpeg_input_args + ["-i", opened.url_or_path, "-map", "0", "-c", "copy", "-f", "mpegts", str(part)]
                        proc = await asyncio.create_subprocess_exec(
                            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE, start_new_session=True,
                        )  # fmt: skip
                        try:
                            _, stderr = await proc.communicate()
                        except asyncio.CancelledError:
                            proc.kill()
                            await proc.wait()
                            raise
                        if proc.returncode != 0:
                            raise RuntimeError(stderr.decode(errors="replace").strip()[-300:])
                        final = self._download_name(job)
                        os.replace(part, inbox / final)
                        self.app.state.execute(
                            "UPDATE replay_jobs SET status='done', last_error=NULL, updated_at=? WHERE key=?", (now_iso(), job["key"])
                        )
                        log.info("download finished", extra={"key": job["key"], "file": final})
                        self._wake.set()
                        return
                    except asyncio.CancelledError:
                        part.unlink(missing_ok=True)
                        self.app.state.execute("UPDATE replay_jobs SET status='queued' WHERE key=?", (job["key"],))
                        raise
                    except Exception as e:
                        err = str(e)
                        part.unlink(missing_ok=True)
                        log.warning("download failed", extra={"key": job["key"], "attempt": attempt + 1, "error": err})
                    self.app.state.execute("UPDATE replay_jobs SET last_error=? WHERE key=?", (err, job["key"]))
                    if attempt < 3:
                        await asyncio.sleep(min(300, 5 * 2**attempt))
                self.app.state.execute("UPDATE replay_jobs SET status='failed', updated_at=? WHERE key=?", (now_iso(), job["key"]))

    def _download_name(self, job: dict[str, Any]) -> str:
        from .naming import sanitize

        ev = self.app.calendar.event(job["event_id"])
        st = job["session_type"]
        if ev and st and st != "SUPP":
            s = ev.session(SessionType(st))
            if s:
                return sanitize(f"{self.series_title} - S{s.season}E{s.episode_no:04d} - {ev.name} - {s.title}") + ".ts"
        season = ev.season if ev else ""
        return sanitize(f"{self.series_title} {season} {ev.name if ev else ''} {job['title']}") + ".ts"

    def retry_download(self, key: str) -> None:
        self.app.state.execute("UPDATE replay_jobs SET status='queued', attempts=0, last_error=NULL WHERE key=?", (key,))

    async def _download_loop(self) -> None:
        while True:
            try:
                if self.app.registry.replay():
                    await self.poll_providers_once()
                    await self.run_downloads_once()
            except Exception:
                log.exception("replay polling failed")
            await asyncio.sleep(self.rcfg.poll.every.total_seconds())

    # ------------------------------------------------------------------ retention (REP-8)
    async def retention_once(self, current_season: int | None = None) -> list[dict[str, Any]]:
        """Items older than N seasons per session type. Dry-run logs them only."""
        rc = self.app.cfg.retention
        if not rc.enabled:
            return []
        current = current_season or self.app.clock().year
        doomed = []
        for row in self.app.state.query("SELECT * FROM library_items"):
            keep = rc.keep.get(SessionType(row["session_type"]), "all")
            if keep == "all":
                continue
            if row["season"] <= current - int(keep):
                doomed.append(row)
        for row in doomed:
            if rc.dry_run:
                log.info("retention (dry-run) would delete", extra={"path": row["path"]})
                continue
            if not self.plex or not row["plex_rating_key"]:
                log.warning("retention skipped: item unknown to Plex", extra={"path": row["path"]})
                continue
            try:
                await self.plex.delete_item(row["plex_rating_key"])
                self.app.state.execute("DELETE FROM library_items WHERE path=?", (row["path"],))
                log.info("retention deleted", extra={"path": row["path"]})
            except Exception as e:
                log.error("retention delete failed", extra={"path": row["path"], "error": str(e)})
        return doomed


def utcnow() -> datetime:
    return datetime.now(UTC)
