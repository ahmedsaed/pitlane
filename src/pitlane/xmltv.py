"""XMLTV guide builder (EPG-2 … EPG-7, docs/epg.md).

Output is deterministic: channels sorted by id, programmes by start time,
fixed attribute order, fixed indentation, so golden-file tests can compare
bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

from .calendar.jolpica import Calendar
from .config import ChannelCfg, Config
from .models import Event, Session, SessionType, supplementary_episode_number

GENERATOR = "Pitlane"
CHANNEL_SUFFIX = ".pitlane"
EN_DASH = "–"
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def xmltv_channel_id(channel_id: str) -> str:
    """Channel IDs are ``{channel_id}.pitlane`` and stay stable (LIVE-2)."""
    return f"{channel_id}{CHANNEL_SUFFIX}"


@dataclass
class Programme:
    channel: str
    start: datetime
    stop: datetime
    title: str
    sub_title: str | None = None
    desc: str | None = None
    categories: tuple[str, ...] = ()
    season: int | None = None
    episode_no: int | None = None
    date: datetime | None = None
    new: bool = False
    is_filler: bool = False
    official_start: datetime | None = None
    next_label: str | None = None  # for filler sub-titles
    original_air_date: datetime | None = None  # filler: date-based episode number
    icon: str | None = None
    # The session this programme shows (filler: the next one), for artwork and descriptions.
    event: Event | None = field(default=None, compare=False, repr=False)
    session: Session | None = field(default=None, compare=False, repr=False)

    @property
    def onscreen(self) -> str | None:
        if self.season is None or self.episode_no is None:
            return None
        return f"S{self.season}E{self.episode_no:04d}"

    @property
    def xmltv_ns(self) -> str | None:
        # Zero-based: season-1 . episode-1 . part   (S2026E1705 → "2025.1704.")
        if self.season is None or self.episode_no is None:
            return None
        return f"{self.season - 1}.{self.episode_no - 1}."


def sub_title(event: Event, title: str) -> str:
    return f"{event.name} {EN_DASH} {title}"


def description(event: Event) -> str:
    # docs/epg.md example: "Round 17 of the 2026 season at Marina Bay Street Circuit, Singapore."
    # QA-009: "{circuitName}, {country}".
    where = event.circuit
    if event.country:
        where = f"{where}, {event.country}" if where else event.country
    return f"Round {event.round} of the {event.season} season at {where}."


def session_window(s: Session, cfg: Config) -> tuple[datetime, datetime]:
    """Padded programme window (EPG-4).

    QA-001: ``stop`` is computed from the *official* start:
    ``official + nominal + post_roll``. Date-only sessions become an all-day
    placeholder (00:00–24:00 UTC) without padding.
    """
    if s.date_only:
        return s.starts_at, s.starts_at + timedelta(days=1)
    start = s.starts_at - cfg.epg.pre_roll
    stop = s.starts_at + timedelta(minutes=s.nominal_minutes) + cfg.epg.post_roll_for(s.type)
    return start, stop


def guide_window(now: datetime, cfg: Config) -> tuple[datetime, datetime]:
    """EPG-2 window. QA-002: aligned to midnight UTC; ``days_back`` whole days before
    today through the end of the ``days_ahead``-th day after today."""
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=cfg.epg.days_back), midnight + timedelta(days=cfg.epg.days_ahead + 1)


def _session_programmes(ch: ChannelCfg, cfg: Config, calendars: dict[str, Calendar]) -> list[Programme]:
    progs: list[Programme] = []
    cid = xmltv_channel_id(ch.id)
    for series_key in ch.series:
        cal = calendars.get(series_key)
        series = cfg.series.get(series_key)
        if cal is None or series is None:
            continue
        for ev in cal.events:
            for s in ev.sessions:
                start, stop = session_window(s, cfg)
                progs.append(
                    Programme(
                        channel=cid,
                        start=start,
                        stop=stop,
                        title=series.title,  # EPG-6: one series title for every session
                        sub_title=sub_title(ev, s.title),
                        desc=description(ev),
                        categories=("Sports", "Motorsport"),
                        season=s.season,
                        episode_no=s.episode_no,
                        date=s.starts_at,
                        new=True,
                        official_start=s.starts_at,
                        event=ev,
                        session=s,
                    )
                )
            # EPG-7 manual programmes, anchored to a session type of each event.
            for m in cfg.epg.manual:
                if m.channel != ch.id:
                    continue
                anchor = ev.session(m.relative_to)
                if anchor is None:
                    continue
                start = anchor.starts_at + m.offset
                progs.append(
                    Programme(
                        channel=cid,
                        start=start,
                        stop=start + m.duration,
                        title=series.title,
                        sub_title=sub_title(ev, m.title),
                        desc=description(ev),
                        categories=("Sports", "Motorsport"),
                        season=ev.season,
                        episode_no=supplementary_episode_number(ev.round, m.supplementary_index),
                        date=start,
                        # QA-004: manual supplementary items are marked new so the
                        # single series rule (EPG-6) records them too.
                        new=True,
                        official_start=start,
                        event=ev,
                    )
                )
    return progs


def _resolve_overlaps(progs: list[Programme]) -> list[Programme]:
    """Cut the earlier programme's stop to the later one's start; never overlap."""
    progs = sorted(progs, key=lambda p: (p.start, p.episode_no or 0))
    out: list[Programme] = []
    for p in progs:
        while out and out[-1].stop > p.start:
            prev = out[-1]
            prev.stop = p.start
            if prev.stop <= prev.start:
                out.pop()  # fully shadowed (same start); keep the later-numbered one
            else:
                break
        out.append(p)
    return out


def _next_label(p: Programme, tz: ZoneInfo) -> str:
    assert p.official_start is not None
    local = p.official_start.astimezone(tz)
    name = p.sub_title or p.title
    when = f"{_DAYS[local.weekday()]} {local.day:02d} {_MONTHS[local.month - 1]} {local:%H:%M} {local.tzname()}"
    return f"{name.replace(f' {EN_DASH} ', ' ')} {EN_DASH} {when}"


def _when(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    return f"{_DAYS[local.weekday()]} {local.day:02d} {_MONTHS[local.month - 1]} {local:%H:%M}"


def _filler_desc(ch: ChannelCfg, nxt: Programme | None, tz: ZoneInfo) -> str:
    if not ch.series:
        return f"Live sports on {ch.name}. The provider publishes no listings for this channel."
    if nxt is None or nxt.official_start is None:
        return f"Nothing live on {ch.name} right now."
    lines = [f"Nothing live on {ch.name} right now."]
    ev = nxt.event
    upcoming = [s for s in ev.sessions if s.starts_at >= nxt.official_start] if ev is not None else []
    if upcoming:
        lines.append(f"Coming up at the {ev.name}: " + " · ".join(f"{s.title} {_when(s.starts_at, tz)}" for s in upcoming))
    return "\n".join(lines)


def _boundaries(t: datetime, step: timedelta, tz: ZoneInfo) -> datetime:
    """Next block boundary after ``t``: local midnight plus whole multiples of ``step``,
    so filler blocks line up with the viewer's clock (00:00, 03:00, … local)."""
    local = t.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = midnight + timedelta(days=1)  # aware arithmetic is wall-clock time
    nxt = midnight
    while nxt.astimezone(UTC) <= t and nxt < tomorrow:
        nxt = nxt + step
    return min(nxt, tomorrow).astimezone(UTC)


MIN_FILLER = timedelta(minutes=30)


def _fillers(
    ch: ChannelCfg, progs: list[Programme], w_start: datetime, w_end: datetime, cfg: Config
) -> list[Programme]:
    """EPG-5: fill every gap inside the window, in blocks aligned to local time
    (``filler_max`` long, never across local midnight). A leading or trailing piece
    shorter than 30 min joins its neighbour block instead of standing alone."""
    tz = ZoneInfo(cfg.epg.timezone)
    cid = xmltv_channel_id(ch.id)
    fillers: list[Programme] = []

    gaps: list[tuple[datetime, datetime, Programme | None]] = []
    cursor = w_start
    for p in progs:
        if p.start > cursor:
            gaps.append((cursor, min(p.start, w_end), p))
        cursor = max(cursor, p.stop)
        if cursor >= w_end:
            break
    if cursor < w_end:
        following = next((p for p in progs if p.start >= w_end), None)
        gaps.append((cursor, w_end, following))

    title = ch.name if ch.series else f"{ch.name} {EN_DASH} Live"
    for g_start, g_end, nxt in gaps:
        cuts = [g_start]
        while cuts[-1] < g_end:
            cuts.append(min(g_end, _boundaries(cuts[-1], cfg.epg.filler_max, tz)))
        # Merge slivers into a neighbour unless that would cross local midnight.
        def same_day(a: datetime, b: datetime) -> bool:
            return a.astimezone(tz).date() == (b - timedelta(microseconds=1)).astimezone(tz).date()

        if len(cuts) > 2 and cuts[1] - cuts[0] < MIN_FILLER and same_day(cuts[0], cuts[2]):
            del cuts[1]
        if len(cuts) > 2 and cuts[-1] - cuts[-2] < MIN_FILLER and same_day(cuts[-3], cuts[-1]):
            del cuts[-2]
        for t, seg_end in zip(cuts, cuts[1:]):
            fillers.append(
                Programme(
                    channel=cid,
                    start=t,
                    stop=seg_end,
                    title=title,
                    # QA-002: no next session known → no sub-title.
                    sub_title=f"Next: {_next_label(nxt, tz)}" if ch.series and nxt is not None else None,
                    desc=_filler_desc(ch, nxt, tz),
                    # docs/epg.md: filler is presented as TV programming, not a movie
                    # (category Series + date-based episode number). Never <new/>, and the
                    # channel-name title keeps it out of the series rule.
                    categories=("Series",),
                    original_air_date=t,
                    is_filler=True,
                    event=nxt.event if ch.series and nxt is not None else None,
                    session=nxt.session if ch.series and nxt is not None else None,
                )
            )
    return fillers


def build_programmes(
    cfg: Config,
    calendars: dict[str, Calendar],
    now: datetime,
    base_url: str = "",
) -> dict[str, list[Programme]]:
    w_start, w_end = guide_window(now, cfg)
    base_url = base_url.rstrip("/")
    result: dict[str, list[Programme]] = {}
    for ch in sorted(cfg.channels, key=lambda c: c.id):
        all_progs = _resolve_overlaps(_session_programmes(ch, cfg, calendars))
        in_window = [p for p in all_progs if p.stop > w_start and p.start < w_end]
        # Filler looks at programmes after the window too, for the "Next:" label.
        fill = _fillers(ch, [p for p in all_progs if p.stop > w_start], w_start, w_end, cfg)
        progs = sorted(in_window + fill, key=lambda p: p.start)
        logo = _logo_url(ch, base_url)
        for p in progs:
            p.icon = _icon(p, base_url, logo)
        result[ch.id] = progs
    return result


def _icon(p: Programme, base_url: str, logo: str | None) -> str | None:
    """Programme artwork: the session's title card, a "next up" card for F1 filler,
    the channel logo for other filler. Manual programmes have no card."""
    if p.event is not None and p.session is not None:
        kind = "next/" if p.is_filler else ""
        return f"{base_url}/static/cards/{kind}{p.event.id}/{p.session.episode_no}.png"
    return logo if p.is_filler else None


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M%S +0000")


def _logo_url(ch: ChannelCfg, base_url: str) -> str | None:
    if not ch.logo:
        return None
    if ch.logo.startswith(("http://", "https://")):
        return ch.logo
    from pathlib import PurePath

    return f"{base_url}/static/logos/{ch.id}{PurePath(ch.logo).suffix}"


def render_xmltv(cfg: Config, calendars: dict[str, Calendar], now: datetime, base_url: str) -> bytes:
    base_url = base_url.rstrip("/")
    programmes = build_programmes(cfg, calendars, now, base_url)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        f"<tv generator-info-name={quoteattr(GENERATOR)}>",
    ]
    channels = sorted(cfg.channels, key=lambda c: c.id)
    for ch in channels:
        lines.append(f"  <channel id={quoteattr(xmltv_channel_id(ch.id))}>")
        lines.append(f"    <display-name>{escape(ch.name)}</display-name>")
        lines.append(f"    <display-name>{escape(ch.guide_number)}</display-name>")
        logo = _logo_url(ch, base_url)
        if logo:
            lines.append(f"    <icon src={quoteattr(logo)}/>")
        lines.append("  </channel>")
    for ch in channels:
        for p in programmes[ch.id]:
            lines.append(
                f"  <programme start={quoteattr(_ts(p.start))} stop={quoteattr(_ts(p.stop))} "
                f"channel={quoteattr(p.channel)}>"
            )
            lines.append(f'    <title lang="en">{escape(p.title)}</title>')
            if p.sub_title:
                lines.append(f'    <sub-title lang="en">{escape(p.sub_title)}</sub-title>')
            if p.desc:
                lines.append(f'    <desc lang="en">{escape(p.desc)}</desc>')
            # QA-005: element order follows the XMLTV DTD (date before category).
            if p.date:
                lines.append(f"    <date>{p.date.astimezone(UTC).strftime('%Y%m%d')}</date>")
            for c in p.categories:
                lines.append(f'    <category lang="en">{escape(c)}</category>')
            if p.icon:
                lines.append(f"    <icon src={quoteattr(p.icon)}/>")
            if p.xmltv_ns:
                lines.append(f'    <episode-num system="xmltv_ns">{p.xmltv_ns}</episode-num>')
                lines.append(f'    <episode-num system="onscreen">{p.onscreen}</episode-num>')
            if p.original_air_date:
                oad = p.original_air_date.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f'    <episode-num system="original-air-date">{oad}</episode-num>')
            if p.new and not p.is_filler:
                lines.append("    <new/>")
            lines.append("  </programme>")
    lines.append("</tv>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def etag_for(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def session_type_title(t: SessionType) -> str:
    from .models import SESSION_TITLES

    return SESSION_TITLES.get(t, t.value)
