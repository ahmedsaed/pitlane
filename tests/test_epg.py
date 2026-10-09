"""EPG-1 … EPG-7: calendar normalization, episode numbering, XMLTV golden files."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

import httpx
import pytest

from conftest import FIXTURES, assert_golden, jolpica_fixture
from pitlane.calendar.jolpica import Calendar, JolpicaClient, normalize_schedule
from pitlane.config import parse_config
from pitlane.http import FetchClient
from pitlane.models import SessionType
from pitlane.xmltv import build_programmes, render_xmltv

BASE = "http://192.168.1.10:5004"


def make_cfg(**epg):
    return parse_config(
        {
            "channels": [
                {"id": "f1-main", "guide_number": 101, "name": "F1 Main Feed", "series": ["f1"], "logo": "/config/logos/f1-main.png"},
                {"id": "f1-alt", "guide_number": 102, "name": "F1 Alternate", "series": ["f1"]},
                {"id": "news", "guide_number": 200, "name": "Paddock News"},
            ],
            "epg": {"days_back": 1, "days_ahead": 3, **epg},
        },
        environ={},
    )


# ------------------------------------------------------------------ normalization


def test_normal_weekend_numbering(calendar_2026):
    ev = calendar_2026.event("2026-17")
    assert ev and not ev.is_sprint_weekend
    got = {s.type: s.episode_no for s in ev.sessions}
    assert got == {
        SessionType.FP1: 1701,
        SessionType.FP2: 1702,
        SessionType.FP3: 1703,
        SessionType.QUALI: 1704,
        SessionType.RACE: 1705,
    }
    race = ev.session(SessionType.RACE)
    assert race.onscreen == "S2026E1705"
    assert race.starts_at == datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    assert race.nominal_minutes == 120


def test_sprint_weekend_numbering(calendar_2026):
    ev = calendar_2026.event("2026-18")
    assert ev.is_sprint_weekend
    got = {s.type: s.episode_no for s in ev.sessions}
    assert got == {
        SessionType.FP1: 1801,
        SessionType.SQ: 1802,
        SessionType.SPRINT: 1803,
        SessionType.QUALI: 1804,
        SessionType.RACE: 1805,
    }
    assert ev.session(SessionType.SQ).nominal_minutes == 45


def test_round_one_episode(calendar_2026):
    assert calendar_2026.event("2026-1").session(SessionType.RACE).episode_no == 105


def test_date_only_sessions(calendar_2026):
    ev = calendar_2026.event("2026-20")
    assert all(s.date_only for s in ev.sessions)
    assert ev.session(SessionType.RACE).starts_at == datetime(2026, 11, 8, tzinfo=UTC)


def test_sprint_shootout_alias_and_legacy_fp2():
    doc = {
        "MRData": {
            "RaceTable": {
                "Races": [
                    {
                        "season": "2023",
                        "round": "4",
                        "raceName": "Test GP",
                        "Circuit": {"circuitName": "C", "Location": {"locality": "L", "country": "X"}},
                        "date": "2023-04-30",
                        "time": "11:00:00Z",
                        "FirstPractice": {"date": "2023-04-28", "time": "09:30:00Z"},
                        "SecondPractice": {"date": "2023-04-29", "time": "08:30:00Z"},
                        "SprintShootout": {"date": "2023-04-29", "time": "08:30:00Z"},
                        "Sprint": {"date": "2023-04-29", "time": "13:30:00Z"},
                        "Qualifying": {"date": "2023-04-28", "time": "13:00:00Z"},
                    }
                ]
            }
        }
    }
    (ev,) = normalize_schedule(doc)
    types = {s.type: s.episode_no for s in ev.sessions}
    assert types[SessionType.SQ] == 402
    # QA-003: FP2 collides with SQ's slot on a sprint weekend → skipped, never duplicated.
    assert SessionType.FP2 not in types
    assert len(set(types.values())) == len(types)


def test_configured_durations():
    cfg = make_cfg(durations={"RACE": "150m"})
    (ev,) = [e for e in normalize_schedule(jolpica_fixture("2026-races.json"), cfg.epg.nominal_minutes) if e.round == 17]
    assert ev.session(SessionType.RACE).nominal_minutes == 150


# ------------------------------------------------------------------ calendar fetch + cache


async def test_jolpica_fetch_cache_and_offline(tmp_path):
    calls = []
    body = (FIXTURES / "jolpica" / "2026-races.json").read_bytes()
    empty = (FIXTURES / "jolpica" / "2027-races.json").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, content=body if "/2026/" in req.url.path else empty)

    http = FetchClient(transport=httpx.MockTransport(handler))
    client = JolpicaClient(http, tmp_path / "jolpica")
    cal = await client.load_calendar("auto", now=datetime(2026, 10, 9, tzinfo=UTC))
    assert len(cal.events) == 6
    assert calls[0] == "https://api.jolpi.ca/ergast/f1/2026/races/?format=json"
    assert (tmp_path / "jolpica" / "2026-races.json").exists()

    def down(req):
        raise httpx.ConnectError("unreachable")

    offline = JolpicaClient(FetchClient(transport=httpx.MockTransport(down)), tmp_path / "jolpica")
    offline.policy.retries = 0
    cal2 = await offline.load_calendar([2026], now=datetime(2026, 10, 9, tzinfo=UTC))
    assert [e.id for e in cal2.events] == [e.id for e in cal.events]
    await http.aclose()


# ------------------------------------------------------------------ XMLTV


def test_golden_normal_weekend(calendar_2026):
    cfg = make_cfg()
    out = render_xmltv(cfg, {"f1": calendar_2026}, datetime(2026, 10, 9, 8, 0, tzinfo=UTC), BASE)
    assert_golden("xmltv-normal-weekend.xml", out)


def test_golden_sprint_weekend_dst(calendar_2026):
    # EU summer time ends 2026-10-25 01:00 UTC, inside this window; local times
    # in filler labels switch from BST to GMT while all programme times stay UTC.
    cfg = make_cfg(
        timezone="Europe/London",
        manual=[
            {
                "channel": "f1-main",
                "title": "Pre-Race Build-Up",
                "relative_to": "RACE",
                "offset": "-90m",
                "duration": "75m",
                "supplementary_index": 1,
            }
        ],
    )
    out = render_xmltv(cfg, {"f1": calendar_2026}, datetime(2026, 10, 23, 6, 0, tzinfo=UTC), BASE)
    assert_golden("xmltv-sprint-weekend-dst.xml", out)
    text = out.decode()
    assert "BST" in text and "GMT" in text


def test_xmltv_example_programme_matches_spec(calendar_2026):
    cfg = make_cfg()
    out = render_xmltv(cfg, {"f1": calendar_2026}, datetime(2026, 10, 9, 8, 0, tzinfo=UTC), BASE)
    root = ET.fromstring(out)
    race = [
        p
        for p in root.findall("programme")
        if p.get("channel") == "f1-main.pitlane" and p.findtext("episode-num[@system='onscreen']") == "S2026E1705"
    ]
    assert len(race) == 1
    p = race[0]
    assert p.get("start") == "20261011115000 +0000"  # official 12:00 − 10 min pre-roll
    assert p.get("stop") == "20261011142000 +0000"  # official + 120 + 20 (QA-001)
    assert p.findtext("title") == "Formula 1"
    assert p.findtext("sub-title") == "Singapore Grand Prix – Race"
    assert p.findtext("desc") == "Round 17 of the 2026 season at Marina Bay Street Circuit, Singapore."
    assert [c.text for c in p.findall("category")] == ["Sports", "Motorsport"]
    assert p.findtext("episode-num[@system='xmltv_ns']") == "2025.1704."
    assert p.findtext("date") == "20261011"
    assert p.find("new") is not None
    ch = root.find("channel[@id='f1-main.pitlane']")
    assert [d.text for d in ch.findall("display-name")] == ["F1 Main Feed", "101"]
    assert ch.find("icon").get("src") == f"{BASE}/static/logos/f1-main.png"


def _check_invariants(progs, w_start, w_end, filler_max):
    for p in progs:
        assert p.stop > p.start
        if p.is_filler:
            assert p.stop - p.start <= filler_max
            assert not p.new
            # split at midnight UTC
            assert p.start.date() == (p.stop - timedelta(microseconds=1)).date()
    for a, b in zip(progs, progs[1:]):
        assert a.stop <= b.start, "programmes overlap"
        assert a.stop == b.start or not (w_start <= a.stop < w_end), "gap in guide"


@pytest.mark.parametrize("now", [datetime(2026, 10, 9, 8, tzinfo=UTC), datetime(2026, 10, 23, 6, tzinfo=UTC)])
def test_no_overlaps_no_gaps(calendar_2026, now):
    cfg = make_cfg(pre_roll="60m", post_roll={"default": "90m"})  # force overlaps
    from pitlane.xmltv import guide_window

    w_start, w_end = guide_window(now, cfg)
    for ch, progs in build_programmes(cfg, {"f1": calendar_2026}, now).items():
        assert progs, ch
        assert progs[0].start <= w_start and progs[-1].stop >= w_end
        _check_invariants(progs, w_start, w_end, cfg.epg.filler_max)


def test_only_tagged_channels_get_sessions(calendar_2026):
    cfg = make_cfg()
    progs = build_programmes(cfg, {"f1": calendar_2026}, datetime(2026, 10, 9, 8, tzinfo=UTC))
    assert all(p.is_filler for p in progs["news"])
    assert any(not p.is_filler for p in progs["f1-main"])
    # EPG-6: same series title on every session programme
    assert {p.title for p in progs["f1-main"] if not p.is_filler} == {"Formula 1"}


def test_xmltv_deterministic(calendar_2026):
    cfg = make_cfg()
    now = datetime(2026, 10, 9, 8, tzinfo=UTC)
    a = render_xmltv(cfg, {"f1": calendar_2026}, now, BASE)
    b = render_xmltv(cfg, {"f1": Calendar(list(reversed(calendar_2026.events)))}, now, BASE)
    assert a == b


def test_escaping():
    cfg = parse_config({"channels": [{"id": "a", "guide_number": 1, "name": "Tom & <Jerry>"}]}, environ={})
    out = render_xmltv(cfg, {}, datetime(2026, 1, 1, tzinfo=UTC), BASE)
    ET.fromstring(out)
    assert b"Tom &amp; &lt;Jerry&gt;" in out


def test_filler_is_presented_as_tv(calendar_2026):
    """docs/epg.md: filler gets <category>Series</category> and a date-based
    original-air-date episode number (programme start, UTC); never <new/>; keeps the
    channel-name title so it never joins the "Formula 1" series rule."""
    cfg = make_cfg()
    out = render_xmltv(cfg, {"f1": calendar_2026}, datetime(2026, 10, 9, 8, tzinfo=UTC), BASE)
    root = ET.fromstring(out)
    names = {c.get("id"): c.findtext("display-name") for c in root.findall("channel")}
    fillers = [p for p in root.findall("programme") if p.findtext("title") != "Formula 1"]
    assert fillers
    for p in fillers:
        assert p.findtext("title") == names[p.get("channel")]
        assert [c.text for c in p.findall("category")] == ["Series"]
        start = datetime.strptime(p.get("start"), "%Y%m%d%H%M%S %z")
        oad = p.find("episode-num[@system='original-air-date']")
        assert oad is not None and oad.text == start.strftime("%Y-%m-%d %H:%M:%S")
        assert p.find("new") is None
        assert p.find("episode-num[@system='xmltv_ns']") is None
    sessions = [p for p in root.findall("programme") if p.findtext("title") == "Formula 1"]
    assert all(p.find("episode-num[@system='original-air-date']") is None for p in sessions)
    assert all("Series" not in [c.text for c in p.findall("category")] for p in sessions)
