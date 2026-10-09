"""Library naming scheme (REP-3, REP-7; docs/replays.md "Library layout").

Deterministic: the same (series, session, title, container) always yields the
same relative path.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_BAD = re.compile(r'[/\\:*?"<>|]')


def sanitize(name: str) -> str:
    """Remove ``/\\:*?"<>|`` and collapse whitespace."""
    s = _BAD.sub("", name)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(".") or "untitled"


def season_dir(series_title: str, season: int) -> PurePosixPath:
    return PurePosixPath(sanitize(series_title)) / f"Season {season}"


def episode_filename(series_title: str, season: int, episode_no: int, event_name: str, title: str, ext: str) -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    return sanitize(f"{series_title} - S{season}E{episode_no:04d} - {event_name} - {title}") + ext.lower()


def episode_path(series_title: str, season: int, episode_no: int, event_name: str, title: str, ext: str) -> PurePosixPath:
    return season_dir(series_title, season) / episode_filename(series_title, season, episode_no, event_name, title, ext)


def extras_path(series_title: str, season: int, event_name: str, title: str, ext: str) -> PurePosixPath:
    """``supplementary_mode: extras`` → ``Season YYYY/Extras/<title>.ext``."""
    ext = ext if ext.startswith(".") else f".{ext}"
    return season_dir(series_title, season) / "Extras" / (sanitize(f"{event_name} - {title}") + ext.lower())
