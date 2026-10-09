"""LIVE-1, LIVE-3, LIVE-4, LIVE-7, LIVE-9, NFR-1, NFR-4 integration tests.

Uses a locally generated HLS stream (scripts/make_test_hls.py) behind two
flaky proxies (scripts/flaky_proxy.py) and an in-process uvicorn server.
Skipped with a clear reason when ffmpeg is not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest
import uvicorn

from conftest import needs_ffmpeg
from pitlane.app import App, create_app
from pitlane.config import parse_config
from pitlane.providers.base import OpenedInput, ProviderError
from pitlane.relay import build_ffmpeg_args

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from flaky_proxy import FlakyProxy  # noqa: E402
from make_test_hls import hls_command, serve  # noqa: E402

# ------------------------------------------------------------------ unit: command line


def test_ffmpeg_args_hls():
    args = build_ffmpeg_args(
        "hls",
        OpenedInput("https://example.invalid/a.m3u8", {"User-Agent": "UA/1", "X-Token": "t"}, ["-live_start_index", "-3"]),
        ffmpeg="ffmpeg",
    )
    s = " ".join(args)
    assert args[:5] == ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    assert "-headers" in args and args[args.index("-headers") + 1] == "X-Token: t\r\n"
    assert args[args.index("-user_agent") + 1] == "UA/1"
    assert "-rw_timeout 10000000 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5" in s
    assert s.endswith("-i https://example.invalid/a.m3u8 -map 0:v:0? -map 0:a? -c copy -f mpegts -mpegts_flags +resend_headers -muxdelay 0 pipe:1")
    assert args.index("-live_start_index") < args.index("-i")


def test_ffmpeg_args_file_loop_and_transcode():
    args = build_ffmpeg_args("file", OpenedInput("/media/x.ts"), loop=True, transcode="720p", ffmpeg="ffmpeg")
    s = " ".join(args)
    assert "-re -stream_loop -1 -i /media/x.ts" in s
    assert "-c:v libx264" in s and "scale=-2:720" in s and "-c copy" not in s


@pytest.mark.parametrize(
    "kind,opened",
    [
        ("hls", OpenedInput("file:///etc/passwd")),
        ("file", OpenedInput("relative.ts")),
        ("hls", OpenedInput("https://x.invalid/a.m3u8", ffmpeg_input_args=["-f", "lavfi"])),
    ],
)
def test_ffmpeg_args_rejects(kind, opened):
    with pytest.raises(ProviderError):
        build_ffmpeg_args(kind, opened, ffmpeg="ffmpeg")


# ------------------------------------------------------------------ fixtures


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def hls_stream(tmp_path_factory):
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed; relay integration tests skipped")
    out = tmp_path_factory.mktemp("hls")
    proc = subprocess.Popen(hls_command(out, size="320x180", rate=25, segment=1), start_new_session=True)
    port = _free_port()
    srv = serve(out, port)
    deadline = time.time() + 20
    while time.time() < deadline:
        pl = out / "index.m3u8"
        if pl.exists() and pl.read_text().count("#EXTINF") >= 3:
            break
        time.sleep(0.2)
    else:
        os.killpg(proc.pid, signal.SIGTERM)
        pytest.fail("test HLS stream did not start")
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(10)


class Harness:
    def __init__(self, app: App, base: str, proxies: list[FlakyProxy]):
        self.app = app
        self.base = base
        self.proxies = proxies


def our_ffmpeg_pids(exclude: set[int]) -> set[int]:
    me = psutil.Process()
    return {p.pid for p in me.children(recursive=True) if p.pid not in exclude and "ffmpeg" in (p.name() or "")}


@pytest.fixture
async def harness(hls_stream, tmp_path):
    proxies = [FlakyProxy(hls_stream), FlakyProxy(hls_stream)]
    for p in proxies:
        await p.start()
    channels = {
        "version": 1,
        "channels": [
            {"channel": "f1-main", "label": "A", "kind": "hls", "url": f"{proxies[0].url}/index.m3u8", "priority": 10},
            {"channel": "f1-main", "label": "B", "kind": "hls", "url": f"{proxies[1].url}/index.m3u8", "priority": 20},
            {"channel": "f1-dead", "label": "Dead", "kind": "hls", "url": "http://127.0.0.1:9/none.m3u8"},
        ],
    }
    (tmp_path / "channels.json").write_text(json.dumps(channels))
    port = _free_port()
    cfg = parse_config(
        {
            "server": {"bind": f"127.0.0.1:{port}", "advertised_url": f"http://127.0.0.1:{port}", "data_dir": str(tmp_path / "data")},
            "tuners": {"count": 2, "stall_timeout": "3s", "tune_timeout": "8s"},
            "channels": [
                {"id": "f1-main", "guide_number": 101, "name": "F1 Main Feed", "series": ["f1"]},
                {"id": "f1-dead", "guide_number": 102, "name": "Dead Channel"},
            ],
            "providers": [{"type": "channel-json", "name": "mine", "path": str(tmp_path / "channels.json")}],
        },
        environ={},
    )
    pl = App(cfg, start_background=False)
    api = create_app(app_obj=pl, install_signal_handlers=False)
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    baseline_pids = our_ffmpeg_pids(set())
    h = Harness(pl, f"http://127.0.0.1:{port}", proxies)
    h.baseline_pids = baseline_pids  # type: ignore[attr-defined]
    yield h
    server.should_exit = True
    await task
    for p in proxies:
        await p.stop()


async def read_bytes(resp: httpx.Response, n: int, timeout: float) -> bytes:
    buf = bytearray()
    it = getattr(resp, "_pl_iter", None)
    if it is None:
        it = resp._pl_iter = resp.aiter_raw()  # type: ignore[attr-defined]

    async def go():
        async for chunk in it:
            buf.extend(chunk)
            if len(buf) >= n:
                return

    await asyncio.wait_for(go(), timeout)
    return bytes(buf)


def assert_ts(data: bytes) -> None:
    off = data.find(b"\x47")
    assert off >= 0 and all(data[off + k * 188] == 0x47 for k in range(min(50, (len(data) - off) // 188)))


async def wait_until(pred, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


# ------------------------------------------------------------------ integration


@needs_ffmpeg
async def test_discovery_endpoints(harness):
    async with httpx.AsyncClient(base_url=harness.base) as c:
        d = (await c.get("/discover.json")).json()
        assert d["TunerCount"] == 2 and len(d["DeviceID"]) == 8 and d["LineupURL"] == f"{harness.base}/lineup.json"
        int(d["DeviceID"], 16)
        lineup = (await c.get("/lineup.json")).json()
        assert lineup == [
            {"GuideNumber": "101", "GuideName": "F1 Main Feed", "URL": f"{harness.base}/auto/v101"},
            {"GuideNumber": "102", "GuideName": "Dead Channel", "URL": f"{harness.base}/auto/v102"},
        ]
        assert (await c.get("/lineup_status.json")).json()["ScanPossible"] == 1
        assert (await c.post("/lineup.post?scan=start")).status_code == 200
        assert b"<UDN>uuid:" + d["DeviceID"].encode() in (await c.get("/device.xml")).content
        assert b"<GuideNumber>101</GuideNumber>" in (await c.get("/lineup.xml")).content
        assert (await c.get("/auto/v999")).status_code == 404
        assert (await c.get("/healthz")).status_code == 200
        x = await c.get("/xmltv.xml")
        assert x.headers["cache-control"] == "max-age=900" and x.headers["etag"]
        assert (await c.get("/xmltv.xml", headers={"If-None-Match": x.headers["etag"]})).status_code == 304
    # LIVE-2 / docs/tuner.md: DeviceID survives restart (persisted in state)
    assert harness.app.state.device_id() == d["DeviceID"]


@needs_ffmpeg
async def test_tune_failover_and_cleanup(harness):
    app = harness.app
    async with httpx.AsyncClient(base_url=harness.base, timeout=None) as c:
        t0 = time.monotonic()
        async with c.stream("GET", "/auto/v101") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "video/mp2t"
            assert "content-length" not in resp.headers
            first = await read_bytes(resp, 188 * 10, 10)
            tune = time.monotonic() - t0
            assert tune < 3.0, f"NFR-1 tune time {tune:.2f}s"
            assert_ts(first)
            await read_bytes(resp, 50_000, 10)
            assert len(app.tracker.live) == 1

            # LIVE-7: kill source A mid-stream → B within stall_timeout + 3 s
            harness.proxies[0].set_mode("stall")
            t_kill = time.monotonic()
            failovers = app.metrics.failovers.labels(channel="f1-main")
            await wait_until(lambda: failovers._value.get() >= 1, 3 + 3 + 1, step=0.01)
            assert failovers._value.get() == 1, "no failover happened"
            # Bytes from B reach the client within stall_timeout + 3 s of the kill.
            await read_bytes(resp, 188 * 10, 3 + 3 + 1 - (time.monotonic() - t_kill))
            switch = time.monotonic() - t_kill
            assert switch <= 3 + 3, f"failover took {switch:.1f}s"
            more = await read_bytes(resp, 100_000, 10)  # client keeps receiving
            assert_ts(more)
            src_a = next(s for s in app.channel_map.sources.values() if s.label == "A")
            assert src_a.state in ("degraded", "offline")
            # the stalled process is retired in the background (SIGTERM, SIGKILL after 3 s)
            assert await wait_until(lambda: len(app.tracker.live) == 1, 4)
        # LIVE-9: client gone → ffmpeg gone and slot free within 5 s
        t_close = time.monotonic()
        assert await wait_until(lambda: not app.tracker.live and not app.pool.in_use, 5)
        assert time.monotonic() - t_close < 5
    assert await wait_until(lambda: not our_ffmpeg_pids(harness.baseline_pids), 5)
    run = app.state.one("SELECT * FROM stream_runs")
    assert run["failovers"] == 1 and run["bytes"] > 100_000 and run["ended_at"]


@needs_ffmpeg
async def test_tuner_limit_and_dead_channel(harness):
    app = harness.app
    async with httpx.AsyncClient(base_url=harness.base, timeout=None) as c:
        # No source can start → 503 within tune_timeout.
        t0 = time.monotonic()
        r = await c.get("/auto/v102")
        assert r.status_code == 503 and time.monotonic() - t0 < 8 + 2
        assert not app.pool.in_use

        async with c.stream("GET", "/auto/v101") as r1, c.stream("GET", "/auto/v101") as r2:
            assert r1.status_code == 200 and r2.status_code == 200
            r3 = await c.get("/auto/v101")
            assert r3.status_code == 503  # LIVE-4
            assert len(app.pool.in_use) == 2
    assert await wait_until(lambda: not app.pool.in_use and not app.tracker.live, 5)
    text = (await httpx.AsyncClient().get(f"{harness.base}/metrics")).text
    assert "pitlane_tuner_rejections_total 2.0" in text


@needs_ffmpeg
async def test_all_sources_exhausted_ends_response(harness):
    app = harness.app
    async with httpx.AsyncClient(base_url=harness.base, timeout=None) as c:
        async with c.stream("GET", "/auto/v101") as resp:
            await read_bytes(resp, 20_000, 10)
            for p in harness.proxies:
                p.set_mode("drop")

            async def drain():
                async for _ in resp._pl_iter:
                    pass

            # The relay ends the response once every source is exhausted.
            await asyncio.wait_for(drain(), 30)
    assert await wait_until(lambda: not app.pool.in_use and not app.tracker.live, 5)


@needs_ffmpeg
async def test_soak_flaps_no_leaks(harness):
    """NFR-4 (scaled down): repeated source up/down transitions leak no processes or fds.

    Set PITLANE_SOAK_FLAPS=1000 for the full acceptance run.
    """
    app = harness.app
    flaps = int(os.environ.get("PITLANE_SOAK_FLAPS", "6"))
    me = psutil.Process()
    async with httpx.AsyncClient(base_url=harness.base, timeout=None) as c:
        # warm up once so lazily opened fds (sqlite WAL, sockets) exist in the baseline
        async with c.stream("GET", "/auto/v101") as resp:
            await read_bytes(resp, 5000, 10)
        await wait_until(lambda: not app.tracker.live, 5)
        fds0 = me.num_fds()
        for i in range(flaps):
            for p in harness.proxies:
                p.set_mode("pass")
            async with c.stream("GET", "/auto/v101") as resp:
                await read_bytes(resp, 5000, 10)
                harness.proxies[i % 2].set_mode("drop" if i % 3 else "stall")
                await asyncio.sleep(0.2)
        for p in harness.proxies:
            p.set_mode("pass")
    assert await wait_until(lambda: not app.tracker.live and not app.pool.in_use, 6)
    assert await wait_until(lambda: not our_ffmpeg_pids(harness.baseline_pids), 6)
    await asyncio.sleep(0.5)
    assert me.num_fds() <= fds0 + 3, f"fd leak: {fds0} → {me.num_fds()}"


@needs_ffmpeg
async def test_offline_at_startup_recovers_before_first_tune(harness):
    """LIVE-6 regression: Pitlane started before its sources were reachable, so every
    source was probed offline; they recovered before the first tune. The tune must
    re-probe the offline sources and succeed instead of returning 503."""
    app = harness.app
    for p in harness.proxies:
        p.set_mode("error")
    await app.health.probe_all()
    main = app.channel_map.channel_sources("f1-main")
    assert {s.state for s in main} == {"offline"}
    assert app.channel_map.ordered_sources("f1-main") == []
    for p in harness.proxies:
        p.set_mode("pass")  # sources come back before anyone tunes
    async with httpx.AsyncClient(base_url=harness.base, timeout=None) as c:
        t0 = time.monotonic()
        async with c.stream("GET", "/auto/v101") as resp:
            assert resp.status_code == 200
            assert_ts(await read_bytes(resp, 188 * 20, 10))
            assert time.monotonic() - t0 < 8  # within tune_timeout
        a = next(s for s in main if s.label == "A")
        assert a.state == "online"  # first in priority order, health state updated
        b = next(s for s in main if s.label == "B")
        assert b.state == "offline"  # not needed, so not re-probed
    assert await wait_until(lambda: not app.pool.in_use and not app.tracker.live, 5)


@needs_ffmpeg
async def test_offline_sources_still_down_gives_503(harness):
    app = harness.app
    for p in harness.proxies:
        p.set_mode("error")
    await app.health.probe_all()
    async with httpx.AsyncClient(base_url=harness.base, timeout=None) as c:
        t0 = time.monotonic()
        r = await c.get("/auto/v101")
        assert r.status_code == 503 and time.monotonic() - t0 < 8 + 2
    assert not app.pool.in_use and not app.tracker.live
