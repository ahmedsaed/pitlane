"""docs/vod.md: xtream provider, VOD catalogue, downloads, follows, retention, pages."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fake_xtream import PANEL, PASS, USER, FakePanel, FakePlex, config, make_media, transport
from pitlane.config import ConfigError, parse_config
from pitlane.connections import ConnectionLimiter
from conftest import needs_ffmpeg

MEDIA = make_media()


@pytest.fixture
def panel():
    return FakePanel(MEDIA)


async def make_app(tmp_path: Path, panel: FakePanel, plex: FakePlex | None = None, **vod_extra):
    from pitlane.app import App

    cfg = parse_config(config(tmp_path, **vod_extra), environ={})
    plex = plex or FakePlex({6: tmp_path / "IPTV Movies", 7: tmp_path / "IPTV Shows"})
    app = App(
        cfg,
        http_transport=transport(panel, plex),
        start_background=False,
        clock=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=UTC),  # no F1 session near
    )
    await app.startup()
    app.fake_plex = plex  # type: ignore[attr-defined]
    return app


# ------------------------------------------------------------------ config


def test_config_rules(tmp_path):
    raw = config(tmp_path)
    cfg = parse_config(raw, environ={})
    assert cfg.vod.provider == "iptv" and cfg.vod.min_free_space == 1_000_000
    assert cfg.connection_limits() == {"iptv": 1}

    inline = config(tmp_path)
    inline["providers"][0]["password"] = "plain"
    with pytest.raises(ConfigError) as e:
        parse_config(inline, environ={})
    assert any("providers[0].password: secrets must be given" in x for x in e.value.errors)

    novod = config(tmp_path)
    novod["providers"][0]["vod"] = False
    with pytest.raises(ConfigError) as e:
        parse_config(novod, environ={})
    assert any("must be an xtream provider with vod: true" in x for x in e.value.errors)

    fast = config(tmp_path)
    fast["providers"][0]["refresh"] = "10m"
    with pytest.raises(ConfigError):
        parse_config(fast, environ={})


# ------------------------------------------------------------------ live via xtream


async def test_live_list_cached_and_credentials_kept_out_of_locators(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    try:
        srcs = app.channel_map.channel_sources("f1-main")
        assert [s.label for s in srcs] == ["Sky Sports F1 [UK]"]
        assert srcs[0].locator == "live:5396" and USER not in srcs[0].locator
        opened = await app.registry.get("iptv").open("live:5396")
        assert opened.url_or_path == f"{PANEL}/live/{USER}/{PASS}/5396.ts"
        assert opened.headers == {"User-Agent": "VLC/3.0.20"}
        assert panel.calls["get_live_streams"] == 1
        # reload = new provider instance: served from the disk cache, no panel request
        await app.reload(None) if app.cfg.source_path else None
        from pitlane.providers.registry import ProviderRegistry

        reg = ProviderRegistry(app.cfg, app.http, app.data_dir)
        await reg.start()
        await reg.get("iptv").refresh()
        assert len(reg.get("iptv").entries) == 2 and panel.calls["get_live_streams"] == 1
        await reg.stop()
    finally:
        await app.shutdown()


# ------------------------------------------------------------------ limiter


async def test_download_lease_yields_to_streams_and_blocks_probes():
    lim = ConnectionLimiter({"iptv": 1})
    d = await lim.download_slot("iptv", 1)
    assert d is not None and d.kind == "download"
    started = asyncio.Event()

    async def downloading():
        started.set()
        await asyncio.sleep(30)

    t = asyncio.get_running_loop().create_task(downloading())
    d.task = t
    await started.wait()
    async with lim.probe_slot("iptv") as p:
        assert p is None  # a probe never waits behind a download
    t.add_done_callback(lambda _: d.release())
    s = await asyncio.wait_for(lim.acquire_stream("iptv", 5, wait_for_streams=False), 2)
    assert s is not None and t.cancelled()
    assert await lim.download_slot("iptv", 0.1) is None  # stream holds it
    s.release()
    assert await lim.download_slot("iptv", 0.1) is not None


# ------------------------------------------------------------------ catalogue


async def test_catalogue_refresh_search_and_budget(tmp_path, panel):
    from pitlane.vod import catalog

    app = await make_app(tmp_path, panel)
    try:
        stats = await app.vod.refresh_catalog()
        assert stats["movies"] == 5 and stats["series"] == 1
        assert sum(panel.calls[a] for a in ("get_vod_categories", "get_vod_streams", "get_series_categories", "get_series")) == 4
        assert await app.vod.refresh_catalog() is None  # not due: no requests
        assert sum(panel.calls.values()) == 6  # + the 2 live-list calls at startup
        hits = catalog.search(app.state, "ong ba", None, None, show_adult=False)
        assert [h["id"] for h in hits] == [101, 102]
        assert [h["id"] for h in catalog.search(app.state, "حق", None, None, False)] == [103]
        assert [h["kind"] for h in catalog.search(app.state, "mob", None, None, False)] == ["series"]
        assert 104 not in [h["id"] for h in catalog.search(app.state, "", "movie", None, False)]  # adult hidden
        vers = catalog.versions(app.state, 102, False)
        assert [v["id"] for v in vers] == [101, 102]  # non-CAM first
    finally:
        await app.shutdown()


# ------------------------------------------------------------------ downloads


async def test_movie_request_download_and_plex(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    notes = []
    try:
        app.notifier = type("N", (), {"notify": lambda self, t, m, **k: notes.append(t)})()
        await app.vod.refresh_catalog()
        assert await app.vod.request_movie(101)
        assert await app.vod.request_movie(101) is False  # already queued
        assert panel.calls["get_vod_info"] == 1
        await app.vod.movie(101)
        assert panel.calls["get_vod_info"] == 1  # cached (VOD-3)
        assert await app.vod.run_once(slot_timeout=1) == "done"
        target = tmp_path / "IPTV Movies" / "Ong Bak 3 (2010) {tmdb-43209}" / "Ong Bak 3 (2010) {tmdb-43209}.mkv"
        assert target.read_bytes() == MEDIA
        assert not list((tmp_path / "IPTV Movies").rglob("*.part"))
        assert app.connections.in_use("iptv") == 0
        for _ in range(100):  # the Plex step runs in the background after the download
            row = app.state.one("SELECT * FROM vod_items WHERE key='movie:101'")
            if row["match_state"] and notes:
                break
            await asyncio.sleep(0.05)
        assert row["status"] == "done" and row["match_state"] == "matched" and row["plex_rating_key"]
        assert app.fake_plex.scans == [(6, str(target.parent))]
        assert app.vod.usage()["day"] == len(MEDIA)
        assert notes == ["Ready to watch: Ong Bak 3 (2010)"]
        assert panel.downloads[0]["path"] == "/movie/…/101.mkv"
    finally:
        app.notifier = None
        await app.shutdown()


async def test_live_tune_preempts_download_which_resumes(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    try:
        await app.vod.refresh_catalog()
        await app.vod.request_movie(101)
        panel.mode, panel.chunk_delay = "slow", 0.05
        run = asyncio.get_running_loop().create_task(app.vod.run_once(slot_timeout=1))
        while not app.vod.current or (app.vod.current.get("done") or 0) < 8192:
            await asyncio.sleep(0.02)
            if run.done():
                break
        # a live tune takes the connection: the download is cancelled, its .part kept
        lease = await asyncio.wait_for(app.connections.acquire_stream("iptv", 5, wait_for_streams=False), 5)
        assert lease is not None
        assert await asyncio.wait_for(run, 5) == "preempted"
        row = app.state.one("SELECT * FROM vod_items WHERE key='movie:101'")
        assert row["status"] == "queued" and row["bytes_done"] > 0
        part = Path(row["target"] + ".part")
        assert part.exists() and part.stat().st_size == row["bytes_done"]
        assert await app.vod.run_once(slot_timeout=0.2) == "busy"  # still streaming
        lease.release()
        panel.mode = "ok"
        assert await app.vod.run_once(slot_timeout=1) == "done"
        assert panel.downloads[-1]["range"] == f"bytes={row['bytes_done']}-"
        assert Path(row["target"]).read_bytes() == MEDIA
        assert app.state.get("vod.range_supported") == "1"
    finally:
        await app.shutdown()


async def test_portal_page_pauses_the_queue(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    try:
        await app.vod.refresh_catalog()
        await app.vod.request_movie(101)
        panel.mode = "portal"
        assert await app.vod.run_once(slot_timeout=1) == "error"
        row = app.state.one("SELECT * FROM vod_items WHERE key='movie:101'")
        assert row["status"] == "queued" and row["attempts"] == 0  # not counted as a failure
        assert 'page title "Quota Notice"' in row["last_error"]
        assert "web page" in app.vod.gate()
        before = len(panel.downloads)
        app.vod._wake.set()
        assert await app.vod.run_once() == "blocked"
        assert len(panel.downloads) == before  # no request while paused
    finally:
        await app.shutdown()


async def test_gates(tmp_path, panel):
    app = await make_app(tmp_path, panel, max_gb_per_day=0.000001)
    try:
        assert app.vod.gate() is None
        app.vod._add_usage(2000)
        assert app.vod.gate() == "daily data cap reached"
    finally:
        await app.shutdown()
    (tmp_path / "b").mkdir()
    app = await make_app(tmp_path / "b", panel, download_window="01:00-02:00", min_free_space="1000TB")
    try:
        assert app.vod.gate().startswith("outside the download window")
        app.cfg.vod.download_window = None
        assert app.vod.gate({"kind": "movie", "total_size": None, "expected_size": 10, "bytes_done": 0}) == "not enough free disk space"
        app.cfg.vod.quiet_around_sessions = timedelta(minutes=30)
        app.clock = lambda: datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # Singapore GP race
        from pitlane.calendar.jolpica import Calendar, normalize_schedule
        from conftest import jolpica_fixture

        app.calendars = {"f1": Calendar(normalize_schedule(jolpica_fixture("2026-races.json")))}
        assert app.vod.gate() == "an F1 session is on or about to start"
    finally:
        await app.shutdown()


async def test_failed_download_retries_then_fails(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    try:
        await app.vod.refresh_catalog()
        await app.vod.request_movie(101)
        panel.mode = "missing"  # the panel now 404s the file
        for attempt in range(3):
            app.vod._retry_after.clear()
            assert await app.vod.run_once(slot_timeout=1) == "error"
        row = app.state.one("SELECT * FROM vod_items WHERE key='movie:101'")
        assert row["status"] == "failed" and row["attempts"] == 3 and "HTTP 404" in row["last_error"]
        app.vod.item_action("movie:101", "retry")
        assert app.state.one("SELECT status FROM vod_items WHERE key='movie:101'")["status"] == "queued"
    finally:
        await app.shutdown()


# ------------------------------------------------------------------ series and follows


async def test_series_request_follow_and_naming(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    try:
        await app.vod.refresh_catalog()
        s = await app.vod.series(501)
        assert list(s["seasons"]) == [1, 2] and s["seasons"][1][0]["size"] == int(1632 * 1000 / 8 * 3540)
        assert await app.vod.request_episodes(501, [e["id"] for e in s["seasons"][1]]) == 2
        app.vod.set_series_tmdb(501, "247718")
        assert await app.vod.request_episodes(501, ["9101"]) == 1
        targets = sorted(r["target"] for r in app.state.query("SELECT target FROM vod_items"))
        root = tmp_path / "IPTV Shows"
        assert targets == [
            str(root / "MobLand (2025) {tmdb-247718}" / "Season 02" / "MobLand (2025) - S02E01.mkv"),
            str(root / "MobLand (2025)" / "Season 01" / "MobLand (2025) - S01E01.mkv"),
            str(root / "MobLand (2025)" / "Season 01" / "MobLand (2025) - S01E02.mkv"),
        ]
        info_calls = panel.calls["get_series_info"]
        await app.vod.follow(501, from_season=2)
        # unchanged series: the next catalogue refresh costs no details request
        await app.vod.refresh_catalog(force=True)
        assert panel.calls["get_series_info"] == info_calls
        # a new episode appears: exactly one details request, and it is queued
        panel.add_episode(9102, 2, 2, added=int(datetime.now(UTC).timestamp()) + 60, last_modified=1800000000)
        stats = await app.vod.refresh_catalog(force=True)
        assert stats["changed_series"] == [501]
        assert panel.calls["get_series_info"] == info_calls + 1
        assert app.state.one("SELECT status FROM vod_items WHERE key='ep:9102'")["status"] == "queued"
    finally:
        await app.shutdown()


# ------------------------------------------------------------------ retention


async def test_retention_after_watched(tmp_path, panel):
    app = await make_app(tmp_path, panel, delete_after_watched="1d")
    try:
        await app.vod.refresh_catalog()
        await app.vod.request_movie(101)
        await app.vod.run_once(slot_timeout=1)
        for _ in range(100):
            row = app.state.one("SELECT * FROM vod_items WHERE key='movie:101'")
            if row["plex_rating_key"]:
                break
            await asyncio.sleep(0.05)
        app.fake_plex.views[row["plex_rating_key"]] = 1
        await app.vod.retention_once()
        row = app.state.one("SELECT * FROM vod_items WHERE key='movie:101'")
        assert row["watched_at"]
        app.state.execute("UPDATE vod_items SET watched_at='2026-01-01T00:00:00+00:00' WHERE key='movie:101'")
        acted = await app.vod.retention_once()
        assert acted[0]["dry_run"] and Path(row["target"]).exists()  # dry-run by default
        app.cfg.vod.dry_run_retention = False
        await app.vod.retention_once()
        assert not Path(row["target"]).exists() and not Path(row["target"]).parent.exists()
        assert (tmp_path / "IPTV Movies").exists()
        assert app.state.one("SELECT status FROM vod_items WHERE key='movie:101'")["status"] == "deleted"
    finally:
        await app.shutdown()


# ------------------------------------------------------------------ pages


async def test_pages_render_without_credentials(tmp_path, panel):
    from pitlane.app import create_app

    app = await make_app(tmp_path, panel)
    try:
        await app.vod.refresh_catalog()
        api = create_app(app_obj=app, install_signal_handlers=False)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1:5998")
        pages = {}
        for path in ("/vod", "/vod?q=ong", "/vod/movie/101", "/vod/series/501", "/vod/downloads", "/"):
            resp = await client.get(path)
            assert resp.status_code == 200, path
            pages[path] = resp.text
        assert "Ong Bak 3" in pages["/vod?q=ong"] and "img.example.invalid/ob3.jpg" in pages["/vod?q=ong"]
        assert f"{PANEL}/images" not in pages["/vod?q=ong"]  # no artwork from the panel host
        assert "≈ 805.2 MB" in pages["/vod/movie/101"] and "data-vod-item='movie:101'" in pages["/vod/movie/101"]
        assert "Season 2" in pages["/vod/series/501"]
        assert "Movies &amp; Shows" in pages["/"]
        resp = await client.post("/vod/request/movie", data={"id": "101"})
        assert resp.status_code == 303
        resp = await client.post("/vod/request/movie", data={"id": "101"}, headers={"Accept": "application/json"})
        assert resp.json() == {"ok": True, "msg": "Already in the queue"}
        st = (await client.get("/api/state")).json()["vod"]
        assert [i["key"] for i in st["items"]] == ["movie:101"] and st["items"][0]["status"] == "queued"
        assert st["free"]["movie"] and st["queued"] == 1
        resp = await client.post("/vod/request/episodes", data={"id": "501", "season": "1"})
        assert resp.status_code == 303 and "2%20episode" in resp.headers["location"]
        resp = await client.post("/vod/item", data={"key": "movie:101", "action": "pause"})
        assert app.state.one("SELECT status FROM vod_items WHERE key='movie:101'")["status"] == "paused"
        dl = (await client.get("/vod/downloads")).text + (await client.get("/api/state")).text
        for text in [*pages.values(), dl]:
            assert USER not in text and PASS not in text
        await client.aclose()
    finally:
        await app.shutdown()


# ------------------------------------------------------------------ play now


async def _serve(app):
    import socket

    import uvicorn

    from pitlane.app import create_app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(app_obj=app, install_signal_handlers=False), host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    task = asyncio.get_running_loop().create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    return server, task, f"http://127.0.0.1:{port}"


def _ts(data: bytes) -> bool:
    off = data.find(b"\x47")
    return off >= 0 and all(data[off + k * 188] == 0x47 for k in range(min(20, (len(data) - off) // 188)))


@needs_ffmpeg
async def test_play_now_streams_once_keeps_copy_and_ends(tmp_path, panel):
    app = await make_app(tmp_path, panel, play_channel={"save": True})
    server, task, base = await _serve(app)
    try:
        await app.vod.refresh_catalog()
        lineup = (await httpx.AsyncClient().get(f"{base}/lineup.json")).json()
        assert {"GuideNumber": "900", "GuideName": "Pitlane Cinema"}.items() <= next(c for c in lineup if c["GuideNumber"] == "900").items()
        async with httpx.AsyncClient(timeout=None) as c:
            assert (await c.get(f"{base}/auto/v900", timeout=10)).status_code == 503  # nothing picked yet
            await app.vod.play_movie(101)
            before = len(panel.downloads)
            data = b""
            async with c.stream("GET", f"{base}/auto/v900") as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_raw():
                    data += chunk  # the stream ends when the title ends (no restart)
        assert _ts(data) and len(data) > 10_000
        assert len(panel.downloads) == before + 1 and panel.downloads[-1]["range"] is None  # one connection
        target = tmp_path / "IPTV Movies" / "Ong Bak 3 (2010) {tmdb-43209}" / "Ong Bak 3 (2010) {tmdb-43209}.mkv"
        assert target.read_bytes() == MEDIA  # watched to the end: kept, byte-identical
        assert app.state.one("SELECT status FROM vod_items WHERE key='movie:101'")["status"] == "done"
        # playing it again comes from the library: no panel request
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", f"{base}/auto/v900") as resp:
                first = await resp.aiter_raw().__anext__()
        assert _ts(first) and len(panel.downloads) == before + 1
    finally:
        server.should_exit = True
        await task
        await app.shutdown()


@needs_ffmpeg
async def test_play_now_switches_title_and_keeps_partial_copy(tmp_path, panel):
    app = await make_app(tmp_path, panel, play_channel={"save": True})
    server, task, base = await _serve(app)
    try:
        await app.vod.refresh_catalog()
        await app.vod.request_movie(105)  # a queued download
        panel.mode, panel.chunk_delay = "slow", 0.05
        dl = asyncio.get_running_loop().create_task(app.vod.run_once(slot_timeout=1))
        while not app.vod.current or not app.vod.current.get("done"):
            await asyncio.sleep(0.02)
        await app.vod.play_movie(101)
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", f"{base}/auto/v900") as resp:
                assert resp.status_code == 200
                assert await asyncio.wait_for(dl, 5) == "preempted"  # live TV first, the download waits
                it = resp.aiter_raw()
                got = await it.__anext__()
                sel = await app.vod.play_movie(105)  # pick another title while watching
                assert sel["switched"] == 1
                for _ in range(200):
                    got += await it.__anext__()
                    if any(d["path"].endswith("/105.mkv") and d["range"] is None for d in panel.downloads[1:]):
                        break
                assert _ts(got)
        await asyncio.sleep(0.3)
        assert [d["path"] for d in panel.downloads][-2:] == ["/movie/…/101.mkv", "/movie/…/105.mkv"]
        row = app.state.one("SELECT * FROM vod_items WHERE key='movie:101'")
        assert row["status"] == "paused" and row["bytes_done"] > 0 and "partly saved" in row["last_error"]
        assert Path(row["target"] + ".part").stat().st_size == row["bytes_done"]
        assert app.connections.in_use("iptv") == 0
    finally:
        server.should_exit = True
        await task
        await app.shutdown()


async def test_unscanned_download_is_scanned_after_restart(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    try:
        await app.vod.refresh_catalog()
        await app.vod.request_movie(101)
        target = Path(app.state.one("SELECT target FROM vod_items")["target"])
        target.parent.mkdir(parents=True)
        target.write_bytes(MEDIA)
        app.state.execute("UPDATE vod_items SET status='done'")  # finished, but the Plex step never ran
        await app.vod.stop()
        await app.vod.start(background=True)
        for _ in range(100):
            if app.state.one("SELECT match_state FROM vod_items")["match_state"]:
                break
            await asyncio.sleep(0.05)
        assert app.state.one("SELECT match_state FROM vod_items")["match_state"] == "matched"
        assert app.fake_plex.scans == [(6, str(target.parent))]
    finally:
        await app.shutdown()


async def test_new_library_gets_a_full_scan_when_folder_scan_misses(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    try:
        app.fake_plex.ignore_folder_scans = True
        await app.vod.refresh_catalog()
        await app.vod.request_movie(101)
        target = Path(app.state.one("SELECT target FROM vod_items")["target"])
        target.parent.mkdir(parents=True)
        target.write_bytes(MEDIA)
        app.state.execute("UPDATE vod_items SET status='done'")
        import pitlane.vod.manager as mm

        orig = mm.PlexClient.wait_for_file

        async def quick(self, *a, timeout=300.0, **k):
            return await orig(self, *a, timeout=min(timeout, 1.0), first_delay=0.1, **k)

        mm.PlexClient.wait_for_file = quick
        try:
            await app.vod._after_download("movie:101", timeout=3)
        finally:
            mm.PlexClient.wait_for_file = orig
        assert app.fake_plex.scans == [(6, str(target.parent)), (6, "")]
        assert app.state.one("SELECT match_state FROM vod_items")["match_state"] == "matched"
    finally:
        await app.shutdown()


async def test_limiter_hold():
    lim = ConnectionLimiter({"iptv": 1})
    lim.hold("iptv", None)
    assert await lim.acquire_stream("iptv", 1, wait_for_streams=True) is None
    assert await lim.download_slot("iptv", 0.1) is None
    async with lim.probe_slot("iptv") as p:
        assert p is None
    assert lim.held_until("iptv") == float("inf")
    lim.release_hold("iptv")
    assert (await lim.acquire_stream("iptv", 1, wait_for_streams=False)) is not None
    lim2 = ConnectionLimiter({"iptv": 1})
    lim2.hold("iptv", -1)  # already over
    assert lim2.held_until("iptv") is None


@needs_ffmpeg
async def test_free_connection_stops_stream_and_download(tmp_path, panel):
    app = await make_app(tmp_path, panel)
    server, task, base = await _serve(app)
    try:
        await app.vod.refresh_catalog()
        await app.vod.request_movie(105)
        panel.mode, panel.chunk_delay = "slow", 0.05
        dl = asyncio.get_running_loop().create_task(app.vod.run_once(slot_timeout=1))
        while not app.vod.current or not app.vod.current.get("done"):
            await asyncio.sleep(0.02)
        res = await app.free_provider("iptv", None)
        assert res == {"streams": 0, "downloads": 1}
        assert await asyncio.wait_for(dl, 5) == "preempted"
        assert await app.vod.run_once() == "blocked" and "freed for you until you resume" in app.vod.blocked
        async with httpx.AsyncClient(timeout=10) as c:
            assert (await c.get(f"{base}/auto/v900")).status_code == 503  # nothing opens while held
            st = (await c.get(f"{base}/api/state")).json()
            assert st["connections"]["iptv"]["held"] == -1
        app.resume_provider("iptv")
        # a running stream is ended by a hold
        await app.vod.play_movie(101)
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", f"{base}/auto/v900") as resp:
                it = resp.aiter_raw()
                await it.__anext__()
                res = await app.free_provider("iptv", 3600)
                assert res["streams"] == 1
                rest = b"".join([b async for b in it])  # the response ends
        assert app.connections.held_until("iptv") is not None
        for _ in range(50):
            if app.connections.in_use("iptv") == 0:
                break
            await asyncio.sleep(0.05)
        assert app.connections.in_use("iptv") == 0 and isinstance(rest, bytes)
    finally:
        server.should_exit = True
        await task
        await app.shutdown()


@needs_ffmpeg
async def test_play_now_copies_nothing_by_default_and_is_paced(tmp_path, panel):
    import time

    app = await make_app(tmp_path, panel)
    server, task, base = await _serve(app)
    try:
        await app.vod.refresh_catalog()
        async with httpx.AsyncClient(timeout=10) as c:
            assert (await c.get(f"{base}/auto/v900")).status_code == 503  # nothing picked
        assert all(s.state == "unknown" for s in app.channel_map.channel_sources("pitlane-cinema"))  # no stale error
        await app.vod.play_movie(101)
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", f"{base}/auto/v900") as resp:
                data = b"".join([b async for b in resp.aiter_raw()])
        assert _ts(data)
        assert app.state.query("SELECT * FROM vod_items") == []  # no copy, no queue item
        assert not list((tmp_path / "IPTV Movies").rglob("*"))
        assert time.monotonic() - t0 < 10  # a 2 s title, inside its 3 s start-up burst
    finally:
        server.should_exit = True
        await task
        await app.shutdown()


def test_play_pipe_is_paced():
    from pitlane.vod.play import pipe_args

    args = pipe_args()
    assert args[args.index("-readrate") + 1] == "1" and args.index("-readrate") < args.index("-i")
