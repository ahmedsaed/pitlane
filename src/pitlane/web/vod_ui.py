"""Movies & Shows pages (VOD-2, docs/vod.md §5.7).

Browsing reads the local catalogue only. Opening a title fetches its details
once (VOD-3). Stream URLs carry the panel credentials and are never rendered.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..providers.base import ProviderError
from ..vod import catalog
from ..vod.manager import fmt_duration, fmt_size
from .ui import CSS, _check_auth, _same_origin

if TYPE_CHECKING:
    from ..app import App

PAGE = 48
VOD_CSS = """
nav.tabs{display:flex;gap:14px;align-items:center}nav.tabs a{color:var(--fg)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px}
.card{display:block;color:inherit;text-decoration:none;background:var(--bg);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.card .img{aspect-ratio:2/3;background:var(--line);display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px;text-align:center;padding:6px}
.card img{width:100%;height:100%;object-fit:cover;display:block}.card .t{padding:6px 8px;font-size:13px}
.badge{display:inline-block;font-size:11px;padding:0 6px;border-radius:9px;border:1px solid var(--line);color:var(--muted);margin-right:4px}
.badge.cam{color:var(--bad);border-color:var(--bad)}.detail{display:flex;gap:16px;flex-wrap:wrap}.detail img{width:200px;border-radius:8px}
.detail .info{flex:1;min-width:260px}.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;min-width:80px}
.bar>div{height:100%;background:var(--accent)}form.search{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
form.search input[type=search]{flex:1;min-width:180px;padding:4px 8px}.pager{display:flex;gap:12px;margin-top:12px}
"""


def vod_router(app: "App") -> APIRouter:
    r = APIRouter()

    def mgr() -> Any:
        return app.vod

    def panel_host() -> str | None:
        try:
            return urlparse(mgr().provider.cfg.url).hostname
        except ProviderError:
            return None

    def img(url: str | None, alt: str) -> str:
        """Artwork from image hosts only: never from the panel itself (it would count as panel traffic)."""
        if not url or urlparse(url).scheme not in ("http", "https") or urlparse(url).hostname == panel_host():
            return f"<div class=img>{escape(alt)}</div>"
        return f"<div class=img><img loading=lazy referrerpolicy=no-referrer src='{escape(url, quote=True)}' alt=''></div>"

    def shell(title: str, body: str, request: Request, refresh: int | None = None) -> HTMLResponse:
        msg = request.query_params.get("msg")
        head = f"<meta http-equiv=refresh content={refresh}>" if refresh else ""
        return HTMLResponse(
            f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>{head}"
            f"<title>{escape(title)} · Pitlane</title><style>{CSS}{VOD_CSS}</style></head><body>"
            f"<header><h1>Pitlane · {escape(title)}</h1><nav class=tabs><a href='/'>Dashboard</a><a href='/vod'>Movies &amp; Shows</a>"
            f"<a href='/vod/downloads'>Downloads</a></nav><span class=muted>{escape(mgr().summary())}</span></header>"
            + (f"<div class=note>{escape(msg)}</div>" if msg else "")
            + f"<main>{body}</main></body></html>"
        )

    def back(path: str, msg: str) -> RedirectResponse:
        return RedirectResponse(f"{path}{'&' if '?' in path else '?'}msg={quote(msg)}", status_code=303)

    async def form(request: Request) -> dict[str, list[str]]:
        return parse_qs((await request.body()).decode())

    def guard(request: Request, post: bool = False) -> Response | None:
        if err := _check_auth(app, request):
            return err
        if mgr() is None:
            return HTMLResponse("Movies & Shows are not configured (vod in pitlane.yaml).", status_code=404)
        if post and not _same_origin(request):
            return Response(status_code=403)
        return None

    def status_badge(key: str) -> str:
        row = app.state.one("SELECT status, bytes_done, total_size, expected_size FROM vod_items WHERE key=?", (key,))
        if not row or row["status"] in ("cancelled", "deleted"):
            return ""
        return f"<span class=badge>{escape(row['status'])}</span>"

    # ------------------------------------------------------------------ browse
    @r.get("/vod", response_class=HTMLResponse)
    async def browse(request: Request):
        if err := guard(request):
            return err
        q = request.query_params.get("q", "")
        kind = request.query_params.get("kind", "")
        cat = request.query_params.get("cat", "")
        page = max(0, int(request.query_params.get("page", "0") or 0))
        cats = catalog.categories(app.state)
        rows = catalog.search(app.state, q, kind or None, cat or None, mgr().cfg.show_adult, limit=PAGE, offset=page * PAGE)
        age = catalog.catalog_age(app.state)
        out = ["<section><form class=search method=get action='/vod'>"]
        out.append(f"<input type=search name=q value='{escape(q, quote=True)}' placeholder='Search movies and shows' autofocus>")
        out.append("<select name=kind>" + "".join(
            f"<option value='{v}'{' selected' if v == kind else ''}>{label}</option>" for v, label in (("", "All"), ("movie", "Movies"), ("series", "Shows"))
        ) + "</select>")  # fmt: skip
        options = ["<option value=''>All categories</option>"]
        for k, label in (("movie", "Movies"), ("series", "Shows")):
            options.append(f"<optgroup label='{label}'>" + "".join(
                f"<option{' selected' if c == cat else ''}>{escape(c)}</option>" for c in cats[k] if c
            ) + "</optgroup>")  # fmt: skip
        out.append(f"<select name=cat>{''.join(options)}</select><button>Search</button></form>")
        if age is None:
            out.append("<p class=muted>The catalogue has not been downloaded yet.</p>")
        if not rows:
            out.append("<p class=muted>Nothing found.</p>")
        out.append("<div class=grid>")
        for row in rows:
            href = f"/vod/{'movie' if row['kind'] == 'movie' else 'series'}/{row['id']}"
            key = f"movie:{row['id']}" if row["kind"] == "movie" else None
            badges = ("<span class='badge cam'>CAM</span>" if row.get("cam") else "") + (status_badge(key) if key else "")
            badges += "" if row["kind"] == "movie" else "<span class=badge>show</span>"
            year = f" ({row['year']})" if row.get("year") else ""
            out.append(f"<a class=card href='{href}'>{img(row.get('poster'), row['name'])}<div class=t>{escape(row['name'])}{escape(year) if year not in row['name'] else ''}<br>{badges}<span class=muted>{escape(row.get('category') or '')}</span></div></a>")
        out.append("</div><div class=pager>")
        params = {"q": q, "kind": kind, "cat": cat}
        if page:
            out.append(f"<a href='/vod?{urlencode({**params, 'page': page - 1})}'>← Newer</a>")
        if len(rows) == PAGE:
            out.append(f"<a href='/vod?{urlencode({**params, 'page': page + 1})}'>Older →</a>")
        out.append("</div>")
        when = f"updated {int(age // 3600)} h ago" if age is not None else "never updated"
        out.append(f"<p class=muted>Catalogue {when}. <form class=inline method=post action='/vod/refresh'><button>Refresh now</button></form> (4 requests to the panel)</p></section>")
        return shell("Movies & Shows", "".join(out), request)

    @r.post("/vod/refresh")
    async def refresh(request: Request):
        if err := guard(request, post=True):
            return err
        try:
            stats = await mgr().refresh_catalog(force=True)
        except ProviderError as e:
            return back("/vod", f"Refresh failed: {e}")
        return back("/vod", f"Catalogue refreshed: {stats['movies']} movies, {stats['series']} shows")

    # ------------------------------------------------------------------ movie
    @r.get("/vod/movie/{vod_id}", response_class=HTMLResponse)
    async def movie_page(vod_id: int, request: Request):
        if err := guard(request):
            return err
        try:
            m = await mgr().movie(vod_id)
        except ProviderError as e:
            return shell("Movie", f"<section><p class=offline>{escape(str(e))}</p></section>", request)
        key = f"movie:{vod_id}"
        item = app.state.one("SELECT * FROM vod_items WHERE key=?", (key,))
        quality = " · ".join(x for x in (
            f"{m['width']}×{m['height']}" if m["width"] else "", m["vcodec"] or "", m["acodec"] or "",
            fmt_duration(m["duration_s"]), f"about {fmt_size(m['size'])}" if m["size"] else "size unknown",
        ) if x)  # fmt: skip
        out = [f"<section class=detail>{img(m['poster'], m['title'])}<div class=info>"]
        out.append(f"<h2>{escape(m['title'])}{f' ({m['year']})' if m['year'] else ''}</h2>")
        if m["cam"]:
            out.append("<p><span class='badge cam'>CAM</span> filmed in a cinema; poor quality</p>")
        out.append(f"<p class=muted>{escape(m['genre'] or '')} · {escape(str(m['category'] or ''))}" + (f" · TMDB {escape(m['tmdb'])}" if m["tmdb"] else " · no TMDB ID: Plex matches by name and year") + "</p>")
        out.append(f"<p>{escape(m['plot'] or '')}</p><p class=muted>{escape(m['cast'] or '')}</p><p>{escape(quality)}</p>")
        if item and item["status"] not in ("cancelled", "deleted", "failed"):
            out.append(f"<p>Status: <b>{escape(item['status'])}</b> · <a href='/vod/downloads'>Downloads</a></p>")
        else:
            out.append(f"<form method=post action='/vod/request/movie'><input type=hidden name=id value={vod_id}><button>Request</button></form>")
        out.append("</div></section>")
        others = [v for v in catalog.versions(app.state, vod_id, mgr().cfg.show_adult) if v["id"] != vod_id]
        if others:
            out.append("<section><h2>Other copies on the panel</h2><table><tr><th>Name</th><th>Category</th><th>Added</th><th></th></tr>")
            for v in others:
                out.append(f"<tr><td><a href='/vod/movie/{v['id']}'>{escape(v['name'])}</a> {'<span class=\"badge cam\">CAM</span>' if v['cam'] else ''}</td><td>{escape(v['category'] or '')}</td><td class=muted>{v['added'] or ''}</td><td>{status_badge('movie:' + str(v['id']))}</td></tr>")
            out.append("</table><p class=muted>Open a copy to see its resolution and size (one request each).</p></section>")
        return shell(m["title"], "".join(out), request)

    @r.post("/vod/request/movie")
    async def request_movie(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        vod_id = int(f.get("id", ["0"])[0])
        try:
            ok = await mgr().request_movie(vod_id)
        except ProviderError as e:
            return back(f"/vod/movie/{vod_id}", f"Request failed: {e}")
        return back(f"/vod/movie/{vod_id}", "Added to the download queue" if ok else "Already requested")

    # ------------------------------------------------------------------ series
    @r.get("/vod/series/{series_id}", response_class=HTMLResponse)
    async def series_page(series_id: int, request: Request):
        if err := guard(request):
            return err
        try:
            s = await mgr().series(series_id)
        except ProviderError as e:
            return shell("Show", f"<section><p class=offline>{escape(str(e))}</p></section>", request)
        follow = app.state.one("SELECT * FROM vod_follows WHERE series_id=?", (series_id,))
        items = {r["key"]: r for r in app.state.query("SELECT key, status FROM vod_items WHERE series_id=?", (series_id,))}
        out = [f"<section class=detail>{img(s['poster'], s['title'])}<div class=info>"]
        out.append(f"<h2>{escape(s['title'])}{f' ({s['year']})' if s['year'] else ''}</h2>")
        out.append(f"<p class=muted>{escape(s['genre'] or '')} · {escape(str(s['category'] or ''))}</p><p>{escape(s['plot'] or '')}</p><p class=muted>{escape(s['cast'] or '')}</p>")
        seasons = list(s["seasons"])
        if follow:
            out.append(f"<form method=post action='/vod/follow'><input type=hidden name=id value={series_id}><input type=hidden name=off value=1>"
                       f"Following: new episodes from season {follow['from_season']} are requested automatically. <button>Stop following</button></form>")  # fmt: skip
        elif seasons:
            opts = "".join(f"<option{' selected' if n == seasons[-1] else ''}>{n}</option>" for n in seasons)
            out.append(f"<form method=post action='/vod/follow'><input type=hidden name=id value={series_id}>Follow new episodes from season <select name=from_season>{opts}</select> <button>Follow</button></form>")
        out.append(f"<form method=post action='/vod/series-tmdb'><input type=hidden name=id value={series_id}>TMDB ID for Plex matching: "
                   f"<input name=tmdb size=8 value='{escape(s['tmdb'] or '', quote=True)}' placeholder='optional'> <button>Save</button></form>")  # fmt: skip
        out.append("</div></section>")
        out.append(f"<form method=post action='/vod/request/episodes'><input type=hidden name=id value={series_id}>")
        for season, eps in s["seasons"].items():
            total = sum(e["size"] or 0 for e in eps)
            out.append(f"<section><h2>Season {season} <span class=muted>({len(eps)} episodes, about {fmt_size(total)})</span> "
                       f"<button name=season value={season}>Request season {season}</button></h2><table><tr><th></th><th>#</th><th>Title</th><th>Length</th><th>Size</th><th>Quality</th><th></th></tr>")  # fmt: skip
            for e in eps:
                st = items.get(f"ep:{e['id']}")
                taken = st is not None and st["status"] not in ("cancelled", "deleted", "failed")
                box = "" if taken else f"<input type=checkbox name=ep value='{escape(e['id'], quote=True)}'>"
                q = f"{e['width']}×{e['height']}" if e["width"] else ""
                out.append(f"<tr><td>{box}</td><td>{e['episode']}</td><td>{escape(e['title'])}</td><td>{fmt_duration(e['duration_s'])}</td><td>{fmt_size(e['size'])}</td><td>{q}</td><td>{escape(st['status']) if taken else ''}</td></tr>")
            out.append("</table></section>")
        if s["seasons"]:
            out.append("<p><button>Request selected episodes</button> <button name=all value=1>Request the whole show</button></p>")
        else:
            out.append("<p class=muted>No episodes on the panel.</p>")
        out.append("</form>")
        return shell(s["title"], "".join(out), request)

    @r.post("/vod/request/episodes")
    async def request_episodes(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        sid = int(f.get("id", ["0"])[0])
        try:
            s = await mgr().series(sid)
            if f.get("all"):
                ids = [e["id"] for eps in s["seasons"].values() for e in eps]
            elif f.get("season"):
                ids = [e["id"] for e in s["seasons"].get(int(f["season"][0]), [])]
            else:
                ids = f.get("ep", [])
            n = await mgr().request_episodes(sid, ids)
        except ProviderError as e:
            return back(f"/vod/series/{sid}", f"Request failed: {e}")
        return back(f"/vod/series/{sid}", f"{n} episode(s) added to the download queue" if n else "Nothing new to request")

    @r.post("/vod/follow")
    async def follow(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        sid = int(f.get("id", ["0"])[0])
        if f.get("off"):
            mgr().unfollow(sid)
            return back(f"/vod/series/{sid}", "No longer following")
        try:
            await mgr().follow(sid, int(f.get("from_season", ["1"])[0]))
        except ProviderError as e:
            return back(f"/vod/series/{sid}", f"Follow failed: {e}")
        return back(f"/vod/series/{sid}", "Following: new episodes will be requested automatically")

    @r.post("/vod/series-tmdb")
    async def series_tmdb(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        sid = int(f.get("id", ["0"])[0])
        try:
            mgr().set_series_tmdb(sid, f.get("tmdb", [""])[0])
        except ValueError as e:
            return back(f"/vod/series/{sid}", str(e))
        return back(f"/vod/series/{sid}", "Saved; applies to episodes requested from now on")

    # ------------------------------------------------------------------ downloads
    @r.get("/vod/downloads", response_class=HTMLResponse)
    async def downloads(request: Request):
        if err := guard(request):
            return err
        m = mgr()
        cfg = m.cfg
        use = m.usage()
        rows = app.state.query(
            "SELECT * FROM vod_items WHERE status != 'deleted' ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 "
            "WHEN 'paused' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END, created_at DESC LIMIT 300"
        )
        out = ["<section><h2>Status</h2><table>"]
        cur = m.current
        if cur:
            pct = int(100 * (cur["done"] or 0) / cur["total"]) if cur.get("total") else 0
            out.append(f"<tr><th>Downloading</th><td>{escape(cur['title'])} · {fmt_size(cur['done'])} of {fmt_size(cur.get('total'))} · {fmt_size(cur.get('rate'))}/s <div class=bar><div style='width:{pct}%'></div></div></td></tr>")
        if m.blocked:
            out.append(f"<tr><th>Waiting</th><td>{escape(m.blocked)}</td></tr>")
        caps = lambda used, cap: fmt_size(used) + (f" of {cap:g} GB" if cap else "")  # noqa: E731
        out.append(f"<tr><th>Data used</th><td>today {caps(use['day'], cfg.max_gb_per_day)} · this month {caps(use['month'], cfg.max_gb_per_month)}</td></tr>")
        free = {k: m.free_space(k) for k in ("movie", "episode")}
        out.append(f"<tr><th>Free space</th><td>movies {fmt_size(free['movie'])} · shows {fmt_size(free['episode'])} (keeps {fmt_size(cfg.min_free_space)} free)</td></tr>")
        rng = app.state.get("vod.range_supported")
        out.append("<tr><th>Resume</th><td>" + {"1": "the panel supports resuming downloads", "0": "the panel does not support resuming; a download paused for live TV restarts"}.get(rng or "", "not known yet (checked on the first resumed download)") + "</td></tr>")
        out.append(f"<tr><th>Rules</th><td class=muted>window {escape(cfg.download_window or 'any time')} · speed {fmt_size(cfg.max_rate) + '/s' if cfg.max_rate else 'unlimited'} · pauses for live TV and around F1 sessions</td></tr></table></section>")
        mid = await m.machine_id()
        unmatched = [r for r in rows if r["match_state"] in ("unmatched", "not found")]
        if unmatched:
            out.append("<section><h2>Needs a Fix Match in Plex</h2><table>")
            for row in unmatched:
                link = (
                    f"<a target=_blank rel=noopener href='https://app.plex.tv/desktop/#!/server/{escape(mid)}/details?key={quote('/library/metadata/' + row['plex_rating_key'], safe='')}'>Open in Plex</a>"
                    if mid and row["plex_rating_key"] else "not in Plex yet"
                )  # fmt: skip
                out.append(f"<tr><td>{escape(row['title'])}</td><td>{escape(row['match_state'])}</td><td>{link}</td></tr>")
            out.append("</table></section>")
        out.append("<section><h2>Queue</h2>")
        if not rows:
            out.append("<p class=muted>Nothing requested yet. <a href='/vod'>Browse movies and shows</a>.</p>")
        else:
            out.append("<table><tr><th>Title</th><th>Status</th><th>Progress</th><th>Size</th><th>Plex</th><th>Note</th><th></th></tr>")
            for row in rows:
                total = row["total_size"] or row["expected_size"]
                pct = int(100 * (row["bytes_done"] or 0) / total) if total else 0
                pct = 100 if row["status"] == "done" else min(pct, 99)
                actions = {
                    "queued": ("pause", "cancel"), "running": ("pause", "cancel"), "paused": ("resume", "cancel"),
                    "failed": ("retry", "cancel"), "cancelled": ("retry",), "done": (),
                }.get(row["status"], ())  # fmt: skip
                btns = "".join(
                    f"<form class=inline method=post action='/vod/item'><input type=hidden name=key value='{escape(row['key'], quote=True)}'>"
                    f"<button name=action value={a}>{a.title()}</button></form> "
                    for a in actions
                )
                out.append(
                    f"<tr><td>{escape(row['title'])}</td><td>{escape(row['status'])}</td><td><div class=bar><div style='width:{pct}%'></div></div></td>"
                    f"<td>{fmt_size(total)}</td><td>{escape(row['match_state'] or '')}</td><td class=muted>{escape(row['last_error'] or '')}</td><td>{btns}</td></tr>"
                )
            out.append("</table>")
        out.append("</section>")
        return shell("Downloads", "".join(out), request, refresh=10 if cur else None)

    @r.post("/vod/item")
    async def item_action(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        try:
            mgr().item_action(f.get("key", [""])[0], f.get("action", [""])[0])
        except (KeyError, ValueError) as e:
            return back("/vod/downloads", f"Not possible: {e}")
        return back("/vod/downloads", "Done")

    return r
