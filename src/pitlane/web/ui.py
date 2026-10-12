"""Minimal operator web UI (OPS-1).

Shows channel/source health, current streams, guide preview, replay queue,
needs-review items with an "assign to session" action, and a reload button.
All provider-supplied strings are HTML-escaped; locators (which may carry
credentials) and secrets are never shown (OPS-4).
"""

from __future__ import annotations

import base64
import hmac
import json
from datetime import datetime, timedelta
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..models import SessionType

if TYPE_CHECKING:
    from ..app import App

CSS = """
:root{--bg:#f6f7f9;--fg:#15171c;--muted:#5d6370;--card:#fff;--line:#e2e5ea;--ok:#127a3a;--warn:#a86400;--bad:#b3261e;--accent:#c4120c}
@media (prefers-color-scheme: dark){:root{--bg:#121418;--fg:#e8eaee;--muted:#9aa1ad;--card:#1b1e24;--line:#2b2f37;--ok:#4cc27a;--warn:#e0a240;--bad:#f07068;--accent:#ff5a4f}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 16px;border-bottom:3px solid var(--accent);background:var(--card)}
header h1{font-size:18px;margin:0 auto 0 0}main{padding:16px;max-width:1200px;margin:auto}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:0 0 16px;overflow-x:auto}
h2{font-size:15px;margin:0 0 8px}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}.online{color:var(--ok)}.degraded{color:var(--warn)}.offline{color:var(--bad)}.unknown{color:var(--muted)}
.banner{padding:10px 16px;background:var(--bad);color:#fff}.note{padding:10px 16px;background:var(--ok);color:#fff}
button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
select,input{font:inherit;max-width:100%}.muted{color:var(--muted)}form.inline{display:inline}
"""


def _check_auth(app: "App", request: Request) -> Response | None:
    pw = app.cfg.server.ui_password
    if not pw:
        return None
    hdr = request.headers.get("authorization", "")
    if hdr.lower().startswith("basic "):
        try:
            _, _, given = base64.b64decode(hdr[6:]).decode().partition(":")
        except Exception:
            given = ""
        if hmac.compare_digest(given.encode(), pw.encode()):
            return None
    return Response("Authentication required\n", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Pitlane"'})


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    from urllib.parse import urlparse

    return urlparse(origin).netloc == request.headers.get("host")


def _ts(dt: datetime, tz) -> str:  # type: ignore[no-untyped-def]
    return dt.astimezone(tz).strftime("%a %d %b %H:%M")


def ui_router(app: "App") -> APIRouter:
    r = APIRouter()

    def status() -> dict[str, Any]:
        cm = app.channel_map
        st = app.state
        return {
            "channels": [
                {
                    "id": c.id,
                    "guide_number": c.guide_number,
                    "name": c.name,
                    "sources": [
                        {
                            "id": s.id,
                            "label": s.label,
                            "provider": s.provider,
                            "kind": s.kind,
                            "priority": s.priority,
                            "state": s.state,
                            "latency_ms": s.latency_ms,
                            "last_error": s.last_error,
                            "last_ok_at": s.last_ok_at,
                        }
                        for s in cm.channel_sources(c.id)
                    ],
                }
                for c in sorted(cm.channels.values(), key=lambda c: (float(c.guide_number), c.guide_number))
            ],
            "streams": [
                {
                    "id": s.id,
                    "channel": s.channel.id,
                    "source": s.current.label if s.current else None,
                    "client": s.client_addr,
                    "bytes": s.bytes_sent,
                    "failovers": s.failovers,
                }
                for s in app.pool.in_use.values()
            ],
            "tuners": {"count": app.pool.count, "in_use": len(app.pool.in_use)},
            # LIVE-10: upstream connections per limited provider (streams, probes, closing inputs)
            "connections": {
                name: {"in_use": app.connections.in_use(name), "limit": limit}
                for name, limit in sorted(app.connections.limits.items())
            },
            "reload_error": app.reload_error,
            "provider_errors": app.registry.errors if app.registry else {},
            "replay_jobs": st.query("SELECT key, event_id, session_type, provider, title, status, attempts, last_error, updated_at FROM replay_jobs ORDER BY updated_at DESC LIMIT 100"),
            "needs_review": [
                {**row, "candidates": json.loads(row["candidates"] or "[]")}
                for row in st.query("SELECT path, reason, candidates, detected_at FROM review_items WHERE resolved_session_id IS NULL ORDER BY detected_at")
            ],
            "library": st.query("SELECT path, session_id, episode_no, title, spoiler_state, filed_at FROM library_items ORDER BY filed_at DESC LIMIT 50"),
            "calendar": {
                "updated_at": app.calendar.updated_at.isoformat() if app.calendar.updated_at else None,
                "error": app.calendar.last_error,
                "date_only_sessions": [s.id for s in app.calendar.sessions() if s.date_only],
            },
        }

    def page(request: Request) -> str:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(app.cfg.epg.timezone)
        spoilers_off = request.query_params.get("spoilers") == "show" or not app.cfg.spoiler.enabled
        s = status()
        out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Pitlane</title><style>{CSS}</style></head><body>"]
        if s["reload_error"]:
            out.append("<div class=banner><b>Config reload rejected – the previous configuration is still active.</b><br>" + "<br>".join(escape(e) for e in s["reload_error"]) + "</div>")
        msg = request.query_params.get("msg")
        if msg:
            out.append(f"<div class=note>{escape(msg)}</div>")
        out.append(
            "<header><h1>Pitlane</h1>"
            f"<span class=muted>Tuners {s['tuners']['in_use']}/{s['tuners']['count']}</span>"
            + "".join(
                f"<span class=muted title='Connections to this provider'>{escape(name)} connections {c['in_use']}/{c['limit']}</span>"
                for name, c in s["connections"].items()
            )
            + (f"<a href='/vod'>Movies &amp; Shows</a><span class=muted>{escape(app.vod.summary())}</span>" if app.vod else "")
            + "<form class=inline method=post action='/ui/refresh-calendar'><button>Refresh calendar</button></form>"
            "<form class=inline method=post action='/ui/reload'><button>Reload config</button></form>"
            + (
                "<a href='/'>Hide results</a>"
                if spoilers_off and app.cfg.spoiler.enabled
                else ("<a href='/?spoilers=show'>Show results</a>" if app.cfg.spoiler.enabled else "")
            )
            + "</header><main>"
        )
        # Channels & sources
        out.append("<section><h2>Channels and sources</h2><table><tr><th>#</th><th>Channel</th><th>Source</th><th>Provider</th><th>Kind</th><th>Prio</th><th>State</th><th>Latency</th><th>Last error</th></tr>")
        for c in s["channels"]:
            srcs = c["sources"] or [None]
            for i, src in enumerate(srcs):
                head = f"<td>{escape(c['guide_number'])}</td><td>{escape(c['name'])}</td>" if i == 0 else "<td></td><td></td>"
                if src is None:
                    out.append(f"<tr>{head}<td colspan=7 class=muted>no sources</td></tr>")
                    continue
                out.append(
                    f"<tr>{head}<td>{escape(src['label'])}</td><td>{escape(src['provider'])}</td><td>{escape(src['kind'])}</td>"
                    f"<td>{src['priority']}</td><td class={escape(src['state'])}>{escape(src['state'])}</td>"
                    f"<td>{src['latency_ms'] if src['latency_ms'] is not None else ''}</td><td>{escape(src['last_error'] or '')}</td></tr>"
                )
        out.append("</table>")
        for name, err in s["provider_errors"].items():
            out.append(f"<p class=offline>Provider {escape(name)}: {escape(err)}</p>")
        out.append("</section>")
        # Streams
        out.append("<section><h2>Current streams</h2>")
        if s["streams"]:
            out.append("<table><tr><th>Channel</th><th>Source</th><th>Client</th><th>Bytes</th><th>Failovers</th><th>ID</th></tr>")
            for st in s["streams"]:
                out.append(f"<tr><td>{escape(st['channel'])}</td><td>{escape(st['source'] or '')}</td><td>{escape(st['client'])}</td><td>{st['bytes']}</td><td>{st['failovers']}</td><td class=muted>{escape(st['id'])}</td></tr>")
            out.append("</table>")
        else:
            out.append("<p class=muted>No active streams.</p>")
        out.append("</section>")
        # Guide preview
        from ..xmltv import build_programmes

        now = app.clock()
        progs = build_programmes(app.cfg, app.calendars, now)
        out.append(f"<section><h2>Guide preview (next 48 h, {escape(app.cfg.epg.timezone)})</h2><table><tr><th>Channel</th><th>Start</th><th>Stop</th><th>Title</th><th>Episode</th></tr>")
        for ch_id, plist in progs.items():
            for p in plist:
                if p.is_filler or p.stop < now or p.start > now + timedelta(hours=48):
                    continue
                out.append(f"<tr><td>{escape(ch_id)}</td><td>{_ts(p.start, tz)}</td><td>{_ts(p.stop, tz)}</td><td>{escape(p.title)} – {escape(p.sub_title or '')}</td><td>{escape(p.onscreen or '')}</td></tr>")
        out.append("</table>")
        cal = s["calendar"]
        out.append(f"<p class=muted>Calendar updated {escape(cal['updated_at'] or 'never')}" + (f" · {escape(cal['error'])}" if cal["error"] else "") + "</p>")
        if cal["date_only_sessions"]:
            out.append("<p class=degraded>Sessions without a start time (all-day placeholders): " + escape(", ".join(cal["date_only_sessions"])) + "</p>")
        out.append("</section>")
        # Needs review
        sessions = app.calendar.sessions()
        opts = "".join(
            f"<option value='{escape(x.id)}'>{escape(x.id)} – {escape((app.calendar.event(x.event_id).name if app.calendar.event(x.event_id) else ''))} {escape(x.title)}</option>"
            for x in sessions
        ) + "".join(f"<option value='{escape(e.id)}-SUPP'>{escape(e.id)} – {escape(e.name)} (supplementary)</option>" for e in app.calendar.events)
        out.append("<section><h2>Needs review</h2>")
        if s["needs_review"]:
            out.append("<table><tr><th>File</th><th>Reason</th><th>Candidates</th><th>Assign to session</th></tr>")
            for item in s["needs_review"]:
                cands = "<br>".join(escape(c) for c in item["candidates"])
                out.append(
                    f"<tr><td>{escape(item['path'])}</td><td>{escape(item['reason'])}</td><td class=muted>{cands}</td><td>"
                    f"<form method=post action='/ui/review/assign'><input type=hidden name=path value='{escape(item['path'], quote=True)}'>"
                    f"<select name=session_id>{opts}</select> <input name=title placeholder='title (supplementary)' size=18> <button>Assign</button></form></td></tr>"
                )
            out.append("</table>")
        else:
            out.append("<p class=muted>Nothing to review.</p>")
        out.append("</section>")
        # Replay queue + library
        out.append("<section><h2>Replay queue</h2>")
        if s["replay_jobs"]:
            out.append("<table><tr><th>Event</th><th>Session</th><th>Provider</th><th>Title</th><th>Status</th><th>Attempts</th><th>Error</th><th></th></tr>")
            for j in s["replay_jobs"]:
                retry = (
                    f"<form method=post action='/ui/downloads/retry'><input type=hidden name=key value='{escape(j['key'], quote=True)}'><button>Retry</button></form>"
                    if j["status"] == "failed"
                    else ""
                )
                out.append(f"<tr><td>{escape(j['event_id'] or '')}</td><td>{escape(j['session_type'] or '')}</td><td>{escape(j['provider'])}</td><td>{escape(j['title'] or '')}</td><td>{escape(j['status'])}</td><td>{j['attempts']}</td><td>{escape(j['last_error'] or '')}</td><td>{retry}</td></tr>")
            out.append("</table>")
        else:
            out.append("<p class=muted>No replay downloads.</p>")
        out.append("<h2>Recently filed</h2><table><tr><th>Episode</th><th>Title</th><th>Filed</th><th>Results</th></tr>")
        for li in s["library"]:
            res = ""
            if spoilers_off:
                res = escape(app.ui_results.get(li["session_id"], "")) if hasattr(app, "ui_results") else ""
            out.append(f"<tr><td>{escape(str(li['episode_no'] or 'extra'))}</td><td>{escape(li['title'] or '')}</td><td>{escape(li['filed_at'] or '')}</td><td>{res or ('<span class=muted>hidden</span>' if not spoilers_off else '')}</td></tr>")
        out.append("</table></section></main></body></html>")
        return "".join(out)

    @r.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if err := _check_auth(app, request):
            return err
        if request.query_params.get("spoilers") == "show" and app.replays:
            await _load_results(app)
        return HTMLResponse(page(request))

    @r.get("/api/status")
    async def api_status(request: Request):
        if err := _check_auth(app, request):
            return err
        return JSONResponse(status())

    async def _form(request: Request) -> dict[str, str]:
        from urllib.parse import parse_qs

        body = (await request.body()).decode()
        return {k: v[0] for k, v in parse_qs(body).items()}

    @r.post("/ui/reload")
    async def reload(request: Request):
        if err := _check_auth(app, request):
            return err
        if not _same_origin(request):
            return Response(status_code=403)
        errors = await app.reload()
        return RedirectResponse("/?msg=" + quote("Config reloaded" if not errors else "Reload rejected"), status_code=303)

    @r.post("/ui/refresh-calendar")
    async def refresh_calendar(request: Request):
        if err := _check_auth(app, request):
            return err
        if not _same_origin(request):
            return Response(status_code=403)
        await app.refresh_calendar()
        return RedirectResponse("/?msg=" + quote("Calendar refreshed"), status_code=303)

    @r.post("/ui/review/assign")
    async def assign(request: Request):
        if err := _check_auth(app, request):
            return err
        if not _same_origin(request) or not app.replays:
            return Response(status_code=403)
        f = await _form(request)
        res = await app.replays.assign(f.get("path", ""), f.get("session_id", ""), f.get("title") or None)
        return RedirectResponse("/?msg=" + quote(f"Assignment: {res.get('outcome')}"), status_code=303)

    @r.post("/ui/downloads/retry")
    async def retry(request: Request):
        if err := _check_auth(app, request):
            return err
        if not _same_origin(request) or not app.replays:
            return Response(status_code=403)
        f = await _form(request)
        app.replays.retry_download(f.get("key", ""))
        return RedirectResponse("/?msg=" + quote("Download re-queued"), status_code=303)

    @r.get("/static/cards/next/{event_id}/{episode}.png")
    async def next_card(event_id: str, episode: int):
        """Guide artwork for F1 filler (docs/epg.md): the session that is on next."""
        import asyncio
        from zoneinfo import ZoneInfo

        from ..replays.titlecard import render_next_card

        ev = app.calendar.event(event_id)
        s = app.calendar.session_by_episode(ev.season, episode) if ev else None
        if ev is None or s is None:
            return Response(status_code=404)
        png = await asyncio.to_thread(render_next_card, ev, s, ZoneInfo(app.cfg.epg.timezone))
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    @r.get("/static/cards/{event_id}/{episode}.png")
    async def card(event_id: str, episode: int):
        from ..replays.titlecard import render_card

        ev = app.calendar.event(event_id)
        if ev is None:
            return Response(status_code=404)
        s = app.calendar.session_by_episode(ev.season, episode)
        title = s.title if s else None
        if title is None:
            row = app.state.one("SELECT title FROM library_items WHERE season=? AND episode_no=?", (ev.season, episode))
            title = row["title"] if row else None
        if title is None:
            return Response(status_code=404)
        import asyncio

        png = await asyncio.to_thread(render_card, ev, s, title)
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    return r


async def _load_results(app: "App") -> None:
    """Results for the UI, only when the viewer toggled spoilers off for the page."""
    from ..replays.spoilers import RESULT_KIND, results_text

    cache: dict[str, str] = {}
    for row in app.state.query("SELECT DISTINCT session_id, session_type FROM library_items"):
        try:
            st = SessionType(row["session_type"])
        except ValueError:
            continue
        if st not in RESULT_KIND:
            continue
        s = app.calendar.session(row["session_id"])
        if not s:
            continue
        try:
            doc = await app.jolpica.results(s.season, s.round, RESULT_KIND[st][0])
        except Exception:
            continue
        txt = results_text(st, doc)
        if txt:
            cache[row["session_id"]] = txt
    app.ui_results = cache  # type: ignore[attr-defined]
