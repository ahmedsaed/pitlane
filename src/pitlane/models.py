"""Domain model (docs/architecture.md, "Data model")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SessionType(StrEnum):
    FP1 = "FP1"
    FP2 = "FP2"
    FP3 = "FP3"
    SQ = "SQ"
    SPRINT = "SPRINT"
    QUALI = "QUALI"
    RACE = "RACE"
    SUPP = "SUPP"


SESSION_TITLES: dict[SessionType, str] = {
    SessionType.FP1: "Practice 1",
    SessionType.FP2: "Practice 2",
    SessionType.FP3: "Practice 3",
    SessionType.SQ: "Sprint Qualifying",
    SessionType.SPRINT: "Sprint",
    SessionType.QUALI: "Qualifying",
    SessionType.RACE: "Race",
}

DEFAULT_NOMINAL_MINUTES: dict[SessionType, int] = {
    SessionType.FP1: 60,
    SessionType.FP2: 60,
    SessionType.FP3: 60,
    SessionType.SQ: 45,
    SessionType.SPRINT: 60,
    SessionType.QUALI: 60,
    SessionType.RACE: 120,
}

# docs/epg.md "Normalization": order within the weekend.
NORMAL_ORDER: dict[SessionType, int] = {
    SessionType.FP1: 1,
    SessionType.FP2: 2,
    SessionType.FP3: 3,
    SessionType.QUALI: 4,
    SessionType.RACE: 5,
}
SPRINT_ORDER: dict[SessionType, int] = {
    SessionType.FP1: 1,
    SessionType.SQ: 2,
    SessionType.SPRINT: 3,
    SessionType.QUALI: 4,
    SessionType.RACE: 5,
}

SUPP_BASE = 10  # supplementary episodes: round*100 + 10 + n, n >= 1 (docs/epg.md)


def episode_number(round_: int, order: int) -> int:
    """``episode_no = round × 100 + order`` (docs/epg.md, "Episode numbering")."""
    return round_ * 100 + order


def supplementary_episode_number(round_: int, index: int) -> int:
    """Supplementary items use ``round × 100 + 10 + n`` with n starting at 1 (REP-7)."""
    if not 1 <= index <= 89:
        raise ValueError("supplementary index must be within 1..89")
    return round_ * 100 + SUPP_BASE + index


# EPG-8: "(continued)" slots of a session that runs late use supplementary indexes 60..89:
# six per session, for the session in slot 1..5 of its round (episode round x 100 + slot).
CONTINUATION_FIRST = 60
CONTINUATION_SLOTS = 6


def continuation_episode_number(session_episode: int, k: int) -> int:
    """Episode number of the ``k``-th (1-based) "(continued)" slot of a session."""
    rnd, slot = divmod(session_episode, 100)
    if not 1 <= slot <= 5 or not 1 <= k <= CONTINUATION_SLOTS:
        raise ValueError("continuation outside the reserved range")
    return supplementary_episode_number(rnd, CONTINUATION_FIRST - 1 + (slot - 1) * CONTINUATION_SLOTS + k)


def continuation_of(episode_no: int) -> tuple[int, int] | None:
    """``(session episode number, k)`` for a "(continued)" episode number, else None."""
    rnd, slot = divmod(episode_no, 100)
    idx = slot - SUPP_BASE - CONTINUATION_FIRST
    if not 0 <= idx < 5 * CONTINUATION_SLOTS:
        return None
    return rnd * 100 + idx // CONTINUATION_SLOTS + 1, idx % CONTINUATION_SLOTS + 1


@dataclass
class Event:
    id: str  # "2026-16"
    season: int
    round: int
    name: str
    circuit: str
    locality: str
    country: str
    is_sprint_weekend: bool
    sessions: list["Session"] = field(default_factory=list)

    def session(self, type_: SessionType) -> "Session | None":
        for s in self.sessions:
            if s.type == type_:
                return s
        return None


@dataclass
class Session:
    id: str  # "2026-16-RACE"
    event_id: str
    season: int
    round: int
    type: SessionType
    title: str
    starts_at: datetime  # UTC
    nominal_minutes: int
    episode_no: int
    date_only: bool = False  # Jolpica gave no time (docs/epg.md)

    @property
    def onscreen(self) -> str:
        return f"S{self.season}E{self.episode_no:04d}"
