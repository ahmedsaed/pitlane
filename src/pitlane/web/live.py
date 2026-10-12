"""One JSON snapshot of everything the UI shows live (``/api/state``, ``/api/events``).

Locators and secrets are never included (OPS-4).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from ..app import App

VOD_ITEMS_MAX = 300


def live_state(app: "App") -> dict[str, Any]:
    cm = app.channel_map
    names = {c.id: c.name for c in cm.channels.values()}
    out: dict[str, Any] = {
        "tuners": {"count": app.pool.count, "in_use": len(app.pool.in_use)},
        "connections": {n: _conn(app, n, lim) for n, lim in sorted(app.connections.limits.items())},
        "reload_error": app.reload_error,
        "streams": [
            {
                "id": s.id,
                "channel": s.channel.id,
                "channel_name": names.get(s.channel.id, s.channel.id),
                "source": s.current.label if s.current else None,
                "client": s.client_addr,
                "bytes": s.bytes_sent,
                "failovers": s.failovers,
            }
            for s in app.pool.in_use.values()
        ],
        "sources": {
            s.id: {"state": s.state, "latency_ms": s.latency_ms, "last_error": s.last_error} for s in cm.sources.values()
        },
        "replay_jobs": app.state.query(
            "SELECT key, event_id, session_type, title, status, last_error FROM replay_jobs ORDER BY updated_at DESC LIMIT 50"
        ),
        "vod": vod_state(app) if app.vod else None,
    }
    return out


def _conn(app: "App", provider: str, limit: int) -> dict[str, Any]:
    """Who is using a limited provider's connections, and whether the operator holds it."""
    users = [
        f"Live TV: {s.channel.name}" for s in app.pool.in_use.values() if s.current is not None and s.current.provider == provider
    ]
    if app.vod and app.vod.current and app.vod.cfg.provider == provider:
        users.append(f"Download: {app.vod.current['title']}")
    held = app.connections.held_until(provider)
    return {
        "in_use": app.connections.in_use(provider),
        "limit": limit,
        "users": users,
        "held": None if held is None else (-1 if held == float("inf") else int(held)),
    }


def vod_state(app: "App") -> dict[str, Any]:
    from ..vod.manager import fmt_size

    m = app.vod
    cfg = m.cfg
    cur = m.current
    rows = app.state.query(
        "SELECT key, kind, title, status, bytes_done, total_size, expected_size, match_state, last_error, plex_rating_key "
        "FROM vod_items WHERE status != 'deleted' ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 "
        "WHEN 'paused' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END, created_at DESC LIMIT ?",
        (VOD_ITEMS_MAX,),
    )
    queued = sum(1 for r in rows if r["status"] in ("queued", "running"))
    rules = [f"window {cfg.download_window}" if cfg.download_window else "any time"]
    rules.append(f"max {fmt_size(cfg.max_rate)}/s" if cfg.max_rate else "no speed limit")
    rules.append("pauses for live TV and F1 sessions")
    rng = app.state.get("vod.range_supported")
    return {
        "summary": m.summary(),
        "blocked": m.blocked,
        "queued": queued,
        "current": {k: cur.get(k) for k in ("key", "title", "done", "total", "rate")} if cur else None,
        "usage": m.usage(),
        "caps": {"day": cfg.max_gb_per_day, "month": cfg.max_gb_per_month},
        "free": {"movie": m.free_space("movie"), "episode": m.free_space("episode")},
        "min_free": cfg.min_free_space,
        "rules": " · ".join(rules),
        "resume": {
            "1": "Interrupted downloads resume where they stopped.",
            "0": "The panel can't resume: an interrupted download restarts.",
        }.get(rng or "", "Resume support is checked on the first interrupted download."),
        "machine_id": m._machine_id,
        "playing": m.playing(),
        "play_channel": cfg.play_channel.guide_number if cfg.play_channel else None,
        "items": [
            {
                "key": r["key"],
                "kind": r["kind"],
                "title": r["title"],
                "status": r["status"],
                "done": r["bytes_done"] or 0,
                "total": r["total_size"],
                "expected": r["expected_size"],
                "match": r["match_state"],
                "error": r["last_error"],
                "rating_key": r["plex_rating_key"],
            }
            for r in rows
        ],
    }


async def event_stream(app: "App", is_disconnected: Any, every: float = 1.0, heartbeat: float = 15.0) -> AsyncIterator[str]:
    """SSE: the snapshot whenever it changed (checked every ``every`` s), a comment as keep-alive."""
    last = None
    quiet = 0.0
    while True:
        if await is_disconnected():
            return
        body = json.dumps(live_state(app), separators=(",", ":"), default=str)
        if body != last:
            last = body
            quiet = 0.0
            yield f"data: {body}\n\n"
        elif quiet >= heartbeat:
            quiet = 0.0
            yield ": keep-alive\n\n"
        await asyncio.sleep(every)
        quiet += every
