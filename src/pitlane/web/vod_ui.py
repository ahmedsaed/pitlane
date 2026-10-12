"""Movies & Shows pages (VOD-2, docs/vod.md §5.7).

Browsing reads the local catalogue only. Opening a title fetches its details
once (VOD-3). Download state on every page is rendered live from the state
feed (web/shell.py). Stream URLs carry the panel credentials and are never
rendered.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from ..providers.base import ProviderError
from ..vod import catalog
from ..vod.manager import fmt_duration, fmt_size
from .shell import done, page
from .ui import _check_auth, _same_origin

if TYPE_CHECKING:
    from ..app import App

PAGE = 48


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
        # The title sits behind the image and shows if the image fails to load.
        return f"<div class=img><span>{escape(alt)}</span><img loading=lazy referrerpolicy=no-referrer src='{escape(url, quote=True)}' alt='' onerror='this.remove()'></div>"

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

    def facts(*items: str) -> str:
        return "<div class=facts>" + "".join(f"<span class=badge>{escape(i)}</span>" for i in items if i) + "</div>"

    # ------------------------------------------------------------------ browse
    @r.get("/vod", response_class=HTMLResponse)
    async def browse(request: Request):
        if err := guard(request):
            return err
        q = request.query_params.get("q", "")
        kind = request.query_params.get("kind", "")
        cat = request.query_params.get("cat", "")
        page_no = max(0, int(request.query_params.get("page", "0") or 0))
        cats = catalog.categories(app.state)
        rows = catalog.search(app.state, q, kind or None, cat or None, mgr().cfg.show_adult, limit=PAGE, offset=page_no * PAGE)
        age = catalog.catalog_age(app.state)
        out = ["<section class=card><form class=search method=get action='/vod'>"]
        out.append(f"<input type=search name=q value='{escape(q, quote=True)}' placeholder='Search {_count(app)}' autofocus>")
        out.append("<select name=kind onchange='this.form.submit()'>" + "".join(
            f"<option value='{v}'{' selected' if v == kind else ''}>{label}</option>" for v, label in (("", "Movies and shows"), ("movie", "Movies"), ("series", "Shows"))
        ) + "</select>")  # fmt: skip
        options = ["<option value=''>All categories</option>"]
        for k, label in (("movie", "Movies"), ("series", "Shows")):
            if kind and kind != k:
                continue
            options.append(f"<optgroup label='{label}'>" + "".join(
                f"<option{' selected' if c == cat else ''}>{escape(c)}</option>" for c in cats[k] if c
            ) + "</optgroup>")  # fmt: skip
        out.append(f"<select name=cat onchange='this.form.submit()'>{''.join(options)}</select><button class=primary>Search</button></form>")
        if age is None:
            out.append("<div class=empty>The catalogue has not been downloaded yet.</div>")
        elif not rows:
            out.append("<div class=empty>Nothing found.</div>")
        out.append("<div class=grid>")
        for row in rows:
            is_movie = row["kind"] == "movie"
            href = f"/vod/{'movie' if is_movie else 'series'}/{row['id']}"
            year = f" ({row['year']})" if row.get("year") and str(row["year"]) not in row["name"] else ""
            badges = "<span class='badge cam'>CAM</span> " if row.get("cam") else ""
            badges += "" if is_movie else "<span class=badge>Show</span> "
            live = f"<span data-vod-badge='movie:{row['id']}'></span>" if is_movie else ""
            out.append(
                f"<a class=card-t href='{href}' data-alt='{escape(row['name'], quote=True)}'>{img(row.get('poster'), row['name'])}"
                f"<div class=t><div class=name>{escape(row['name'])}{escape(year)}</div><div class='muted small'>{escape(row.get('category') or '')}</div>"
                f"<div class=row style='margin-top:4px'>{badges}{live}</div></div></a>"
            )
        out.append("</div><div class=pager>")
        params = {"q": q, "kind": kind, "cat": cat}
        if page_no:
            out.append(f"<a class=btn href='/vod?{urlencode({**params, 'page': page_no - 1})}'>← Newer</a>")
        if len(rows) == PAGE:
            out.append(f"<a class=btn href='/vod?{urlencode({**params, 'page': page_no + 1})}'>Older →</a>")
        out.append("</div></section>")
        when = f"updated {_ago(age)}" if age is not None else "never downloaded"
        out.append(
            f"<div class='muted small row'>Catalogue {when}; it refreshes daily.<form class=inline method=post action='/vod/refresh' data-ajax data-after=reload>"
            "<button title='4 requests to the panel'>Refresh now</button></form></div>"
        )
        return page(app, "Movies & Shows", "vod", "".join(out))

    @r.post("/vod/refresh")
    async def refresh(request: Request):
        if err := guard(request, post=True):
            return err
        try:
            stats = await mgr().refresh_catalog(force=True)
        except ProviderError as e:
            return done(request, "/vod", f"Refresh failed: {e}", ok=False)
        return done(request, "/vod", f"Catalogue refreshed: {stats['movies']:,} movies, {stats['series']:,} shows")

    # ------------------------------------------------------------------ movie
    @r.get("/vod/movie/{vod_id}", response_class=HTMLResponse)
    async def movie_page(vod_id: int, request: Request):
        if err := guard(request):
            return err
        try:
            m = await mgr().movie(vod_id)
        except ProviderError as e:
            return page(app, "Movie", "vod", f"<section class=card><p class=err>{escape(str(e))}</p><a class=btn href='/vod'>← Back</a></section>")
        quality = [
            _res(m["width"], m["height"]),
            (m["vcodec"] or "").upper(),
            fmt_duration(m["duration_s"]),
            f"≈ {fmt_size(m['size'])}" if m["size"] else "size unknown",
            f"★ {m['rating']}" if m["rating"] else "",
        ]
        out = [f"<section class=card><div class=detail><div class=poster data-alt='{escape(m['title'], quote=True)}'>{img(m['poster'], m['title'])}</div><div class=info>"]
        out.append(f"<h2>{escape(m['title'])}{f' <span class=muted>({m['year']})</span>' if m['year'] else ''}</h2>")
        out.append(facts(*quality, m["genre"] or "", m["category"] or ""))
        if m["cam"]:
            out.append("<p><span class='badge cam'>CAM</span> <span class=small>Filmed in a cinema: poor picture and sound.</span></p>")
        out.append(f"<p>{escape(m['plot'] or '')}</p>")
        if m["cast"]:
            out.append(f"<p class='muted small'>{escape(m['cast'])}</p>")
        out.append(
            f"<p class='muted small'>{'Plex will match it exactly (TMDB ' + escape(m['tmdb']) + ').' if m['tmdb'] else 'No TMDB ID: Plex matches it by name and year.'}"
            " Downloads run at about playback speed and only while no live channel is playing.</p>"
        )
        out.append(
            f"<div data-vod-item='movie:{vod_id}' data-request-url='/vod/request/movie' data-request-id='{vod_id}' data-play='movie={vod_id}'>"
            f"<noscript><form method=post action='/vod/request/movie'><input type=hidden name=id value={vod_id}><button class=primary>Download to Plex</button></form></noscript></div>"
        )
        out.append("</div></div></section>")
        others = [v for v in catalog.versions(app.state, vod_id, mgr().cfg.show_adult) if v["id"] != vod_id]
        if others:
            out.append("<section class=card><h2>Other copies on the panel <span class=sub>open one to see its quality and size</span></h2><div class=scroll><table><tr><th>Name</th><th>Category</th><th>Added</th><th></th></tr>")
            for v in others:
                added = datetime.fromtimestamp(v["added"]).strftime("%d %b %Y") if v["added"] else ""
                cam = "<span class='badge cam'>CAM</span>" if v["cam"] else ""
                out.append(f"<tr><td><a href='/vod/movie/{v['id']}'>{escape(v['name'])}</a> {cam}</td><td class=muted>{escape(v['category'] or '')}</td><td class=muted>{added}</td><td><span data-vod-badge='movie:{v['id']}'></span></td></tr>")
            out.append("</table></div></section>")
        return page(app, m["title"], "vod", "".join(out))

    @r.post("/vod/play")
    async def play(request: Request):
        """Play now: put a movie (``movie``) or an episode (``series`` + ``episode``) on the play channel."""
        if err := guard(request, post=True):
            return err
        pc = mgr().cfg.play_channel
        f = await form(request)
        back = request.headers.get("referer") or "/vod"
        if pc is None:
            return done(request, back, "The play channel is turned off (vod.play_channel)", ok=False)
        try:
            if f.get("episode"):
                sel = await mgr().play_episode(int(f.get("series", ["0"])[0]), f["episode"][0])
            else:
                sel = await mgr().play_movie(int(f.get("movie", ["0"])[0]))
        except ProviderError as e:
            return done(request, back, f"Play failed: {e}", ok=False)
        if sel["switched"]:
            return done(request, back, f"Switched channel {pc.guide_number} to {sel['title']}")
        return done(request, back, f"{sel['title']} is ready on channel {pc.guide_number} ({pc.name}): tune to it in Plex")

    @r.post("/vod/request/movie")
    async def request_movie(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        vod_id = int(f.get("id", ["0"])[0])
        try:
            ok = await mgr().request_movie(vod_id)
        except ProviderError as e:
            return done(request, f"/vod/movie/{vod_id}", f"Request failed: {e}", ok=False)
        return done(request, f"/vod/movie/{vod_id}", "Added to the download queue" if ok else "Already in the queue")

    # ------------------------------------------------------------------ series
    @r.get("/vod/series/{series_id}", response_class=HTMLResponse)
    async def series_page(series_id: int, request: Request):
        if err := guard(request):
            return err
        try:
            s = await mgr().series(series_id)
        except ProviderError as e:
            return page(app, "Show", "vod", f"<section class=card><p class=err>{escape(str(e))}</p><a class=btn href='/vod'>← Back</a></section>")
        follow = app.state.one("SELECT * FROM vod_follows WHERE series_id=?", (series_id,))
        seasons = list(s["seasons"])
        n_eps = sum(len(v) for v in s["seasons"].values())
        out = [f"<section class=card><div class=detail><div class=poster data-alt='{escape(s['title'], quote=True)}'>{img(s['poster'], s['title'])}</div><div class=info>"]
        out.append(f"<h2>{escape(s['title'])}{f' <span class=muted>({s['year']})</span>' if s['year'] else ''}</h2>")
        out.append(facts(f"{len(seasons)} season{'s' if len(seasons) != 1 else ''}", f"{n_eps} episodes", f"★ {s['rating']}" if s["rating"] else "", s["genre"] or "", s["category"] or ""))
        out.append(f"<p>{escape(s['plot'] or '')}</p>")
        if s["cast"]:
            out.append(f"<p class='muted small'>{escape(s['cast'])}</p>")
        if follow:
            out.append(
                f"<form class=row method=post action='/vod/follow' data-ajax data-after=reload><input type=hidden name=id value={series_id}><input type=hidden name=off value=1>"
                f"<span class=badge><span class='dot online'></span>Following</span><span class=small>New episodes from season {follow['from_season']} download automatically.</span><button>Stop following</button></form>"
            )
        elif seasons:
            opts = "".join(f"<option{' selected' if n == seasons[-1] else ''}>{n}</option>" for n in seasons)
            out.append(
                f"<form class=row method=post action='/vod/follow' data-ajax data-after=reload><input type=hidden name=id value={series_id}>"
                f"<span class=small>Download new episodes automatically from season</span><select name=from_season>{opts}</select><button>Follow</button></form>"
            )
        out.append(
            "<details style='margin-top:10px'><summary class=small>Plex matching</summary>"
            f"<form class=row method=post action='/vod/series-tmdb' data-ajax style='margin-top:6px'><input type=hidden name=id value={series_id}>"
            f"<span class=small>TMDB ID (if Plex picks the wrong show):</span><input name=tmdb size=9 value='{escape(s['tmdb'] or '', quote=True)}' placeholder='e.g. 247718'><button>Save</button></form></details>"
        )
        out.append("</div></div></section>")
        play_gn = mgr().cfg.play_channel.guide_number if mgr().cfg.play_channel else None
        out.append(f"<form method=post action='/vod/request/episodes' data-ajax data-after=clear><input type=hidden name=id value={series_id}><input type=hidden name=series value={series_id}>")
        for i, (season, eps) in enumerate(s["seasons"].items()):
            total = sum(e["size"] or 0 for e in eps)
            # Collapsible: the first season starts open (the only one, for most shows).
            out.append(
                f"<details class='card season'{' open' if i == 0 else ''}><summary><b>Season {season}</b>"
                f"<span class=sub>{len(eps)} episodes · ≈ {fmt_size(total)}</span></summary>"
                f"<div class=row style='justify-content:flex-end;margin:4px 0 8px'><button type=button data-check-all='{season}'>Select all</button>"
                f"<button name=season value={season} class=primary>⬇ Download season {season}</button></div>"
                "<div class=scroll><table><tr><th style='width:28px'></th><th>#</th><th>Title</th><th>Length</th><th>Size</th><th>Quality</th><th></th><th></th></tr>"
            )
            for e in eps:
                k = escape(f"ep:{e['id']}", quote=True)
                eid = escape(e["id"], quote=True)
                q = _res(e["width"], e["height"])
                play = f"<button formaction='/vod/play' name=episode value='{eid}' title='Play on channel {escape(play_gn)}'>▶ Play</button>" if play_gn else ""
                out.append(
                    f"<tr><td><input type=checkbox name=ep value='{eid}' data-season='{season}' data-vod-check='{k}'></td>"
                    f"<td class=muted>{e['episode']}</td><td>{escape(e['title'])}</td><td class=muted>{fmt_duration(e['duration_s'])}</td>"
                    f"<td class=muted>{fmt_size(e['size'])}</td><td class=muted>{q}</td><td><span data-vod-badge='{k}'></span></td>"
                    f"<td class=row style='justify-content:flex-end;flex-wrap:nowrap'>{play}"
                    f"<button name=only value='{eid}' data-vod-dl='{k}' title='Download this episode to Plex'>⬇ Download</button></td></tr>"
                )
            out.append("</table></div></details>")
        if s["seasons"]:
            whole = fmt_size(sum(e["size"] or 0 for v in s["seasons"].values() for e in v))
            out.append(
                "<div class=row style='justify-content:flex-end'><button>Download selected episodes</button>"
                f"<button name=all value=1 data-confirm='Download all {n_eps} episodes (≈ {whole})?'>Download the whole show</button></div>"
            )
        else:
            out.append("<section class=card><div class=empty>No episodes on the panel.</div></section>")
        out.append("</form>")
        return page(app, s["title"], "vod", "".join(out))

    @r.post("/vod/request/episodes")
    async def request_episodes(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        sid = int(f.get("id", ["0"])[0])
        back = f"/vod/series/{sid}"
        try:
            s = await mgr().series(sid)
            if f.get("only"):
                ids = f["only"][:1]
            elif f.get("all"):
                ids = [e["id"] for eps in s["seasons"].values() for e in eps]
            elif f.get("season"):
                ids = [e["id"] for e in s["seasons"].get(int(f["season"][0]), [])]
            else:
                ids = f.get("ep", [])
            if not ids:
                return done(request, back, "Select some episodes first", ok=False)
            n = await mgr().request_episodes(sid, ids)
        except ProviderError as e:
            return done(request, back, f"Request failed: {e}", ok=False)
        return done(request, back, f"{n} episode{'s' if n != 1 else ''} added to the download queue" if n else "Those episodes are already in the queue")

    @r.post("/vod/follow")
    async def follow(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        sid = int(f.get("id", ["0"])[0])
        if f.get("off"):
            mgr().unfollow(sid)
            return done(request, f"/vod/series/{sid}", "No longer following")
        try:
            await mgr().follow(sid, int(f.get("from_season", ["1"])[0]))
        except ProviderError as e:
            return done(request, f"/vod/series/{sid}", f"Follow failed: {e}", ok=False)
        return done(request, f"/vod/series/{sid}", "Following: new episodes will download automatically")

    @r.post("/vod/series-tmdb")
    async def series_tmdb(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        sid = int(f.get("id", ["0"])[0])
        try:
            mgr().set_series_tmdb(sid, f.get("tmdb", [""])[0])
        except ValueError as e:
            return done(request, f"/vod/series/{sid}", str(e), ok=False)
        return done(request, f"/vod/series/{sid}", "Saved; applies to episodes requested from now on")

    # ------------------------------------------------------------------ downloads
    @r.get("/vod/downloads", response_class=HTMLResponse)
    async def downloads(request: Request):
        if err := guard(request):
            return err
        await mgr().machine_id()  # for "Fix Match" links
        body = (
            "<section class=card><h2>Downloads</h2><div data-r=vodStatus><div class=empty>Loading…</div></div></section>"
            "<section class=card hidden><h2>Needs a Fix Match in Plex <span class=sub>Plex could not identify these</span></h2><div data-r=vodUnmatched></div></section>"
            "<section class=card><h2>Queue</h2><div data-r=vodQueue><div class=empty>Loading…</div></div>"
            "<noscript><p class=muted>This page needs JavaScript.</p></noscript></section>"
        )
        return page(app, "Downloads", "downloads", body)

    @r.post("/vod/item")
    async def item_action(request: Request):
        if err := guard(request, post=True):
            return err
        f = await form(request)
        action = f.get("action", [""])[0]
        try:
            mgr().item_action(f.get("key", [""])[0], action)
        except (KeyError, ValueError) as e:
            return done(request, "/vod/downloads", f"Not possible: {e}", ok=False)
        verb = {"pause": "Paused", "resume": "Resumed", "retry": "Queued again", "cancel": "Cancelled"}.get(action, "Done")
        return done(request, "/vod/downloads", verb)

    return r


def _res(width: int | None, height: int | None) -> str:
    """Name the resolution by width: a 1920×802 widescreen film is "1080p", not "802p"."""
    if not width:
        return f"{height}p" if height else ""
    for w, name in ((3800, "4K"), (1900, "1080p"), (1260, "720p"), (700, "SD")):
        if width >= w:
            return name
    return f"{width}×{height}"


def _count(app: "App") -> str:
    m = app.state.one("SELECT (SELECT COUNT(*) FROM vod_titles) AS m, (SELECT COUNT(*) FROM vod_series) AS s")
    return f"{m['m']:,} movies and {m['s']:,} shows" if m and m["m"] else "movies and shows"


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"
