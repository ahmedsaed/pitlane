"""Local copy of the panel's movie and series catalogue (VOD-1, docs/vod.md §5.1).

One refresh is four bulk requests. It is applied in one transaction, so the UI
never sees a half-written catalogue, and it reports which series changed
(``last_modified``) so followed series are checked only when they changed.
"""

from __future__ import annotations

import time
from typing import Any

from ..logging import get_logger
from ..state import State
from .names import is_cam, normalize, parse_year, year_from_date

log = get_logger("vod.catalog")

KV_AT = "vod.catalog_at"


def _int(v: Any, default: int | None = None) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def catalog_age(state: State) -> float | None:
    at = state.get(KV_AT)
    return time.time() - float(at) if at else None


def is_due(state: State, every_s: float) -> bool:
    age = catalog_age(state)
    return age is None or age >= every_s


def apply_catalog(state: State, doc: dict[str, list]) -> dict[str, Any]:
    """Replace the catalogue tables with ``doc`` (from ``XtreamProvider.vod_catalog``)."""
    vcat = {str(c.get("category_id")): str(c.get("category_name") or "").strip() for c in doc["vod_categories"] if isinstance(c, dict)}
    scat = {str(c.get("category_id")): str(c.get("category_name") or "").strip() for c in doc["series_categories"] if isinstance(c, dict)}
    before_movies = {r["id"] for r in state.query("SELECT id FROM vod_titles")}
    before_series = {r["id"]: r["last_modified"] for r in state.query("SELECT id, last_modified FROM vod_series")}

    movies = []
    for m in doc["movies"]:
        if not isinstance(m, dict) or _int(m.get("stream_id")) is None:
            continue
        name = str(m.get("name") or "").strip()
        movies.append((
            _int(m["stream_id"]), name, normalize(name), parse_year(name), vcat.get(str(m.get("category_id")), ""),
            _float(m.get("rating")), m.get("stream_icon") or None, _int(m.get("added"), 0),
            str(m.get("container_extension") or "mkv").lower(), int(is_cam(name)), int(str(m.get("is_adult")) == "1"),
        ))  # fmt: skip
    series = []
    for s in doc["series"]:
        if not isinstance(s, dict) or _int(s.get("series_id")) is None:
            continue
        name = str(s.get("name") or "").strip()
        series.append((
            _int(s["series_id"]), name, normalize(name), year_from_date(s.get("releaseDate")) or parse_year(name),
            scat.get(str(s.get("category_id")), ""), _float(s.get("rating")), s.get("cover") or None,
            (s.get("plot") or "").strip() or None, s.get("genre") or None, _int(s.get("last_modified"), 0),
        ))  # fmt: skip

    stmts: list[tuple[str, Any]] = [("DELETE FROM vod_titles", ()), ("DELETE FROM vod_series", ())]
    stmts += [("INSERT OR REPLACE INTO vod_titles VALUES(?,?,?,?,?,?,?,?,?,?,?)", r) for r in movies]
    stmts += [("INSERT OR REPLACE INTO vod_series VALUES(?,?,?,?,?,?,?,?,?,?)", r) for r in series]
    if state.fts:
        stmts.append(("DELETE FROM vod_search", ()))
        stmts += [("INSERT INTO vod_search(kind, id, name) VALUES('movie', ?, ?)", (r[0], r[1])) for r in movies]
        stmts += [("INSERT INTO vod_search(kind, id, name) VALUES('series', ?, ?)", (r[0], r[1])) for r in series]
    stmts.append(("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KV_AT, str(time.time()))))
    state.transaction(stmts)

    changed_series = [r[0] for r in series if r[0] in before_series and (r[9] or 0) > (before_series[r[0]] or 0)]
    stats = {
        "movies": len(movies),
        "series": len(series),
        "new_movies": len({r[0] for r in movies} - before_movies) if before_movies else 0,
        "changed_series": changed_series,
    }
    log.info("vod catalogue refreshed", extra={k: v for k, v in stats.items() if k != "changed_series"} | {"changed_series": len(changed_series)})
    return stats


def search(state: State, text: str, kind: str | None, category: str | None, show_adult: bool, limit: int = 60, offset: int = 0) -> list[dict[str, Any]]:
    """Titles matching ``text`` (FTS5 prefix match on every word), newest first."""
    text = (text or "").strip()
    out: list[dict[str, Any]] = []
    kinds = [kind] if kind in ("movie", "series") else ["movie", "series"]
    ids: dict[str, list[int]] | None = None
    if text:
        ids = {"movie": [], "series": []}
        if state.fts:
            words = [w for w in "".join(c if c.isalnum() else " " for c in text).split() if w]
            if words:
                q = " ".join(f'"{w}"*' for w in words)
                for r in state.query("SELECT kind, id FROM vod_search WHERE vod_search MATCH ? LIMIT 2000", (q,)):
                    ids[r["kind"]].append(int(r["id"]))
        else:
            like = f"%{text}%"
            ids["movie"] = [r["id"] for r in state.query("SELECT id FROM vod_titles WHERE name LIKE ? LIMIT 2000", (like,))]
            ids["series"] = [r["id"] for r in state.query("SELECT id FROM vod_series WHERE name LIKE ? LIMIT 2000", (like,))]
    for k in kinds:
        table = "vod_titles" if k == "movie" else "vod_series"
        stamp = "added" if k == "movie" else "last_modified"
        where, params = [], []
        if ids is not None:
            if not ids[k]:
                continue
            where.append(f"id IN ({','.join('?' * len(ids[k]))})")
            params += ids[k]
        if category:
            where.append("category = ?")
            params.append(category)
        if k == "movie" and not show_adult:
            where.append("adult = 0")
        sql = f"SELECT *, '{k}' AS kind, {stamp} AS stamp FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {stamp} DESC LIMIT ? OFFSET ?"
        out += state.query(sql, params + [limit, offset])
    out.sort(key=lambda r: r.get("stamp") or 0, reverse=True)
    return out[:limit]


def categories(state: State) -> dict[str, list[str]]:
    return {
        "movie": [r["category"] for r in state.query("SELECT category, COUNT(*) n FROM vod_titles GROUP BY category ORDER BY category")],
        "series": [r["category"] for r in state.query("SELECT category, COUNT(*) n FROM vod_series GROUP BY category ORDER BY category")],
    }


def versions(state: State, movie_id: int, show_adult: bool) -> list[dict[str, Any]]:
    """The duplicate group of a movie (same normalized name and year), non-CAM and newest first."""
    row = state.one("SELECT norm, year FROM vod_titles WHERE id=?", (movie_id,))
    if not row or not row["norm"]:
        return []
    rows = state.query(
        "SELECT * FROM vod_titles WHERE norm=? AND (year IS ? OR year IS NULL OR ? IS NULL)" + ("" if show_adult else " AND adult=0"),
        (row["norm"], row["year"], row["year"]),
    )
    return sorted(rows, key=lambda r: (r["cam"], -(r["added"] or 0)))
