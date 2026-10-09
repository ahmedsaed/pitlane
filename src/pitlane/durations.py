"""Duration parsing for configuration values (`docs/configuration.md`: durations take s, m, h, or d)."""

from __future__ import annotations

import re
from datetime import timedelta

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([smhd])\s*$")


def parse_duration(value: object) -> timedelta:
    """Parse ``"10s"``, ``"5m"``, ``"6h"``, ``"1d"``, ``"-90m"`` into a :class:`timedelta`.

    Bare numbers are rejected: the reference says durations carry a unit.
    """
    if isinstance(value, timedelta):
        return value
    if not isinstance(value, str):
        raise ValueError(f"expected a duration string like '10s' or '5m', got {value!r}")
    m = _RE.match(value)
    if not m:
        raise ValueError(f"invalid duration {value!r}; use a number followed by s, m, h or d")
    return timedelta(seconds=float(m.group(1)) * _UNITS[m.group(2)])


def format_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs and secs % size == 0:
            return f"{secs // size}{unit}"
    return f"{secs}s"
