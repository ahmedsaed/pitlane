"""PROV-1 … PROV-6 and LIVE-5/LIVE-6 tests (no live network: httpx.MockTransport)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

import httpx
import jsonschema
import pytest

from conftest import FIXTURES
from pitlane.channels import ChannelMap
from pitlane.config import parse_config
from pitlane.health import HealthChecker
from pitlane.http import FetchClient, FetchPolicy
from pitlane.metrics import Metrics
from pitlane.providers.base import (
    OpenedInput,
    ProbeResult,
    ProviderContext,
    ProviderError,
    check_ts_sync,
    default_probe,
    validate_headers,
    validate_input_args,
)
from pitlane.providers.channel_json import parse_channel_json
from pitlane.providers.m3u import parse_m3u
from pitlane.providers.registry import ProviderRegistry, load_plugins
from pitlane.state import State

ROOT = Path(__file__).parent.parent
PROV = FIXTURES / "providers"

TS_PACKETS = bytes([0x47] + [0xFF] * 187) * 120


def ctx_for(handler, **kw) -> ProviderContext:
    http = FetchClient(transport=httpx.MockTransport(handler))
    return ProviderContext(
        http=http,
        policy=FetchPolicy(retries=0, rate_limit_per_sec=1000),
        logger=logging.getLogger("test"),
        cache_dir=Path("/nonexistent"),
        resolve_secret=lambda s: s,
        **kw,
    )


# ------------------------------------------------------------- allow-list (PROV-5)


def test_allow_list_accepts_documented_options():
    args = ["-re", "-rw_timeout", "5000000", "-reconnect", "1", "-reconnect_streamed", "1",
            "-live_start_index", "-3", "-user_agent", "X/1", "-headers", "A: b\r\n"]  # fmt: skip
    assert validate_input_args(args) == args


@pytest.mark.parametrize(
    "bad",
    [
        ["-f", "lavfi"],
        ["-i", "x"],
        ["-y"],
        ["-map", "0"],
        ["-rw_timeout"],
        ["-rw_timeout", "abc"],
        ["-user_agent", "-f"],
        ["-reconnect", "1", "-filter_complex", "x"],
        ["-user_agent", "a\nb"],
    ],
)
def test_allow_list_rejects(bad):
    with pytest.raises(ProviderError):
        validate_input_args(bad)


def test_headers_validation():
    assert validate_headers({"User-Agent": "x"}) == {"User-Agent": "x"}
    with pytest.raises(ProviderError):
        validate_headers({"X": "a\r\nInjected: 1"})
    with pytest.raises(ProviderError):
        validate_headers({"Bad Name": "x"})


# ------------------------------------------------------------- m3u


def test_parse_m3u():
    entries = parse_m3u((PROV / "sample.m3u").read_text())
    assert [e.attrs["tvg-id"] for e in entries] == ["f1.main", "f1.main.backup", "cooking"]
    assert entries[0].headers == {"User-Agent": "ExampleAgent/1.0", "Referer": "https://example.invalid/"}
    assert entries[0].kind == "hls" and entries[1].kind == "ts"
    assert entries[0].name == "F1 Main Feed"


def _cfg(providers, channels=None, **extra):
    return parse_config(
        {
            "channels": channels
            or [
                {"id": "f1-main", "guide_number": 101, "name": "F1 Main Feed", "series": ["f1"]},
                {"id": "f1-alt", "guide_number": 102, "name": "F1 Alternate"},
            ],
            "providers": providers,
            **extra,
        },
        environ={},
    )


async def _registry(cfg, tmp_path, handler=None):
    http = FetchClient(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(404))))
    reg = ProviderRegistry(cfg, http, tmp_path)
    await reg.start()
    return reg


async def test_m3u_provider_mapping(tmp_path):
    cfg = _cfg(
        [
            {
                "type": "m3u",
                "name": "iptv",
                "path": str(PROV / "sample.m3u"),
                "map": [
                    {"match": {"tvg-id": r"^f1\.main$"}, "channel": "f1-main", "priority": 10},
                    {"match": {"tvg-id": r"backup", "group-title": "Motor"}, "channel": "f1-main", "priority": 20},
                ],
            }
        ]
    )
    reg = await _registry(cfg, tmp_path)
    p = reg.get("iptv")
    specs = await p.list_sources()
    assert [(s.channel, s.priority, s.kind) for s in specs] == [("f1-main", 10, "hls"), ("f1-main", 20, "ts")]
    opened = await p.open(specs[0].locator)
    assert opened.headers["User-Agent"] == "ExampleAgent/1.0"


async def test_m3u_map_unmatched_creates_stable_channels(tmp_path):
    cfg = _cfg([{"type": "m3u", "name": "iptv", "path": str(PROV / "sample.m3u"), "map_unmatched": True}])
    state = State(tmp_path / "s.db")
    reg = await _registry(cfg, tmp_path)
    cmap = ChannelMap(state)
    cmap.load_channels(cfg)
    await cmap.refresh_from(reg)
    auto = sorted((c.id, c.guide_number) for c in cmap.channels.values() if c.auto)
    assert len(auto) == 3 and all(gn.isdigit() and int(gn) >= 1000 for _, gn in auto)
    # LIVE-2: same numbers after a "restart"
    cmap2 = ChannelMap(State(tmp_path / "s.db"))
    cmap2.load_channels(cfg)
    await cmap2.refresh_from(reg)
    assert sorted((c.id, c.guide_number) for c in cmap2.channels.values() if c.auto) == auto


async def test_m3u_from_url_with_retry(tmp_path):
    calls = {"n": 0}
    body = (PROV / "sample.m3u").read_text()

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, text=body)

    cfg = _cfg(
        [
            {
                "type": "m3u",
                "name": "iptv",
                "url": "https://example.invalid/list.m3u",
                "http": {"retries": 2, "backoff": "0s", "rate_limit_per_sec": 100},
                "map": [{"match": {"name": "Cooking"}, "channel": "f1-alt"}],
            }
        ]
    )
    reg = await _registry(cfg, tmp_path, handler)
    specs = await reg.get("iptv").list_sources()
    assert calls["n"] == 2  # PROV-4 retry
    assert [s.channel for s in specs] == ["f1-alt"]


# ------------------------------------------------------------- channel-json


def test_channel_json_schema_file_matches_examples():
    schema = json.loads((ROOT / "schemas" / "channel-json.v1.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    doc = json.loads((PROV / "channels.json").read_text())
    doc["channels"] = doc["channels"][:3]
    jsonschema.validate(doc, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"version": 2, "channels": []}, schema)


def test_parse_channel_json():
    entries, errors = parse_channel_json((PROV / "channels.json").read_text())
    assert [e.label for e in entries] == ["Main feed – primary", "Alt", "Disabled"]
    assert len(errors) == 1 and "channels[3].url" in errors[0]
    with pytest.raises(ProviderError):
        parse_channel_json('{"version": 2, "channels": []}')


async def test_channel_json_provider(tmp_path):
    cfg = _cfg([{"type": "channel-json", "name": "mine", "path": str(PROV / "channels.json")}])
    reg = await _registry(cfg, tmp_path)
    specs = await reg.get("mine").list_sources()
    assert [(s.channel, s.priority) for s in specs] == [("f1-main", 10), ("f1-alt", 100)]
    opened = await reg.get("mine").open(specs[0].locator)
    assert opened.headers == {"User-Agent": "Pitlane/1.0"}


# ------------------------------------------------------------- hdhomerun


async def test_hdhomerun_provider(tmp_path):
    lineup = [
        {"GuideNumber": "4.1", "GuideName": "Channel 4 HD", "URL": "http://192.0.2.50:5004/auto/v4.1"},
        {"GuideNumber": "5.1", "GuideName": "Other", "URL": "http://192.0.2.50:5004/auto/v5.1"},
    ]

    def handler(req):
        if req.url.path == "/lineup.json":
            return httpx.Response(200, json=lineup)
        if req.url.path == "/discover.json":
            return httpx.Response(200, json={"DeviceID": "ABCD0123"})
        return httpx.Response(404)

    cfg = _cfg(
        [
            {
                "type": "hdhomerun",
                "name": "antenna",
                "device": "http://192.0.2.50",
                "map": [{"match": {"GuideName": "^Channel 4"}, "channel": "f1-alt", "priority": 10}],
            }
        ]
    )
    reg = await _registry(cfg, tmp_path, handler)
    p = reg.get("antenna")
    specs = await p.list_sources()
    assert [(s.channel, s.kind, s.label) for s in specs] == [("f1-alt", "tuner", "4.1 Channel 4 HD")]
    res = await p.probe(specs[0].locator)
    assert res.state == "online"


# ------------------------------------------------------------- default probe


def hls_handler(segment_status=200, delay=None):
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=3000000\nhigh/index.m3u8\n"
    media = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg0.ts\n#EXTINF:2.0,\nseg1.ts\n"
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url.path)
        if req.url.path == "/live/master.m3u8":
            return httpx.Response(200, text=master)
        if req.url.path == "/live/low/index.m3u8":
            return httpx.Response(200, text=media)
        if req.url.path == "/live/low/seg0.ts":
            return httpx.Response(segment_status, content=TS_PACKETS)
        if req.url.path == "/raw.ts":
            return httpx.Response(200, content=TS_PACKETS)
        if req.url.path == "/notts":
            return httpx.Response(200, content=b"x" * 30000)
        return httpx.Response(404)

    return handler, seen


async def test_probe_hls_follows_master_first_variant():
    handler, seen = hls_handler()
    ctx = ctx_for(handler)
    res = await default_probe("hls", OpenedInput("http://h.invalid/live/master.m3u8"), ctx)
    assert res.state == "online", res.error
    assert seen == ["/live/master.m3u8", "/live/low/index.m3u8", "/live/low/seg0.ts"]


async def test_probe_hls_segment_error_offline():
    handler, _ = hls_handler(segment_status=403)
    res = await default_probe("hls", OpenedInput("http://h.invalid/live/master.m3u8"), ctx_for(handler))
    assert res.state == "offline" and "403" in res.error


async def test_probe_degraded_when_slow():
    handler, _ = hls_handler()
    res = await default_probe("hls", OpenedInput("http://h.invalid/live/master.m3u8"), ctx_for(handler, degraded_latency_ms=-1))
    assert res.state == "degraded"


async def test_probe_ts_sync_bytes():
    handler, _ = hls_handler()
    ctx = ctx_for(handler)
    assert (await default_probe("ts", OpenedInput("http://h.invalid/raw.ts"), ctx)).state == "online"
    res = await default_probe("ts", OpenedInput("http://h.invalid/notts"), ctx)
    assert res.state == "offline" and "sync" in res.error


def test_check_ts_sync_offset():
    assert check_ts_sync(b"\x00" * 5 + TS_PACKETS)
    assert not check_ts_sync(b"\x00" * 188 * 101)


async def test_probe_file(tmp_path):
    ctx = ctx_for(lambda r: httpx.Response(404))
    res = await default_probe("file", OpenedInput(str(tmp_path / "missing.ts")), ctx)
    assert res.state == "offline"


# ------------------------------------------------------------- plugins (PROV-6)


PLUGIN = '''
from pitlane.providers.base import BaseProvider, SourceSpec, OpenedInput
PROVIDER_TYPE = "demo"
MARKER = []

class DemoProvider(BaseProvider):
    capabilities = {"live"}
    async def list_sources(self):
        return [SourceSpec(channel="f1-main", locator="demo-1", kind="ts", label="Demo")]
    def kind_of(self, locator):
        return "ts"
    async def open(self, locator):
        return OpenedInput("http://h.invalid/raw.ts")
'''


async def test_plugin_loading(tmp_path):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "demo.py").write_text(PLUGIN)
    (pdir / "evil.py").write_text('PROVIDER_TYPE = "evil"\nraise SystemExit("must not be imported")\n')
    assert set(load_plugins(pdir, ["demo"])) == {"demo"}
    cfg = _cfg([{"type": "demo", "name": "d"}], plugins={"dir": str(pdir), "enabled": ["demo"]})
    reg = await _registry(cfg, tmp_path)
    assert [s.label for s in await reg.get("d").list_sources()] == ["Demo"]


# ------------------------------------------------------------- channel map + health (LIVE-5, LIVE-6)


class FakeProvider:
    def __init__(self, name, results):
        self.name = name
        self.capabilities = {"live"}
        self.results = results

    async def probe(self, locator):
        r = self.results[locator]
        if isinstance(r, Exception):
            raise r
        return r


class FakeRegistry:
    def __init__(self, providers):
        self.p = {p.name: p for p in providers}

    def get(self, name):
        return self.p.get(name)

    def live(self):
        return list(self.p.values())


async def test_health_and_source_order(tmp_path):
    from pitlane.providers.base import SourceSpec

    cfg = _cfg([])
    cmap = ChannelMap(State(":memory:"))
    cmap.load_channels(cfg)
    cmap.apply_sources(
        [
            ("p", SourceSpec("f1-main", "a", "hls", "A", priority=10)),
            ("p", SourceSpec("f1-main", "b", "hls", "B", priority=20)),
            ("p", SourceSpec("f1-main", "c", "hls", "C", priority=30)),
            ("p", SourceSpec("f1-main", "d", "hls", "D", priority=5)),
            ("p", SourceSpec("nope", "e", "hls", "E")),
        ]
    )
    assert len(cmap.sources) == 4  # unknown channel ignored
    prov = FakeProvider(
        "p",
        {
            "a": ProbeResult("degraded", 3000, None),
            "b": ProbeResult("online", 10, None),
            "c": ProbeResult("online", 10, None),
            "d": RuntimeError("boom"),
        },
    )
    metrics = Metrics()
    hc = HealthChecker(cmap, FakeRegistry([prov]), metrics, timedelta(minutes=5), timedelta(minutes=1))
    await hc.probe_all()
    order = [s.label for s in cmap.ordered_sources("f1-main")]
    assert order == ["B", "C", "A"]  # online by priority, then degraded; offline D excluded
    d = next(s for s in cmap.sources.values() if s.label == "D")
    assert d.state == "offline" and d.last_error == "boom"
    text = metrics.render().decode()
    assert 'pitlane_source_state{channel="f1-main"' in text
    assert [c.id for c in cmap.lineup()] == ["f1-main"]


def test_adaptive_interval():
    from datetime import UTC, datetime

    hc = HealthChecker(
        ChannelMap(State(":memory:")), FakeRegistry([]), Metrics(), timedelta(minutes=5), timedelta(minutes=1),
        is_near_session=lambda now: now.hour == 12,
    )  # fmt: skip
    assert hc.current_interval(datetime(2026, 1, 1, 12, tzinfo=UTC)) == timedelta(minutes=1)
    assert hc.current_interval(datetime(2026, 1, 1, 9, tzinfo=UTC)) == timedelta(minutes=5)
