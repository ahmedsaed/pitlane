"""Catalogue name parsing and Plex file naming (VOD-7)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from ..replays.naming import sanitize

_YEAR_END = re.compile(r"\s*[(\[]((?:19|20)\d\d)[)\]]\s*$")  # "(2010)" or "[2010]" only: "Blade Runner 2049" is a title
# Quality and source tags that panels append to names; they are not part of the title.
_TAGS = re.compile(
    r"\b(?:4k|uhd|fhd|hd|sd|1080p?|720p?|2160p?|hevc|h\.?26[45]|x26[45]|multi[- ]?sub|vip|"
    r"(?:hd-?)?cam(?:rip)?|hdts|telesync|web-?dl|webrip|bluray|brrip|dvdrip)\b",
    re.IGNORECASE,
)
_CAM = re.compile(r"\b(?:(?:hd-?)?cam(?:rip)?|hdts|telesync|telecine)\b", re.IGNORECASE)
_ARABIC = re.compile(r"[؀-ۿ]")


def _detag(name: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", name)).strip(" -–_|")


def parse_year(name: str) -> int | None:
    m = _YEAR_END.search(_detag(name))
    return int(m.group(1)) if m else None


def strip_year(name: str) -> str:
    return _YEAR_END.sub("", _detag(name)).strip() or name.strip()


def is_cam(name: str) -> bool:
    return bool(_CAM.search(name))


def normalize(name: str) -> str:
    """Grouping key for duplicates: lower case, no year, no quality tags, no punctuation."""
    s = strip_year(name).lower()
    s = re.sub(r"[^\w؀-ۿ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def year_from_date(value: str | None) -> int | None:
    m = re.match(r"^\s*((?:19|20)\d\d)", value or "")
    return int(m.group(1)) if m else None


def clean_title(name: str) -> str:
    """Display/file title: no year, no quality tags."""
    return strip_year(name)


def _tag(year: int | None, tmdb: str | None) -> str:
    out = f" ({year})" if year else ""
    if tmdb:
        out += f" {{tmdb-{tmdb}}}"
    return out


def movie_relpath(title: str, year: int | None, tmdb: str | None, ext: str) -> PurePosixPath:
    """``Title (Year) {tmdb-ID}/Title (Year) {tmdb-ID}.ext`` (Plex movie naming)."""
    base = sanitize(clean_title(title) + _tag(year, tmdb))
    return PurePosixPath(base) / f"{base}.{ext.lower().lstrip('.')}"


def show_dir(name: str, year: int | None, tmdb: str | None = None) -> str:
    return sanitize(clean_title(name) + _tag(year, tmdb))


def episode_relpath(show: str, year: int | None, season: int, episode: int, ext: str, tmdb: str | None = None) -> PurePosixPath:
    """``Show (Year)/Season 01/Show (Year) - S01E01.ext`` (Plex TV naming)."""
    folder = show_dir(show, year, tmdb)
    plain = sanitize(clean_title(show) + _tag(year, None))
    return PurePosixPath(folder) / f"Season {season:02d}" / f"{plain} - S{season:02d}E{episode:02d}.{ext.lower().lstrip('.')}"


def is_arabic(name: str) -> bool:
    return bool(_ARABIC.search(name))
