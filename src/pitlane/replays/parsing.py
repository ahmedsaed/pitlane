"""Filename parsing for replay matching (REP-2 steps 1 and 2, docs/replays.md)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..models import Event, SessionType

EPISODE_RE = re.compile(r"(?<![A-Za-z0-9])S(\d{4})E(\d{2,4})(?![0-9])", re.IGNORECASE)
SEASON_RE = re.compile(r"\b((?:19|20)\d{2})\b")
ROUND_RES = [
    re.compile(r"\bround\s*(\d{1,2})\b"),
    re.compile(r"\br(\d{1,2})\b"),
]
SEASON_ROUND_RE = re.compile(r"\b((?:19|20)\d{2})x(\d{2})\b")
FUZZY_THRESHOLD = 85

_SUPP = ["build up", "buildup", "pre race", "pre qualifying", "post race", "analysis", "notebook", "show", "highlights"]
_FP = {
    SessionType.FP1: ["fp1", "practice 1", "practice one", "free practice 1"],
    SessionType.FP2: ["fp2", "practice 2", "practice two"],
    SessionType.FP3: ["fp3", "practice 3", "practice three"],
}
_SQ = ["sprint qualifying", "sprint shootout", "sq"]


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """Lowercase; separators → spaces; collapse whitespace (docs/replays.md step 2)."""
    s = strip_accents(name).lower()
    s = re.sub(r"[\s._\-–—+,;:()\[\]{}'\"/\\|]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _has(norm: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", norm) is not None


def detect_session(norm: str) -> SessionType | None:
    """Keyword table (docs/replays.md). Returns None when no or conflicting keywords.

    QA-006: supplementary keywords take precedence over core-session keywords,
    because phrases like "pre race" or "race highlights" contain core words and
    must never take a core session's episode slot.
    """
    if any(_has(norm, k) for k in _SUPP):
        return SessionType.SUPP
    fps = [t for t, kws in _FP.items() if any(_has(norm, k) for k in kws)]
    if len(fps) > 1:
        return None
    if fps:
        return fps[0]
    if any(_has(norm, k) for k in _SQ):
        return SessionType.SQ
    if _has(norm, "sprint"):
        return SessionType.SPRINT
    if _has(norm, "qualifying") or _has(norm, "quali"):
        return SessionType.QUALI
    if _has(norm, "race") or _has(norm, "grand prix"):
        return SessionType.RACE
    return None


@dataclass
class ParsedName:
    original: str
    norm: str
    episode: tuple[int, int] | None = None  # (season, episode_no) from SxxxxEyyyy
    season: int | None = None
    round: int | None = None
    session_type: SessionType | None = None
    notes: list[str] = field(default_factory=list)


def parse_episode(name: str) -> tuple[int, int] | None:
    found = {(int(m.group(1)), int(m.group(2))) for m in EPISODE_RE.finditer(name)}
    return found.pop() if len(found) == 1 else None


def parse_name(name: str) -> ParsedName:
    norm = normalize(name)
    p = ParsedName(original=name, norm=norm, episode=parse_episode(name))
    seasons = {int(m.group(1)) for m in SEASON_RE.finditer(norm)}
    rounds: set[int] = set()
    for m in SEASON_ROUND_RE.finditer(norm):
        seasons.add(int(m.group(1)))
        rounds.add(int(m.group(2)))
    for rx in ROUND_RES:
        rounds |= {int(m.group(1)) for m in rx.finditer(norm)}
    if len(seasons) == 1:
        p.season = seasons.pop()
    elif seasons:
        p.notes.append(f"several seasons in name: {sorted(seasons)}")
    if len(rounds) == 1:
        p.round = rounds.pop()
    elif rounds:
        p.notes.append(f"several rounds in name: {sorted(rounds)}")
    p.session_type = detect_session(norm)
    return p


def fuzzy_events(norm: str, events: list[Event]) -> list[Event]:
    """Events whose raceName, circuit name or locality reaches token-set ratio ≥ 85."""
    hits = []
    for ev in events:
        best = 0.0
        for cand in (ev.name, ev.circuit, ev.locality):
            if cand:
                best = max(best, fuzz.token_set_ratio(norm, normalize(cand)))
        if best >= FUZZY_THRESHOLD:
            hits.append(ev)
    return hits


def resolve_events(p: ParsedName, events: list[Event]) -> list[Event]:
    """Events consistent with the parsed season / round / fuzzy event name.

    No season → no candidates (Pitlane MUST NOT guess, REP-2).
    """
    if p.season is None:
        return []
    season_events = [e for e in events if e.season == p.season]
    by_round = [e for e in season_events if e.round == p.round] if p.round is not None else None
    by_name = fuzzy_events(p.norm, season_events)
    if by_round is not None and by_name:
        return [e for e in by_round if e in by_name]
    if by_round is not None:
        return by_round
    return by_name
