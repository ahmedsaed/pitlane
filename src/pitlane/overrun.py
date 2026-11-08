"""Sessions that run late (EPG-8, docs/epg.md "Sessions that run late").

Plex records a guide slot from its start to its stop and does not move the stop of a
recording that has already started when the guide changes **[observed 2026-11-07,
PMS 1.43]**; it does pick up slots added a few minutes ahead. So while a session is
on, Pitlane watches F1 live timing. When a guide slot is about to end (``lead``) and
the session is not over, it adds a ``block``-long "(continued)" slot after it, moves
the post-session programmes behind it, and asks Plex to reload the guide. The replay
pipeline appends each "(continued)" recording to the session's episode.

Without live timing data (service down, or it covers another session) nothing is
added: the published schedule applies, as before.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from .livetiming import LiveTiming
from .logging import get_logger
from .models import Session
from .xmltv import session_window

if TYPE_CHECKING:
    from .app import App

log = get_logger("overrun")

TICK_S = 20.0
MATCH_WITHIN = timedelta(minutes=10)  # live timing's scheduled start vs the calendar's
GRACE = timedelta(minutes=5)  # keep watching this long after the guide slot ended


class OverrunTracker:
    def __init__(self, app: "App", feed_factory: Callable[[], Any] = LiveTiming) -> None:
        self.app = app
        self.feed_factory = feed_factory
        self.feed: Any = None
        self._feed_task: asyncio.Task | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ state
    def _get(self, session_id: str) -> dict[str, Any]:
        raw = self.app.state.get(f"overrun:{session_id}")
        return json.loads(raw) if raw else {"blocks": 0, "ended": None}

    def _put(self, session_id: str, st: dict[str, Any]) -> None:
        self.app.state.set(f"overrun:{session_id}", json.dumps(st))

    def blocks(self) -> dict[str, int]:
        """Session id → "(continued)" slots, for the guide."""
        out = {}
        for row in self.app.state.query("SELECT key, value FROM kv WHERE key LIKE 'overrun:%'"):
            n = json.loads(row["value"]).get("blocks", 0)
            if n:
                out[row["key"].removeprefix("overrun:")] = n
        return out

    def end_of(self, s: Session) -> datetime:
        """Where the session's guide slots end now, "(continued)" slots included."""
        return session_window(s, self.app.cfg)[1] + self._get(s.id)["blocks"] * self.app.cfg.epg.overrun.block

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="overrun")

    async def stop(self) -> None:
        for t in (self._task, self._feed_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = self._feed_task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("overrun check failed")
            await asyncio.sleep(TICK_S)

    def current(self, now: datetime) -> Session | None:
        """The latest-starting session that has started and whose slots have not ended."""
        live = [
            s
            for s in self.app.calendar.sessions()
            if not s.date_only and s.starts_at <= now <= self.end_of(s) + GRACE and not self._get(s.id)["ended"]
        ]
        return max(live, key=lambda s: s.starts_at, default=None)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or self.app.clock()
        cfg = self.app.cfg.epg.overrun
        s = self.current(now) if cfg.enabled else None
        if s is None:
            await self._stop_feed()
            return
        self._start_feed()
        live = self.feed.session if self.feed.fresh() else None
        if live is None or live.starts_at is None or abs(live.starts_at - s.starts_at) > MATCH_WITHIN:
            return  # no evidence about this session: keep the schedule
        st = self._get(s.id)
        if live.over:
            st["ended"] = now.isoformat()
            self._put(s.id, st)
            log.info("session ended", extra={"session": s.id, "status": live.status, "part": live.part, "continued_slots": st["blocks"]})
            await self._stop_feed()
            return
        end = self.end_of(s)
        if now < end - cfg.lead:
            return
        if st["blocks"] >= cfg.max_blocks:
            if not st.get("capped"):
                st["capped"] = True
                self._put(s.id, st)
                log.warning("session still running; no more continued slots", extra={"session": s.id, "status": live.status, "slots": st["blocks"]})
            return
        st["blocks"] += 1
        self._put(s.id, st)
        self.app.guide_changed()
        log.info(
            "session running late; guide extended",
            extra={"session": s.id, "status": live.status, "slot": st["blocks"], "until": (end + cfg.block).isoformat()},
        )
        await self.app.reload_plex_guide()

    # ------------------------------------------------------------------ feed
    def _start_feed(self) -> None:
        if self._feed_task is None or self._feed_task.done():
            self.feed = self.feed_factory()
            run = getattr(self.feed, "run", None)
            if run is not None:
                self._feed_task = asyncio.get_running_loop().create_task(run(), name="live-timing")

    async def _stop_feed(self) -> None:
        t, self._feed_task = self._feed_task, None
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
