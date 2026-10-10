"""Health checker (LIVE-5): probes every source on an adaptive schedule."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from .channels import ChannelMap, Source
from .connections import ConnectionLimiter
from .logging import get_logger
from .metrics import STATE_VALUE, Metrics
from .providers.base import ProbeResult
from .state import now_iso

log = get_logger("health")

NEAR_SESSION = timedelta(minutes=30)


def near_session(now: datetime, sessions: list[Any], window_for: Callable[[Any], tuple[datetime, datetime]]) -> bool:
    """True within 30 min of a session's (padded) window."""
    for s in sessions:
        start, stop = window_for(s)
        if start - NEAR_SESSION <= now <= stop + NEAR_SESSION:
            return True
    return False


def health_checks_enabled(provider: Any) -> bool:
    """LIVE-11: ``health_checks: false`` on a provider turns off every probe of its sources."""
    cfg = getattr(provider, "cfg", None)
    value = cfg.get("health_checks", True) if isinstance(cfg, dict) else getattr(cfg, "health_checks", True)
    return value is not False


class HealthChecker:
    def __init__(
        self,
        channel_map: ChannelMap,
        registry: Any,
        metrics: Metrics,
        interval: timedelta,
        interval_near: timedelta,
        is_near_session: Callable[[datetime], bool] = lambda now: False,
        concurrency: int = 8,
        limiter: ConnectionLimiter | None = None,
    ) -> None:
        self.map = channel_map
        self.registry = registry
        self.metrics = metrics
        self.interval = interval
        self.interval_near = interval_near
        self.is_near_session = is_near_session
        self.limiter = limiter or ConnectionLimiter()
        self._sem = asyncio.Semaphore(concurrency)
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    def current_interval(self, now: datetime | None = None) -> timedelta:
        now = now or datetime.now(UTC)
        return self.interval_near if self.is_near_session(now) else self.interval

    async def probe_source(self, src: Source, *, preemptible: bool = True) -> ProbeResult | None:
        """Probe and record. Returns ``None`` without recording when the provider's
        connection limit leaves no slot (LIVE-10), a stream preempted the probe, or the
        provider has health checks turned off (LIVE-11)."""
        provider = self.registry.get(src.provider)
        async with self._sem:
            if provider is None:
                result = ProbeResult("offline", None, "provider not running")
            elif not health_checks_enabled(provider):
                return None
            else:
                async with self.limiter.probe_slot(src.provider, preemptible=preemptible) as lease:
                    if lease is None:
                        log.debug("probe skipped: provider connections in use", extra={"source": src.id})
                        return None
                    inner = asyncio.get_running_loop().create_task(provider.probe(src.locator))
                    lease.task = inner
                    try:
                        result = await inner
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if inner.cancelled() and not (current and current.cancelling()):
                            log.debug("probe preempted by a stream", extra={"source": src.id})
                            return None
                        inner.cancel()
                        raise
                    except Exception as e:
                        result = ProbeResult("offline", None, str(e) or e.__class__.__name__)
        self.record(src, result)
        return result

    def record(self, src: Source, result: ProbeResult) -> None:
        prev = src.state
        src.state = result.state
        src.latency_ms = result.latency_ms
        src.last_error = result.error
        src.last_probe_at = now_iso()
        if result.state in ("online", "degraded"):
            src.last_ok_at = src.last_probe_at
        self.map.persist_source(src)
        self.metrics.source_state.labels(source=src.id, channel=src.channel_id).set(STATE_VALUE[result.state])
        if prev != result.state:
            log.info(
                "source state changed",
                extra={"source": src.id, "channel": src.channel_id, "from": prev, "to": result.state, "error": result.error},
            )

    def mark(self, src: Source, state: str, error: str | None) -> None:
        """Set state from the relay (e.g. stall → degraded) without a probe."""
        self.record(src, ProbeResult(state, src.latency_ms, error))  # type: ignore[arg-type]

    async def probe_all(self) -> None:
        await asyncio.gather(*(self.probe_source(s) for s in list(self.map.sources.values())))

    def reprobe_soon(self, src: Source) -> None:
        """docs/tuner.md: a source that failed during a stream is reprobed immediately."""
        t = asyncio.get_running_loop().create_task(self.probe_source(src))
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await self.probe_all()
            except Exception:  # pragma: no cover
                log.exception("health round failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.current_interval().total_seconds())
            except TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run(), name="health")

    async def stop(self) -> None:
        for t in [self._task, *self._pending]:
            if t:
                t.cancel()
        for t in [self._task, *self._pending]:
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None
