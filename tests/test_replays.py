"""REP-1 … REP-8 tests, incl. acceptance criteria 6 (replay filing) and 7 (spoilers)."""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from conftest import FIXTURES, assert_golden, needs_ffmpeg
from fake_pms import FakePMS
from pitlane.app import App
from pitlane.config import parse_config
from pitlane.models import SessionType
from pitlane.replays.matcher import Match, Matcher, NoMatch, descriptive_title
from pitlane.replays.naming import episode_filename, sanitize
from pitlane.replays.parsing import detect_session, normalize
from pitlane.replays.pipeline import is_ignored
from pitlane.replays.spoilers import results_text, safe_summary

TOKEN = "fake-plex-owner-token"
SPOILER_TOKEN = "fake-spoiler-user-token"
NAMES = ["Alex", "Rivera", "Bea", "Kowalski", "Chen", "Dubois", "Northwind", "Helix", "Vantage", "Okafor"]


class RoutingTransport(httpx.AsyncBaseTransport):
    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]):
        self.routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        t = self.routes.get(request.url.host)
        if t is None:
            raise httpx.ConnectError(f"no route to {request.url.host} (tests never hit live APIs)")
        return await t.handle_async_request(request)


def jolpica_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path  # /ergast/f1/2026/races/ or /ergast/f1/2026/17/results/
    parts = [x for x in p.split("/") if x][2:]
    name = "-".join(parts) + ".json"
    f = FIXTURES / "jolpica" / name
    if not f.exists():
        return httpx.Response(404)
    return httpx.Response(200, content=f.read_bytes(), headers={"content-type": "application/json"})


def write_media(path: Path, size: int = 1024, content: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if content is not None else os.urandom(size))
    return path


@pytest.fixture
def cal_cache(tmp_path):
    d = tmp_path / "data" / "jolpica"
    d.mkdir(parents=True)
    for f in ("2026-races.json", "2027-races.json"):
        (d / f).write_bytes((FIXTURES / "jolpica" / f).read_bytes())
    return d


def make_cfg(tmp_path, **over):
    tokfile = tmp_path / "spoiler_token"
    tokfile.write_text(SPOILER_TOKEN)
    raw = {
        "server": {"data_dir": str(tmp_path / "data"), "advertised_url": "http://pitlane.invalid:5004"},
        "channels": [{"id": "f1-main", "guide_number": 101, "name": "F1 Main Feed", "series": ["f1"]}],
        "series": {"f1": {"title": "Formula 1", "seasons": [2026]}},
        "replays": {
            "inboxes": [str(tmp_path / "dvr"), str(tmp_path / "inbox")],
            "library_root": str(tmp_path / "library"),
            "needs_review_dir": str(tmp_path / "inbox" / "needs-review"),
            "settle_time": "0s",
            **over.pop("replays", {}),
        },
        "plex": {"url": "http://pms.invalid:32400", "token": "${PLEX}", "library_section_id": 7,
                 "spoiler_user": "kid", "spoiler_token": f"${{file:{tokfile}}}"},  # fmt: skip
        **over,
    }
    return parse_config(raw, environ={"PLEX": TOKEN})


async def no_tags(path):
    return {}


@pytest.fixture
async def env(tmp_path, cal_cache):
    pms = FakePMS(TOKEN)
    transport = RoutingTransport(
        {"pms.invalid": httpx.ASGITransport(app=pms.app), "api.jolpi.ca": httpx.MockTransport(jolpica_handler)}
    )

    async def build(**over):
        cfg = make_cfg(tmp_path, **over)
        app = App(cfg, http_transport=transport, start_background=False, clock=lambda: datetime(2026, 10, 12, tzinfo=UTC))
        await app.startup()
        app.replays.probe = no_tags
        app.replays.matcher.probe = no_tags
        app.replays.plex_poll_timeout = 2
        return app

    apps = []

    async def factory(**over):
        a = await build(**over)
        apps.append(a)
        return a

    factory.pms = pms  # type: ignore[attr-defined]
    factory.tmp = tmp_path  # type: ignore[attr-defined]
    yield factory
    for a in apps:
        await a.shutdown()


def tree(root: Path) -> bytes:
    lines = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))
    return ("\n".join(lines) + "\n").encode()


# ------------------------------------------------------------------ unit


def test_session_keywords():
    assert detect_session(normalize("British GP FP1")) == SessionType.FP1
    assert detect_session(normalize("Practice Two")) == SessionType.FP2
    assert detect_session(normalize("Sprint Shootout")) == SessionType.SQ
    assert detect_session(normalize("Sprint Race")) == SessionType.SPRINT
    assert detect_session(normalize("Qualifying")) == SessionType.QUALI
    assert detect_session(normalize("Grand Prix")) == SessionType.RACE
    assert detect_session(normalize("Pre-Race Build-Up")) == SessionType.SUPP  # QA-006
    assert detect_session(normalize("Race Highlights")) == SessionType.SUPP
    assert detect_session(normalize("fp1 and fp2")) is None
    assert detect_session(normalize("holiday")) is None


def test_naming():
    assert sanitize('a/b\\c:d*e?f"g<h>i|j   k') == "abcdefghij k"
    assert (
        episode_filename("Formula 1", 2026, 105, "Australian Grand Prix", "Race", ".MKV")
        == "Formula 1 - S2026E0105 - Australian Grand Prix - Race.mkv"
    )


def test_ignored_files(tmp_path):
    assert is_ignored(tmp_path / ".grab" / "x.ts", tmp_path)
    assert is_ignored(tmp_path / ".hidden.ts", tmp_path)
    assert is_ignored(tmp_path / "x.ts.part", tmp_path)
    assert is_ignored(tmp_path / "x.tmp", tmp_path)
    assert not is_ignored(tmp_path / "sub" / "x.ts", tmp_path)


def test_descriptive_title(calendar_2026):
    ev = calendar_2026.event("2026-12")
    assert descriptive_title("F1.2026.British.Grand.Prix.Pre-Race.Build-Up.1080p", ev, "Formula 1") == "Pre-Race Build-Up"


def test_spoiler_texts(calendar_2026):
    import json

    ev = calendar_2026.event("2026-17")
    s = ev.session(SessionType.RACE)
    assert safe_summary(ev, s) == "Round 17 · Marina Bay Street Circuit, Singapore · Sunday 11 October 2026"
    doc = json.loads((FIXTURES / "jolpica" / "2026-17-results.json").read_text())
    assert results_text(SessionType.RACE, doc).startswith("Results: 1. Alex Rivera (Northwind Racing), 2. Bea Kowalski")
    q = json.loads((FIXTURES / "jolpica" / "2026-17-qualifying.json").read_text())
    assert results_text(SessionType.QUALI, q).startswith("Pole: Alex Rivera (Northwind Racing). Results:")
    assert results_text(SessionType.FP1, doc) is None


async def test_matcher_steps(calendar_2026, tmp_path):
    from pitlane.xmltv import session_window

    cfg = parse_config({}, environ={})
    tags: dict[str, dict] = {}

    async def probe(p):
        return tags.get(p.name, {})

    m = Matcher(lambda: calendar_2026, lambda s: session_window(s, cfg), probe=probe)
    # (a) episode in filename (Plex DVR naming)
    r = await m.match(write_media(tmp_path / "Formula 1 - S2026E1705 - Singapore Grand Prix – Race.ts"))
    assert isinstance(r, Match) and r.session.id == "2026-17-RACE" and r.step == "episode"
    # (a) episode in container tags
    tags["tagged.mkv"] = {"title": "Formula 1 - S2026E1704 - Singapore Grand Prix"}
    r = await m.match(write_media(tmp_path / "tagged.mkv"))
    assert isinstance(r, Match) and r.session.id == "2026-17-QUALI"
    # supplementary episode slot keeps its number and descriptive title
    r = await m.match(write_media(tmp_path / "Formula 1 - S2026E1711 - Singapore Grand Prix – Pre-Race Build-Up.ts"))
    assert r.session_type == SessionType.SUPP and r.episode_no == 1711 and r.title == "Pre-Race Build-Up"
    # (b) filename parsing with fuzzy circuit/locality
    r = await m.match(write_media(tmp_path / "F1.2026.Silverstone.Qualifying.1080p.mkv"))
    assert isinstance(r, Match) and r.session.id == "2026-12-QUALI" and r.step == "filename"
    r = await m.match(write_media(tmp_path / "f1 2026x18 sprint.mkv"))
    assert r.session.id == "2026-18-SPRINT"
    # (b) asks for a session the weekend doesn't have → falls to (c), then needs-review
    p = write_media(tmp_path / "2026 Singapore Grand Prix sprint.mkv")
    os.utime(p, (0, 0))
    r = await m.match(p)
    assert isinstance(r, NoMatch) and any("no SPRINT" in c for c in r.candidates)
    # (c) time: creation_time inside a padded window
    tags["rec.ts"] = {"creation_time": "2026-10-10T12:55:00.000000Z"}
    r = await m.match(write_media(tmp_path / "rec.ts"))
    assert isinstance(r, Match) and r.session.id == "2026-17-QUALI" and r.step == "time:creation_time"
    # nothing → no guess
    p = write_media(tmp_path / "holiday video.mp4")
    os.utime(p, (0, 0))
    r = await m.match(p)
    assert isinstance(r, NoMatch)


# ------------------------------------------------------------------ pipeline (acceptance 6)


async def test_replay_filing_acceptance(env):
    app = await env()
    tmp, pms = env.tmp, env.pms
    dvr_file = write_media(tmp / "dvr" / "Formula 1" / "Season 2026" / "Formula 1 - S2026E1705 - Singapore Grand Prix – Race.ts")
    name_file = write_media(tmp / "inbox" / "F1.2026.Silverstone.Qualifying.1080p.mkv")
    supp_file = write_media(tmp / "inbox" / "F1 2026 British Grand Prix Pre-Race Build-Up.mkv")
    unmatched = write_media(tmp / "inbox" / "holiday video.mp4")
    os.utime(unmatched, (0, 0))

    results = {p.name: await app.replays.process_file(p) for p in (dvr_file, name_file, supp_file, unmatched)}
    assert results[dvr_file.name]["outcome"] == "filed" and results[dvr_file.name]["step"] == "episode"
    assert results[name_file.name]["outcome"] == "filed" and results[name_file.name]["step"] == "filename"
    assert results[supp_file.name]["episode_no"] == 1211
    assert results[unmatched.name]["outcome"] == "needs_review"
    assert (tmp / "inbox" / "needs-review" / "holiday video.mp4").exists()
    assert app.state.one("SELECT * FROM review_items")["reason"] in ("no match", "no unique match")

    lib = tmp / "library"
    assert_golden("library-tree.txt", tree(lib))
    snapshot = {p: p.stat().st_mtime_ns for p in lib.rglob("*")}

    # Re-running the job changes nothing.
    for p in list((tmp / "dvr").rglob("*.ts")) + list((tmp / "inbox").glob("*.*")):
        await app.replays.process_file(p)
    assert {p: p.stat().st_mtime_ns for p in lib.rglob("*")} == snapshot
    assert app.replays.scan_once and not await app.replays.scan_once()

    # Plex: targeted scans of the exact folders, then metadata with lock flags.
    scans = [c["params"]["path"] for c in pms.calls if c["path"].endswith("/refresh")]
    assert scans == [str(lib / "Formula 1" / "Season 2026")] * 3
    assert all(c["token"] == TOKEN for c in pms.calls)
    race = next(e for e in pms.episodes.values() if e.index == 1705)
    assert race.title == "Singapore Grand Prix – Race"
    assert race.summary == "Round 17 · Marina Bay Street Circuit, Singapore · Sunday 11 October 2026"
    assert {"title", "summary", "originallyAvailableAt"} <= race.locked
    # the title card is uploaded as image bytes (PMS 1.43.5 refuses posters by local URL)
    assert len(race.posters) == 1 and race.posters[0].startswith("png:")
    assert pms.titles.get(FakePMS.SHOW_KEY) == "Formula 1" and pms.titles.get("22026") == "2026 Season"


async def test_conflict_goes_to_review(env):
    app = await env()
    tmp = env.tmp
    a = write_media(tmp / "inbox" / "a" / "Formula 1 - S2026E1701 - Singapore Grand Prix - Practice 1.ts")
    b = write_media(tmp / "inbox" / "b" / "Formula 1 - S2026E1701 - Singapore Grand Prix - Practice 1.ts")
    assert (await app.replays.process_file(a))["outcome"] == "filed"
    r = await app.replays.process_file(b)
    assert r["outcome"] == "needs_review" and r["reason"] == "conflict"
    target = tmp / "library" / "Formula 1" / "Season 2026" / "Formula 1 - S2026E1701 - Singapore Grand Prix - Practice 1.ts"
    assert target.read_bytes() != Path(r["path"]).read_bytes()  # never overwritten


async def test_same_content_already_filed_move_deletes_source(env):
    app = await env()
    tmp = env.tmp
    data = os.urandom(2048)
    a = write_media(tmp / "inbox" / "a" / "Formula 1 - S2026E1702 - x.ts", content=data)
    assert (await app.replays.process_file(a))["outcome"] == "filed"
    b = write_media(tmp / "inbox" / "b" / "Formula 1 - S2026E1702 - x.ts", content=data)
    assert (await app.replays.process_file(b))["outcome"] == "already_filed"
    assert not b.exists()


async def test_hardlink_mode_and_extras(env):
    app = await env(replays={"file_mode": "hardlink", "supplementary_mode": "extras"})
    tmp = env.tmp
    src = write_media(tmp / "dvr" / "Formula 1 - S2026E1703 - Singapore Grand Prix - Practice 3.ts")
    r = await app.replays.process_file(src)
    assert r["outcome"] == "filed" and src.exists()
    assert os.stat(src).st_ino == os.stat(r["target"]).st_ino
    supp = write_media(tmp / "inbox" / "2026 Singapore Grand Prix Post Race Analysis.mkv")
    r = await app.replays.process_file(supp)
    assert r["target"].endswith("Season 2026/Extras/Singapore Grand Prix - Post Race Analysis.mkv")
    # hardlink source stays, but the scanner doesn't re-process it
    assert src not in await app.replays.scan_once()


async def test_manual_assignment_is_saved_by_hash(env):
    app = await env()
    tmp = env.tmp
    data = os.urandom(4096)
    p = write_media(tmp / "inbox" / "mystery.ts", content=data)
    os.utime(p, (0, 0))
    r = await app.replays.process_file(p)
    assert r["outcome"] == "needs_review"
    r2 = await app.replays.assign(r["path"], "2026-17-FP2")
    assert r2["outcome"] == "filed" and "S2026E1702" in r2["target"]
    # same content dropped again later, under a different name → saved assignment wins
    Path(r2["target"]).unlink()
    app.state.execute("DELETE FROM library_items")
    p2 = write_media(tmp / "inbox" / "again.ts", content=data)
    os.utime(p2, (0, 0))
    r3 = await app.replays.process_file(p2)
    assert r3["outcome"] == "filed" and r3["step"] == "assignment"


async def test_watcher_settle_and_min_size(env):
    app = await env(replays={"settle_time": "30s"})
    tmp = env.tmp
    big = write_media(tmp / "inbox" / "Formula 1 - S2026E1701 - x.ts", size=6 * 1024 * 1024)
    write_media(tmp / "inbox" / "Formula 1 - S2026E1702 - small.ts", size=1024)
    write_media(tmp / "dvr" / ".grab" / "Formula 1 - S2026E1703 - x.ts", size=6 * 1024 * 1024)
    write_media(tmp / "inbox" / "Formula 1 - S2026E1704 - x.ts.part", size=6 * 1024 * 1024)
    assert await app.replays.scan_once(now=1000) == []
    assert await app.replays.scan_once(now=1029) == []
    assert await app.replays.scan_once(now=1031) == [big]
    with open(big, "ab") as f:  # still growing → timer restarts
        f.write(b"x")
    assert await app.replays.scan_once(now=1040) == []
    assert await app.replays.scan_once(now=1071) == [big]


# ------------------------------------------------------------------ spoilers (acceptance 7)


async def test_spoiler_mode(env):
    app = await env()
    pms = env.pms
    tmp = env.tmp
    race = write_media(tmp / "dvr" / "Formula 1 - S2026E1705 - Singapore Grand Prix – Race.ts")
    quali = write_media(tmp / "dvr" / "Formula 1 - S2026E1704 - Singapore Grand Prix – Qualifying.ts")
    await app.replays.process_file(race)
    await app.replays.process_file(quali)

    def leaked():
        return [s for s in pms.written_strings() if any(n in s for n in NAMES)]

    assert leaked() == []
    assert await app.replays.spoiler_sync_once() == 0
    assert leaked() == []

    race_ep = next(e for e in pms.episodes.values() if e.index == 1705)
    # Watched by the owner only → still hidden for the spoiler user.
    race_ep.view_count = 1
    pms.user_views[SPOILER_TOKEN] = {}
    assert await app.replays.spoiler_sync_once() == 0
    assert leaked() == []

    pms.user_views[SPOILER_TOKEN] = {race_ep.rating_key: 1}
    assert await app.replays.spoiler_sync_once() == 1
    assert "1. Alex Rivera (Northwind Racing)" in race_ep.summary
    assert race_ep.title == "Singapore Grand Prix – Race"  # never changes the title
    quali_ep = next(e for e in pms.episodes.values() if e.index == 1704)
    assert not any(n in quali_ep.summary for n in NAMES)
    # spoiler-user reads used the spoiler token
    assert any(c["token"] == SPOILER_TOKEN for c in pms.calls if c["method"] == "GET" and "/metadata/" in c["path"])
    assert await app.replays.spoiler_sync_once() == 0  # idempotent



async def test_spoiler_sync_handles_items_gone_from_plex(env, caplog):
    """A 404 for a library item is resolved once, not warned about every round."""
    app = await env()
    pms = env.pms
    tmp = env.tmp
    pms.user_views[SPOILER_TOKEN] = {}
    for n, t in [(1705, "Race"), (1704, "Qualifying")]:
        await app.replays.process_file(write_media(tmp / "dvr" / f"Formula 1 - S2026E{n} - Singapore Grand Prix – {t}.ts"))
    rows = {r["episode_no"]: r for r in app.state.query("SELECT * FROM library_items")}
    by_index = {e.index: e for e in pms.episodes.values()}

    def metadata_gets() -> list[str]:
        return [c["path"] for c in pms.calls if c["method"] == "GET" and "/metadata/" in c["path"]]

    # Race: file deleted and the item removed from Plex → row dropped.
    Path(rows[1705]["path"]).unlink()
    del pms.episodes[by_index[1705].rating_key]
    # Qualifying: Plex re-added the same file under a new key → row follows it.
    ep = pms.episodes.pop(by_index[1704].rating_key)
    ep.rating_key = "9999"
    pms.episodes["9999"] = ep

    assert await app.replays.spoiler_sync_once() == 0
    after = {r["episode_no"]: r for r in app.state.query("SELECT * FROM library_items")}
    assert set(after) == {1704}
    assert after[1704]["plex_rating_key"] == "9999" and after[1704]["spoiler_state"] == "hidden"

    seen = len(metadata_gets())
    assert await app.replays.spoiler_sync_once() == 0
    assert metadata_gets()[seen:] == ["/library/metadata/9999"]  # only the re-linked item is polled

    # Item gone from Plex but the file is still there → key cleared, polling stops.
    del pms.episodes["9999"]
    assert await app.replays.spoiler_sync_once() == 0
    assert app.state.one("SELECT plex_rating_key FROM library_items")["plex_rating_key"] is None
    seen = len(metadata_gets())
    assert await app.replays.spoiler_sync_once() == 0
    assert metadata_gets()[seen:] == []
    assert "viewCount read failed" not in caplog.text

async def test_spoiler_disabled_writes_results(env):
    app = await env(spoiler={"enabled": False})
    tmp = env.tmp
    await app.replays.process_file(write_media(tmp / "dvr" / "Formula 1 - S2026E1705 - Singapore Grand Prix - Race.ts"))
    ep = next(e for e in env.pms.episodes.values() if e.index == 1705)
    assert "Alex Rivera" in ep.summary


# ------------------------------------------------------------------ downloads (REP-4), retention (REP-8)


@needs_ffmpeg
async def test_directory_provider_downloads(env, tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    src = drop / "F1 2026 Singapore Grand Prix Qualifying.ts"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10", "-t", "1",
         "-c:v", "mpeg2video", "-f", "mpegts", str(src)],
        check=True,
    )  # fmt: skip
    app = await env(providers=[{"type": "directory", "name": "drop", "path": str(drop)}])
    now = datetime(2026, 10, 11, 20, tzinfo=UTC)
    assert await app.replays.poll_providers_once(now) == 1
    assert await app.replays.poll_providers_once(now) == 0  # de-duplicated
    await app.replays.run_downloads_once()
    job = app.state.one("SELECT * FROM replay_jobs")
    assert job["status"] == "done", job
    out = env.tmp / "dvr" / "Formula 1 - S2026E1704 - Singapore Grand Prix - Qualifying.ts"
    assert out.exists() and not list((env.tmp / "dvr" / ".dl").iterdir())
    r = await app.replays.process_file(out)
    assert r["outcome"] == "filed" and r["step"] == "episode"
    # A library item for the same session now exists → not downloaded again.
    app.state.execute("DELETE FROM replay_jobs")
    assert await app.replays.poll_providers_once(now) == 0


async def test_retention_dry_run_and_delete(env):
    app = await env(retention={"enabled": True, "dry_run": True})
    app.state.execute(
        "INSERT INTO library_items(path, session_id, season, episode_no, session_type, sha256, plex_rating_key) VALUES"
        "('/l/a', '2025-1-FP1', 2025, 101, 'FP1', 'x', '55'), ('/l/b', '2025-1-RACE', 2025, 105, 'RACE', 'y', '56'),"
        "('/l/c', '2025-1-SQ', 2025, 102, 'SQ', 'z', '57')"
    )
    doomed = await app.replays.retention_once(current_season=2026)
    assert [d["path"] for d in doomed] == ["/l/a"]
    assert env.pms.deleted == []
    app.cfg.retention.dry_run = False
    await app.replays.retention_once(current_season=2026)
    assert env.pms.deleted == ["55"]


async def test_identical_content_for_different_session_goes_to_review(env, caplog):
    """REP-3 regression: a correctly parsed qualifying file that is byte-identical to an
    already filed race must not be deleted; it goes to needs-review as a duplicate, and
    logs name the episode it matched (not the existing item's)."""
    import logging

    app = await env()
    tmp = env.tmp
    data = os.urandom(4096)
    race = write_media(tmp / "dvr" / "Formula 1 - S2026E1705 - Singapore Grand Prix - Race.ts", content=data)
    filed = await app.replays.process_file(race)
    assert filed["outcome"] == "filed"
    quali = write_media(tmp / "inbox" / "F1 2026 Round 12 British Grand Prix Qualifying.ts", content=data)
    with caplog.at_level(logging.INFO, logger="pitlane"):
        r = await app.replays.process_file(quali)
    assert r["outcome"] == "needs_review" and r["reason"] == "duplicate"
    assert r["episode_no"] == 1204
    assert Path(r["path"]).exists() and Path(r["path"]).read_bytes() == data  # never deleted
    row = app.state.one("SELECT * FROM review_items")
    assert row["reason"] == "duplicate" and filed["target"] in row["candidates"]
    assert Path(filed["target"]).read_bytes() == data  # existing item untouched
    eps = [getattr(rec, "episode", None) for rec in caplog.records if getattr(rec, "file", None) == quali.name]
    assert 1204 in eps and 1705 not in eps


# ------------------------------------------------------------------ DVR names without season (step 1)

DVR_NAME = "Formula 1 - E1703 - Singapore Grand Prix – Sprint.ts"


async def _matcher(calendar, tags=None):
    from pitlane.xmltv import session_window

    cfg = parse_config({}, environ={})

    async def probe(p):
        return (tags or {}).get(p.name, {})

    return Matcher(lambda: calendar, lambda s: session_window(s, cfg), probe=probe)


async def test_dvr_name_without_season_parent_folder(calendar_2026, tmp_path):
    # Season from the parent folder; the file's mtime (1970) must not be used.
    p = write_media(tmp_path / "Formula 1" / "Season 2026" / DVR_NAME)
    os.utime(p, (0, 0))
    r = await (await _matcher(calendar_2026)).match(p)
    # Step 1 trusts the episode number: E1703 is round 17, slot 3 (FP3 on the
    # normal-weekend fixture calendar), regardless of the words after it.
    assert isinstance(r, Match) and r.step == "episode" and r.session.id == "2026-17-FP3"


async def test_dvr_name_without_season_uses_recording_start_year(calendar_2026, tmp_path):
    p = write_media(tmp_path / "dvr" / DVR_NAME)
    tags = {DVR_NAME: {"creation_time": "2026-10-10T09:20:00Z"}}
    r = await (await _matcher(calendar_2026, tags)).match(p)
    assert isinstance(r, Match) and r.step == "episode" and r.session.id == "2026-17-FP3"
    # no creation_time: mtime year as the last resort
    p2 = write_media(tmp_path / "dvr2" / DVR_NAME)
    ts = datetime(2026, 10, 10, 11, tzinfo=UTC).timestamp()
    os.utime(p2, (ts, ts))
    r2 = await (await _matcher(calendar_2026)).match(p2)
    assert isinstance(r2, Match) and r2.session.id == "2026-17-FP3"


async def test_dvr_name_without_season_falls_through(calendar_2026, tmp_path):
    # Recording-start year with no such episode → step 1 doesn't match; steps 2/3 run.
    p = write_media(tmp_path / "dvr" / DVR_NAME)
    os.utime(p, (0, 0))
    r = await (await _matcher(calendar_2026)).match(p)
    assert isinstance(r, NoMatch)
    assert any("S1970E1703" in c for c in r.candidates)
    # A conflicting "Season" folder that isn't a season name is ignored, not guessed from.
    p2 = write_media(tmp_path / "Season two" / "Formula 1 - E1705 - Singapore Grand Prix – Race.ts")
    ts = datetime(2026, 10, 11, 12, 30, tzinfo=UTC).timestamp()
    os.utime(p2, (ts, ts))
    r2 = await (await _matcher(calendar_2026)).match(p2)
    assert isinstance(r2, Match) and r2.session.id == "2026-17-RACE"


async def test_dvr_seasonless_recording_filed_end_to_end(env):
    app = await env()
    p = write_media(env.tmp / "dvr" / "Formula 1" / "Season 2026" / "Formula 1 - E1705 - Singapore Grand Prix – Race.ts")
    r = await app.replays.process_file(p)
    assert r["outcome"] == "filed" and r["step"] == "episode" and r["episode_no"] == 1705
    assert r["target"].endswith("Formula 1 - S2026E1705 - Singapore Grand Prix - Race.ts")


# ------------------------------------------------------------------ SUPP duplicate decision


async def test_identical_supp_same_event_is_already_filed(env):
    """docs/replays.md (Matching rules): "If an incoming SUPP file is byte-identical to a
    SUPP item already filed for the same event, that existing path counts as its own
    target ("already filed", REP-3), not as a duplicate"."""
    app = await env()
    data = os.urandom(4096)
    first = write_media(env.tmp / "inbox" / "F1 2026 British Grand Prix Pre-Race Build-Up.mkv", content=data)
    r1 = await app.replays.process_file(first)
    assert r1["outcome"] == "filed" and r1["episode_no"] == 1211
    again = write_media(env.tmp / "inbox" / "again" / "2026 British Grand Prix Build Up show.mkv", content=data)
    r2 = await app.replays.process_file(again)
    assert r2["outcome"] == "already_filed" and r2["target"] == r1["target"] and r2["episode_no"] == 1211
    assert not again.exists()  # move mode: identical to its own target → deleted
    assert len(list((env.tmp / "library").rglob("*.mkv"))) == 1
    assert app.state.one("SELECT COUNT(*) AS n FROM review_items")["n"] == 0
    # Identical SUPP content arriving as a different event's item is a duplicate (REP-3).
    other = write_media(env.tmp / "inbox" / "x" / "F1 2026 Singapore Grand Prix Pre-Race Build-Up.mkv", content=data)
    r3 = await app.replays.process_file(other)
    assert r3["outcome"] == "needs_review" and r3["reason"] == "duplicate"


# ------------------------------------------------------------------ poster verification (REP-5 step 4)


async def test_poster_discarded_then_retried(env, caplog):
    import logging

    app = await env()
    app.replays.poster_retry_delays = (0.05, 0.1, 0.2, 0.4)
    env.pms.discard_uploads = 2  # PMS still processing the new item: first two uploads dropped
    with caplog.at_level(logging.INFO, logger="pitlane"):
        r = await app.replays.process_file(write_media(env.tmp / "dvr" / "Formula 1 - S2026E1705 - Singapore Grand Prix - Race.ts"))
        assert r["outcome"] == "filed"
        assert app.replays.poster_tasks  # verification runs in the background, not in the pipeline
        await asyncio.gather(*app.replays.poster_tasks)
    ep = next(e for e in env.pms.episodes.values() if e.index == 1705)
    assert len(ep.posters) == 3 and len(set(ep.posters)) == 1
    selected = [x for x in ep.poster_list if x["selected"]]
    assert len(selected) == 1 and selected[0]["selected"] is True
    assert selected[0]["key"] == f"/library/metadata/{ep.rating_key}/file?url=upload%3A%2F%2Fposters%2Fseasons%2F2026%2Fepisodes%2F1705%2F3"
    assert any(rec.getMessage() == "poster verified" and rec.uploads == 3 for rec in caplog.records)
    gets = [c for c in env.pms.calls if c["method"] == "GET" and c["path"].endswith("/posters")]
    assert len(gets) == 3


async def test_poster_gives_up_with_warning(env, caplog):
    import logging

    app = await env()
    app.replays.poster_retry_delays = (0.01, 0.01, 0.01, 0.01)
    env.pms.discard_uploads = 100
    with caplog.at_level(logging.INFO, logger="pitlane"):
        await app.replays.process_file(write_media(env.tmp / "dvr" / "Formula 1 - S2026E1704 - Singapore Grand Prix - Qualifying.ts"))
        await asyncio.gather(*app.replays.poster_tasks)
    ep = next(e for e in env.pms.episodes.values() if e.index == 1704)
    assert len(ep.posters) == 5  # first upload + 4 retries (30/60/120/240 s in production)
    warn = [rec for rec in caplog.records if rec.getMessage() == "poster not kept by Plex; giving up"]
    assert len(warn) == 1 and warn[0].levelname == "WARNING"


def test_production_poster_backoff():
    from pitlane.replays.pipeline import POSTER_RETRY_DELAYS

    assert POSTER_RETRY_DELAYS == (30.0, 60.0, 120.0, 240.0)
    assert 7 * 60 <= sum(POSTER_RETRY_DELAYS) <= 10 * 60


async def test_poster_check_decodes_real_pms_keys(env, caplog):
    """REP-5 step 4 regression: real PMS lists the uploaded card as
    ``/library/metadata/5655/file?url=upload%3A%2F%2Fposters%2F…`` (selected: true). A check
    for the literal text "upload://" never matched it and retried 5 times before giving up."""
    import logging

    from pitlane.replays.plex import poster_uri

    real = [
        {"selected": True, "key": "/library/metadata/5655/file?url=upload%3A%2F%2Fposters%2Fseasons%2F2026%2Fepisodes%2F1605%2Fabc123"},
        {"selected": False, "key": "/library/metadata/5655/file?url=media%3A%2F%2F2%2Fd%2Fabc.bundle%2FContents%2FThumbnails%2Fthumb1.jpg"},
    ]
    assert poster_uri(real[0]["key"]).startswith("upload://posters/seasons/2026/episodes/1605/")
    assert poster_uri(real[1]["key"]).startswith("media://")
    assert "upload://" not in real[0]["key"]  # why the literal check failed

    app = await env()
    app.replays.poster_retry_delays = (0.01, 0.01, 0.01, 0.01)
    with caplog.at_level(logging.INFO, logger="pitlane"):
        await app.replays.process_file(write_media(env.tmp / "dvr" / "Formula 1 - S2026E1705 - Singapore Grand Prix - Race.ts"))
        await asyncio.gather(*app.replays.poster_tasks)
    ep = next(e for e in env.pms.episodes.values() if e.index == 1705)
    assert all("upload://" not in x["key"] and "url=" in x["key"] for x in ep.poster_list)  # encoded form
    assert len(ep.posters) == 1  # kept on the first upload → no retries
    msgs = [r.getMessage() for r in caplog.records]
    assert "poster verified" in msgs and "poster not kept by Plex; giving up" not in msgs
    # a selected media:// bundle thumbnail alone is not our card
    ep.poster_list = [real[1] | {"selected": True}]
    assert await app.replays.plex.poster_selected_upload(ep.rating_key) is False
