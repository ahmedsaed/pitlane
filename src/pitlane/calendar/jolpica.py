"""Jolpica F1 calendar: fetch, disk cache and normalization (EPG-1, docs/epg.md).

Normalization turns ``MRData.RaceTable.Races[]`` into :class:`Event` and
:class:`Session` objects with episode numbers (``round × 100 + order``).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Callable

from ..http import FetchClient, FetchPolicy
from ..logging import get_logger
from ..models import (
    DEFAULT_NOMINAL_MINUTES,
    NORMAL_ORDER,
    SESSION_TITLES,
    SPRINT_ORDER,
    Event,
    Session,
    SessionType,
    episode_number,
)

log = get_logger("calendar")

BASE_URL = "https://api.jolpi.ca/ergast/f1"

# Jolpica field → session type (docs/epg.md "Normalization").
_FIELDS: list[tuple[str, SessionType]] = [
    ("FirstPractice", SessionType.FP1),
    ("SecondPractice", SessionType.FP2),
    ("ThirdPractice", SessionType.FP3),
    ("SprintQualifying", SessionType.SQ),
    ("SprintShootout", SessionType.SQ),
    ("Sprint", SessionType.SPRINT),
    ("Qualifying", SessionType.QUALI),
]


def _parse_when(d: str | None, t: str | None) -> tuple[datetime | None, bool]:
    """Return (UTC datetime, date_only)."""
    if not d:
        return None, False
    day = date.fromisoformat(d)
    if not t:
        return datetime.combine(day, time(0, 0), UTC), True
    tt = t.strip()
    if tt.endswith("Z"):
        tt = tt[:-1]
    parsed = time.fromisoformat(tt)
    if parsed.tzinfo is not None:
        dt = datetime.combine(day, parsed).astimezone(UTC)
    else:
        dt = datetime.combine(day, parsed, UTC)
    return dt, False


def normalize_race(race: dict[str, Any], nominal: Callable[[SessionType, int], int] | None = None) -> Event:
    """Normalize one Jolpica race object into an :class:`Event` with its sessions."""
    season = int(race["season"])
    rnd = int(race["round"])
    circuit = race.get("Circuit") or {}
    loc = circuit.get("Location") or {}
    is_sprint = "Sprint" in race
    ev = Event(
        id=f"{season}-{rnd}",
        season=season,
        round=rnd,
        name=race.get("raceName", f"Round {rnd}"),
        circuit=circuit.get("circuitName", ""),
        locality=loc.get("locality", ""),
        country=loc.get("country", ""),
        is_sprint_weekend=is_sprint,
    )
    order_table = SPRINT_ORDER if is_sprint else NORMAL_ORDER
    raw: list[tuple[SessionType, dict[str, Any]]] = []
    seen: set[SessionType] = set()
    for field_name, stype in _FIELDS:
        if field_name in race and stype not in seen:
            raw.append((stype, race[field_name]))
            seen.add(stype)
    raw.append((SessionType.RACE, {"date": race.get("date"), "time": race.get("time")}))

    used_orders: set[int] = set()
    sessions: list[Session] = []
    # Sessions that belong to this weekend format claim their slots first.
    raw.sort(key=lambda item: item[0] not in order_table)
    for stype, when in raw:
        starts, date_only = _parse_when(when.get("date"), when.get("time"))
        if starts is None:
            continue
        order = order_table.get(stype)
        if order is None:
            # QA-003: a sprint weekend that also lists FP2/FP3 (older formats).
            # Use the normal-weekend slot if it is free; otherwise skip the
            # session rather than emit a duplicate episode number.
            order = NORMAL_ORDER.get(stype)
        if order is None or order in used_orders:
            log.warning(
                "session has no free episode slot; skipped",
                extra={"event": ev.id, "session": stype.value},
            )
            continue
        used_orders.add(order)
        default_min = DEFAULT_NOMINAL_MINUTES[stype]
        sessions.append(
            Session(
                id=f"{ev.id}-{stype.value}",
                event_id=ev.id,
                season=season,
                round=rnd,
                type=stype,
                title=SESSION_TITLES[stype],
                starts_at=starts,
                nominal_minutes=nominal(stype, default_min) if nominal else default_min,
                episode_no=episode_number(rnd, order),
                date_only=date_only,
            )
        )
    sessions.sort(key=lambda s: (s.starts_at, s.episode_no))
    ev.sessions = sessions
    return ev


def normalize_schedule(doc: dict[str, Any], nominal: Callable[[SessionType, int], int] | None = None) -> list[Event]:
    races = (((doc or {}).get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
    return [normalize_race(r, nominal) for r in races]


class Calendar:
    """In-memory view of all known events, loaded from Jolpica or its disk cache."""

    def __init__(self, events: list[Event] | None = None) -> None:
        self.events: list[Event] = sorted(events or [], key=lambda e: (e.season, e.round))
        self.updated_at: datetime | None = None
        self.last_error: str | None = None

    def sessions(self) -> list[Session]:
        return [s for e in self.events for s in e.sessions]

    def event(self, event_id: str) -> Event | None:
        for e in self.events:
            if e.id == event_id:
                return e
        return None

    def event_by_round(self, season: int, rnd: int) -> Event | None:
        return self.event(f"{season}-{rnd}")

    def season_events(self, season: int) -> list[Event]:
        return [e for e in self.events if e.season == season]

    def session_by_episode(self, season: int, episode_no: int) -> Session | None:
        for s in self.sessions():
            if s.season == season and s.episode_no == episode_no:
                return s
        return None

    def session(self, session_id: str) -> Session | None:
        for s in self.sessions():
            if s.id == session_id:
                return s
        return None


class JolpicaClient:
    """Fetches schedules/results with a disk cache under ``<data_dir>/jolpica/`` (EPG-1)."""

    def __init__(
        self,
        http: FetchClient,
        cache_dir: str | os.PathLike[str],
        base_url: str = BASE_URL,
        policy: FetchPolicy | None = None,
    ) -> None:
        self.http = http
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url.rstrip("/")
        # One schedule request per 6 h is far below Jolpica's limits; keep a
        # conservative client-side rate anyway.
        self.policy = policy or FetchPolicy(timeout=15, retries=2, backoff=2, rate_limit_per_sec=1)

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    async def _get_json(self, path: str, cache_key: str, use_network: bool = True) -> dict[str, Any] | None:
        cache = self._cache_path(cache_key)
        if use_network:
            try:
                resp = await self.http.get(f"{self.base_url}/{path}", policy=self.policy, params={"format": "json"})
                doc = resp.json()
                await asyncio.to_thread(_atomic_write, cache, json.dumps(doc, indent=1, sort_keys=True))
                return doc
            except Exception as e:  # network down → use cache as-is (docs/epg.md)
                log.warning("jolpica fetch failed; using cache", extra={"path": path, "error": str(e)})
        if cache.exists():
            return json.loads(await asyncio.to_thread(cache.read_text, encoding="utf-8"))
        return None

    async def schedule(self, season: int, use_network: bool = True) -> dict[str, Any] | None:
        return await self._get_json(f"{season}/races/", f"{season}-races", use_network)

    async def results(self, season: int, rnd: int, kind: str) -> dict[str, Any] | None:
        """kind: results | sprint | qualifying (spoiler-gated, REP-6)."""
        if kind not in ("results", "sprint", "qualifying"):
            raise ValueError(kind)
        return await self._get_json(f"{season}/{rnd}/{kind}/", f"{season}-{rnd}-{kind}")

    async def load_calendar(
        self,
        seasons: list[int] | str,
        now: datetime | None = None,
        nominal: Callable[[SessionType, int], int] | None = None,
        use_network: bool = True,
    ) -> Calendar:
        now = now or datetime.now(UTC)
        if seasons == "auto":
            # Current season, plus the next one once it is published.
            wanted = [now.year, now.year + 1]
        else:
            wanted = list(seasons)
        events: list[Event] = []
        errors = []
        for season in wanted:
            doc = await self.schedule(season, use_network=use_network)
            if doc is None:
                errors.append(f"no data for season {season}")
                continue
            events.extend(normalize_schedule(doc, nominal))
        cal = Calendar(events)
        cal.updated_at = now
        cal.last_error = "; ".join(errors) or None
        return cal


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

