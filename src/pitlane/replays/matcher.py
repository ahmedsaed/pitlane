"""Matcher: file → (event, session) or needs-review (REP-2, docs/replays.md "Matching").

Order: saved manual assignment (by file hash) → (a) episode number from the
filename or container tags → (b) filename parsing → (c) recorded start time.
Each step must give exactly one session; otherwise the next step runs. If no
step matches, the result is a :class:`NoMatch` listing every candidate.
Pitlane never guesses.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable

from ..calendar.jolpica import Calendar
from ..models import SESSION_TITLES, SUPP_BASE, Event, Session, SessionType
from .parsing import EPISODE_RE, ParsedName, normalize, parse_episode, parse_name, resolve_events


@dataclass
class Match:
    event: Event
    session_type: SessionType
    session: Session | None  # None for SUPP
    episode_no: int | None  # None → filer allocates the next free SUPP index
    title: str
    step: str


@dataclass
class NoMatch:
    reason: str
    candidates: list[str] = field(default_factory=list)


MediaProbe = Callable[[Path], Awaitable[dict]]


async def ffprobe_tags(path: Path) -> dict:
    """Container tags via ffprobe (empty when unavailable)."""
    exe = shutil.which("ffprobe")
    if not exe:
        return {}
    proc = await asyncio.create_subprocess_exec(
        exe, "-v", "error", "-show_entries", "format_tags", "-of", "json", "--", str(path),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {}
    try:
        tags = json.loads(out or b"{}").get("format", {}).get("tags", {}) or {}
    except json.JSONDecodeError:
        return {}
    return {str(k).lower(): str(v) for k, v in tags.items()}


def _episode_from_tags(tags: dict) -> tuple[int, int] | None:
    found: set[tuple[int, int]] = set()
    for v in tags.values():
        for m in EPISODE_RE.finditer(v):
            found.add((int(m.group(1)), int(m.group(2))))
    season = tags.get("season_number")
    ep = tags.get("episode_sort") or tags.get("episode_id")
    if season and ep and season.isdigit() and ep.isdigit() and int(season) >= 1900:
        found.add((int(season), int(ep)))
    return found.pop() if len(found) == 1 else None


def _creation_time(tags: dict) -> datetime | None:
    v = tags.get("creation_time")
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def descriptive_title(stem: str, event: Event, series_title: str) -> str:
    """Original descriptive title of a supplementary file, minus series/event/numbering noise.

    QA-011: not checked for result names (spoilers) – pending an answer.
    """
    s = re.sub(r"[._]+", " ", stem)
    s = EPISODE_RE.sub(" ", s)
    for noise in (series_title, event.name, "formula 1", "formula one", "f1"):
        if noise:
            s = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(noise)}(?![A-Za-z0-9])", " ", s)
    s = re.sub(rf"(?i)\b(round\s*\d{{1,2}}|r\d{{1,2}}|{event.season}x\d{{2}}|{event.season})\b", " ", s)
    s = re.sub(r"\b(1080p|720p|2160p|4k|hdtv|web|webrip|h264|x264|h265|x265|aac)\b", " ", s, flags=re.I)
    s = re.sub(r"^[\s\-–—]+|[\s\-–—]+$", "", s)
    s = re.sub(r"\s+[-–—][-–—\s]*\s+", " - ", s)  # only spaced dashes; keep "Build-Up"
    s = re.sub(r"\s+", " ", s).strip(" -–—")
    return s or "Supplementary"


class Matcher:
    def __init__(
        self,
        calendar_fn: Callable[[], Calendar],
        window_fn: Callable[[Session], tuple[datetime, datetime]],
        series_title: str = "Formula 1",
        probe: MediaProbe = ffprobe_tags,
        assignment_fn: Callable[[str], dict | None] | None = None,
    ) -> None:
        self.calendar_fn = calendar_fn
        self.window_fn = window_fn
        self.series_title = series_title
        self.probe = probe
        self.assignment_fn = assignment_fn

    async def match(self, path: Path, sha256: str | None = None) -> Match | NoMatch:
        cal = self.calendar_fn()
        candidates: list[str] = []

        # Saved manual assignment keyed on the file hash (docs/replays.md).
        if sha256 and self.assignment_fn:
            a = self.assignment_fn(sha256)
            if a:
                m = self._from_assignment(cal, a["session_id"], a.get("title"), path)
                if m:
                    return m

        tags = await self.probe(path)

        # (a) episode number in the filename or in container tags.
        ep = parse_episode(path.name) or _episode_from_tags(tags)
        if ep:
            m = self._from_episode(cal, ep[0], ep[1], path)
            if isinstance(m, Match):
                return m
            candidates.append(f"episode S{ep[0]}E{ep[1]:04d}: {m}")

        # (b) filename parsing.
        parsed = parse_name(path.stem)
        m2 = self._from_filename(cal, parsed, path)
        if isinstance(m2, Match):
            return m2
        candidates.extend(m2)

        # (c) recorded start time within one padded session window.
        when = _creation_time(tags)
        source = "creation_time"
        if when is None:
            try:
                when = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                source = "mtime"
            except OSError:
                when = None
        if when is not None:
            hits = []
            for s in cal.sessions():
                start, stop = self.window_fn(s)
                if start <= when < stop:
                    hits.append(s)
            if len(hits) == 1:
                s = hits[0]
                ev = cal.event(s.event_id)
                assert ev is not None
                return Match(ev, s.type, s, s.episode_no, s.title, f"time:{source}")
            candidates.extend(f"time ({source} {when.isoformat()}): {s.id}" for s in hits)

        return NoMatch("no unique match" if candidates else "no match", candidates)

    # ------------------------------------------------------------------ steps
    def _from_assignment(self, cal: Calendar, session_id: str, title: str | None, path: Path) -> Match | None:
        if session_id.endswith("-SUPP"):
            ev = cal.event(session_id.removesuffix("-SUPP"))
            if ev is None:
                return None
            return Match(ev, SessionType.SUPP, None, None, title or descriptive_title(path.stem, ev, self.series_title), "assignment")
        s = cal.session(session_id)
        if s is None:
            return None
        ev = cal.event(s.event_id)
        assert ev is not None
        return Match(ev, s.type, s, s.episode_no, s.title, "assignment")

    def _from_episode(self, cal: Calendar, season: int, episode_no: int, path: Path) -> Match | str:
        rnd, slot = divmod(episode_no, 100)
        ev = cal.event_by_round(season, rnd)
        if ev is None:
            return "no such event"
        if SUPP_BASE < slot <= 99:
            title = _title_after_episode(path.stem, ev) or descriptive_title(path.stem, ev, self.series_title)
            return Match(ev, SessionType.SUPP, None, episode_no, title, "episode")
        s = cal.session_by_episode(season, episode_no)
        if s is None:
            return "no such session"
        return Match(ev, s.type, s, s.episode_no, s.title, "episode")

    def _from_filename(self, cal: Calendar, parsed: ParsedName, path: Path) -> Match | list[str]:
        if parsed.season is None:
            return [f"filename: no season{'; ' + '; '.join(parsed.notes) if parsed.notes else ''}"]
        events = resolve_events(parsed, cal.events)
        if len(events) != 1:
            return [f"filename: event candidates {[e.id for e in events] or 'none'}"]
        ev = events[0]
        st = parsed.session_type
        if st is None:
            return [f"filename: {ev.id} but no session keyword"]
        if st == SessionType.SUPP:
            return Match(ev, st, None, None, descriptive_title(path.stem, ev, self.series_title), "filename")
        s = ev.session(st)
        if s is None:
            return [f"filename: {ev.id} has no {st.value} session"]
        return Match(ev, st, s, s.episode_no, s.title, "filename")


def _title_after_episode(stem: str, ev: Event) -> str | None:
    """``Show - S2026E1711 - Singapore Grand Prix – Pre-Race Build-Up`` → ``Pre-Race Build-Up``."""
    m = EPISODE_RE.search(stem)
    if not m:
        return None
    rest = stem[m.end() :].strip(" -–—_.")
    if normalize(rest).startswith(normalize(ev.name)):
        rest = rest[len(ev.name) :].strip(" -–—_.")
    return rest or None


def core_title(t: SessionType) -> str:
    return SESSION_TITLES.get(t, t.value)
