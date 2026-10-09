"""Spoiler-safe summaries and result reveal (REP-6, docs/replays.md "Spoiler mode")."""

from __future__ import annotations

from typing import Any

from ..models import Event, Session, SessionType

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip

# QA-010: which Jolpica results endpoint holds the results of a session type. FP and SQ
# have no documented endpoint (docs/epg.md), so they never get results.
RESULT_KIND: dict[SessionType, tuple[str, str]] = {
    SessionType.RACE: ("results", "Results"),
    SessionType.SPRINT: ("sprint", "SprintResults"),
    SessionType.QUALI: ("qualifying", "QualifyingResults"),
}


def safe_summary(event: Event, session: Session | None) -> str:
    """``Round 17 · Marina Bay Street Circuit, Singapore · Sunday 11 October 2026`` (never results)."""
    when = session.starts_at if session else (event.sessions[-1].starts_at if event.sessions else None)
    where = event.circuit + (f", {event.country}" if event.country else "")  # QA-009
    parts = [f"Round {event.round}", where]
    if when is not None:
        parts.append(f"{_DAYS[when.weekday()]} {when.day} {_MONTHS[when.month - 1]} {when.year}")
    return " · ".join(parts)


def episode_title(event: Event, title: str) -> str:
    return f"{event.name} – {title}"


def _driver(row: dict[str, Any]) -> str:
    d = row.get("Driver") or {}
    name = f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()
    team = (row.get("Constructor") or {}).get("name")
    return f"{name} ({team})" if team else name


def results_text(session_type: SessionType, doc: dict[str, Any] | None) -> str | None:
    """Top 3 (plus pole for QUALI/SQ) from a Jolpica results document, or None."""
    if not doc or session_type not in RESULT_KIND:
        return None
    _, key = RESULT_KIND[session_type]
    races = ((doc.get("MRData") or {}).get("RaceTable") or {}).get("Races") or []
    if not races:
        return None
    rows = sorted(races[0].get(key) or [], key=lambda r: int(r.get("position", 999)))
    if not rows:
        return None
    top = ", ".join(f"{r.get('position')}. {_driver(r)}" for r in rows[:3])
    text = f"Results: {top}."
    if session_type in (SessionType.QUALI, SessionType.SQ):
        text = f"Pole: {_driver(rows[0])}. " + text
    return text


def revealed_summary(safe: str, results: str) -> str:
    return f"{safe}\n\n{results}"
