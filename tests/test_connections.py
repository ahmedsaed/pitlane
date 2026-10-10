"""LIVE-10: per-provider connection limits (unit tests for the limiter)."""

from __future__ import annotations

import asyncio

from pitlane.config import parse_config
from pitlane.connections import ConnectionLimiter


async def test_unlimited_provider_never_blocks():
    lim = ConnectionLimiter({})
    leases = [await lim.acquire_stream("any", 0, wait_for_streams=False) for _ in range(5)]
    assert all(lease is not None for lease in leases)
    async with lim.probe_slot("any") as p:
        assert p is not None


async def test_stream_slot_is_exclusive_and_released():
    lim = ConnectionLimiter({"iptv": 1})
    a = await lim.acquire_stream("iptv", 1, wait_for_streams=False)
    assert a is not None and lim.in_use("iptv") == 1
    # Busy with a stream: no wait unless asked.
    assert await lim.acquire_stream("iptv", 5, wait_for_streams=False) is None
    assert await lim.acquire_stream("iptv", 0.1, wait_for_streams=True) is None

    async def later():
        await asyncio.sleep(0.1)
        a.release()

    asyncio.get_running_loop().create_task(later())
    b = await lim.acquire_stream("iptv", 2, wait_for_streams=True)  # channel change: old stream closes
    assert b is not None and lim.in_use("iptv") == 1
    b.release()
    b.release()  # idempotent
    assert lim.in_use("iptv") == 0


async def test_closing_input_holds_slot_until_released():
    lim = ConnectionLimiter({"iptv": 1})
    a = await lim.acquire_stream("iptv", 1, wait_for_streams=False)
    a.closing()
    asyncio.get_running_loop().call_later(0.1, a.release)
    # Not a live stream any more, so failover waits for it instead of giving up.
    b = await lim.acquire_stream("iptv", 2, wait_for_streams=False)
    assert b is not None


async def test_probes_are_skipped_while_streaming():
    lim = ConnectionLimiter({"iptv": 1})
    a = await lim.acquire_stream("iptv", 1, wait_for_streams=False)
    async with lim.probe_slot("iptv") as p:
        assert p is None
    a.release()
    async with lim.probe_slot("iptv") as p:
        assert p is not None and lim.in_use("iptv") == 1
    assert lim.in_use("iptv") == 0


async def test_probes_take_turns():
    lim = ConnectionLimiter({"iptv": 1})
    active = 0
    peak = 0

    async def probe():
        nonlocal active, peak
        async with lim.probe_slot("iptv") as p:
            assert p is not None
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(probe() for _ in range(4)))
    assert peak == 1


async def test_stream_preempts_running_probe():
    lim = ConnectionLimiter({"iptv": 1})
    started = asyncio.Event()

    async def probe():
        async with lim.probe_slot("iptv") as p:
            p.task = asyncio.current_task()
            started.set()
            await asyncio.sleep(30)

    t = asyncio.get_running_loop().create_task(probe())
    await started.wait()
    lease = await asyncio.wait_for(lim.acquire_stream("iptv", 5, wait_for_streams=False), 1)
    assert lease is not None and t.cancelled()


async def test_tune_time_probe_is_not_preempted():
    lim = ConnectionLimiter({"iptv": 1})
    async with lim.probe_slot("iptv", preemptible=False) as p:
        p.task = asyncio.current_task()
        assert await lim.acquire_stream("iptv", 0.1, wait_for_streams=False) is None


def test_config_max_connections():
    cfg = parse_config(
        {
            "providers": [
                {"type": "m3u", "name": "iptv", "path": "/x.m3u", "max_connections": 1},
                {"type": "channel-json", "name": "mine", "path": "/c.json"},
            ]
        },
        environ={},
    )
    assert cfg.connection_limits() == {"iptv": 1}
