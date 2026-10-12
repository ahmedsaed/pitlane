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

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from ..models import SessionType
from .live import event_stream, live_state
from .shell import done
from .shell import page as shell_page

if TYPE_CHECKING:
    from ..app import App

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

    def page(request: Request) -> HTMLResponse:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(app.cfg.epg.timezone)
        spoilers_off = request.query_params.get("spoilers") == "show" or not app.cfg.spoiler.enabled
        s = status()
        out = []
        # Now playing (live)
        out.append("<section class=card><h2>Now playing</h2><div data-r=streams><div class=empty>Loading…</div></div></section>")
        # Channels: one row each, sources (failover order) behind a disclosure; health is live
        out.append(f"<section class=card><h2>Channels <span class=sub>{len(s['channels'])} channels · click one for its sources in failover order</span></h2><div class=chans>")
        for c in s["channels"]:
            srcs = c["sources"]
            first = srcs[0]["label"] if srcs else "no sources"
            more = f" <span class='muted small'>+{len(srcs) - 1} backup{'s' if len(srcs) > 2 else ''}</span>" if len(srcs) > 1 else ""
            dots = "".join(f"<span class='dot {escape(x['state'])}' data-src-dot='{escape(x['id'], quote=True)}' title='{escape(x['label'], quote=True)}'></span>" for x in srcs)
            out.append(
                f"<details class=chan><summary><span class=muted>{escape(c['guide_number'])}</span><b>{escape(c['name'])} <span class=badge data-ch-live='{escape(c['id'], quote=True)}' hidden><span class='dot running'></span>playing</span></b>"
                f"<span class=src>{escape(first)}{more}</span><span class=dots>{dots}</span></summary>"
            )
            if srcs:
                out.append("<div class=scroll><table><tr><th>Source</th><th>Provider</th><th>Priority</th><th>State</th><th>Last error</th></tr>")
                for src in srcs:
                    sid = escape(src["id"], quote=True)
                    out.append(
                        f"<tr><td>{escape(src['label'])}</td><td class=muted>{escape(src['provider'])}</td><td class=muted>{src['priority']}</td>"
                        f"<td data-src='{sid}'></td><td class='small err' data-src-err='{sid}'>{escape(src['last_error'] or '')}</td></tr>"
                    )
                out.append("</table></div>")
            out.append("</details>")
        out.append("</div>")
        for name, err in s["provider_errors"].items():
            out.append(f"<p class=offline>Provider {escape(name)}: {escape(err)}</p>")
        out.append("</section>")
        # Guide preview (static)
        from ..xmltv import build_programmes

        now = app.clock()
        progs = build_programmes(app.cfg, app.calendars, now)
        rows = []
        for ch_id, plist in progs.items():
            for p in plist:
                if p.is_filler or p.stop < now or p.start > now + timedelta(hours=48):
                    continue
                on = p.start <= now < p.stop
                rows.append((p.start, f"<tr><td>{'<span class=\"dot running\"></span> ' if on else ''}{_ts(p.start, tz)}</td><td class=muted>{_ts(p.stop, tz)}</td><td>{escape(ch_id)}</td><td>{escape(p.sub_title or p.title)}</td><td class=muted>{escape(p.onscreen or '')}</td></tr>"))
        cal = s["calendar"]
        out.append(f"<section class=card><h2>Coming up <span class=sub>next 48 h, {escape(app.cfg.epg.timezone)}</span></h2>")
        if rows:
            out.append("<div class=scroll><table><tr><th>Start</th><th>End</th><th>Channel</th><th>Session</th><th>Episode</th></tr>" + "".join(r for _, r in sorted(rows, key=lambda x: x[0])) + "</table></div>")
        else:
            out.append("<div class=empty>No sessions in the next 48 hours.</div>")
        updated = _ts(datetime.fromisoformat(cal["updated_at"]), tz) if cal["updated_at"] else "never"
        out.append(f"<p class='muted small'>F1 calendar updated {escape(updated)}" + (f" · <span class=err>{escape(cal['error'])}</span>" if cal["error"] else "") + "</p>")
        if cal["date_only_sessions"]:
            out.append("<p class=degraded>Sessions without a start time (all-day placeholders): " + escape(", ".join(cal["date_only_sessions"])) + "</p>")
        out.append("</section>")
        # Needs review (static; assigning reloads)
        if s["needs_review"]:
            sessions = app.calendar.sessions()
            opts = "".join(
                f"<option value='{escape(x.id)}'>{escape(x.id)} – {escape((app.calendar.event(x.event_id).name if app.calendar.event(x.event_id) else ''))} {escape(x.title)}</option>"
                for x in sessions
            ) + "".join(f"<option value='{escape(e.id)}-SUPP'>{escape(e.id)} – {escape(e.name)} (supplementary)</option>" for e in app.calendar.events)
            out.append(f"<section class=card><h2>Needs review <span class=sub>{len(s['needs_review'])} file(s)</span></h2><div class=scroll><table><tr><th>File</th><th>Reason</th><th>Assign to session</th></tr>")
            for item in s["needs_review"]:
                cands = "<br>".join(escape(c) for c in item["candidates"])
                out.append(
                    f"<tr><td>{escape(item['path'])}<div class='muted small'>{cands}</div></td><td>{escape(item['reason'])}</td><td>"
                    f"<form class=row method=post action='/ui/review/assign' data-ajax data-after=reload><input type=hidden name=path value='{escape(item['path'], quote=True)}'>"
                    f"<select name=session_id>{opts}</select> <input name=title placeholder='title (supplementary)' size=16> <button class=primary>Assign</button></form></td></tr>"
                )
            out.append("</table></div></section>")
        # Replays: queue live, library static
        if app.replays:
            out.append("<section class=card><h2>Replay downloads</h2><div data-r=replays><div class=empty>Loading…</div></div></section>")
            toggle = (
                "<a class=btn href='/'>Hide results</a>" if spoilers_off and app.cfg.spoiler.enabled
                else ("<a class=btn href='/?spoilers=show'>Show results</a>" if app.cfg.spoiler.enabled else "")
            )  # fmt: skip
            out.append(f"<section class=card><h2>Recently filed {toggle}</h2>")
            if s["library"]:
                out.append("<div class=scroll><table><tr><th>Episode</th><th>Title</th><th>Filed</th><th>Results</th></tr>")
                for li in s["library"]:
                    res = escape(app.ui_results.get(li["session_id"], "")) if spoilers_off and hasattr(app, "ui_results") else ""
                    out.append(f"<tr><td class=muted>{escape(str(li['episode_no'] or 'extra'))}</td><td>{escape(li['title'] or '')}</td><td class=muted>{escape((li['filed_at'] or '')[:16].replace('T', ' '))}</td><td>{res or ('<span class=muted>hidden</span>' if not spoilers_off else '')}</td></tr>")
                out.append("</table></div>")
            else:
                out.append("<div class=empty>Nothing filed yet.</div>")
            out.append("</section>")
        actions = (
            "<form class=inline method=post action='/ui/refresh-calendar' data-ajax data-after=reload><button title='Fetch the F1 calendar now'>Refresh calendar</button></form>"
            "<form class=inline method=post action='/ui/reload' data-ajax data-after=reload><button title='Re-read pitlane.yaml'>Reload config</button></form>"
        )
        return shell_page(app, "Overview", "overview", "".join(out), actions)

    @r.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if err := _check_auth(app, request):
            return err
        if request.query_params.get("spoilers") == "show" and app.replays:
            await _load_results(app)
        return page(request)

    @r.get("/api/state")
    async def api_state(request: Request):
        """Live UI snapshot (web/live.py)."""
        if err := _check_auth(app, request):
            return err
        return JSONResponse(json.loads(json.dumps(live_state(app), default=str)), headers={"Cache-Control": "no-store"})

    @r.get("/api/events")
    async def api_events(request: Request):
        """The live snapshot as Server-Sent Events, sent whenever it changes."""
        if err := _check_auth(app, request):
            return err
        return StreamingResponse(
            event_stream(app, request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

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
        return done(request, "/", "Config reloaded" if not errors else "Reload rejected: " + "; ".join(errors), ok=not errors)

    @r.post("/ui/refresh-calendar")
    async def refresh_calendar(request: Request):
        if err := _check_auth(app, request):
            return err
        if not _same_origin(request):
            return Response(status_code=403)
        await app.refresh_calendar()
        err = app.calendar.last_error
        return done(request, "/", f"Calendar refresh failed: {err}" if err else "Calendar refreshed", ok=not err)

    @r.post("/ui/review/assign")
    async def assign(request: Request):
        if err := _check_auth(app, request):
            return err
        if not _same_origin(request) or not app.replays:
            return Response(status_code=403)
        f = await _form(request)
        res = await app.replays.assign(f.get("path", ""), f.get("session_id", ""), f.get("title") or None)
        return done(request, "/", f"Assignment: {res.get('outcome')}")

    @r.post("/ui/downloads/retry")
    async def retry(request: Request):
        if err := _check_auth(app, request):
            return err
        if not _same_origin(request) or not app.replays:
            return Response(status_code=403)
        f = await _form(request)
        app.replays.retry_download(f.get("key", ""))
        return done(request, "/", "Download re-queued")

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
