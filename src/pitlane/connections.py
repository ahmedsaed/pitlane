"""Per-provider connection limits (LIVE-10; docs/tuner.md "Connection limits").

Many IPTV accounts allow a single connection, and a second one either is refused
or kicks the first off. A provider with ``max_connections`` set gets a lease for
every upstream connection Pitlane opens to it: one per stream input and one per
health probe.

- Streams win over probes: a stream that needs a slot cancels running health
  probes on that provider and waits for them to let go.
- A provider whose slots are all streaming is never probed; the live stream is
  the health signal (the relay marks failures as they happen).
- A slot is released only after the old input has fully closed, so failover
  between two sources of the same provider never overlaps connections.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

LeaseKind = Literal["stream", "probe", "closing"]


class Lease:
    def __init__(self, limiter: "ConnectionLimiter | None", provider: str, kind: LeaseKind, preemptible: bool = False):
        self.limiter = limiter
        self.provider = provider
        self.kind: LeaseKind = kind
        self.preemptible = preemptible
        self.task: asyncio.Task | None = None  # cancelled when a stream preempts this probe
        self._released = False

    def closing(self) -> None:
        """The connection is being torn down; keep the slot until :meth:`release`."""
        self.kind = "closing"

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.limiter is not None:
            self.limiter._release(self)


class ConnectionLimiter:
    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self.limits: dict[str, int] = dict(limits or {})
        self._held: dict[str, list[Lease]] = {}
        self._waiting_streams: Counter[str] = Counter()
        self._changed: dict[str, asyncio.Event] = {}

    def configure(self, limits: dict[str, int]) -> None:
        """Apply new limits (config reload). Leases already held stay valid."""
        self.limits = dict(limits)
        for provider in list(self._changed):
            self._notify(provider)

    def limit(self, provider: str) -> int | None:
        return self.limits.get(provider)

    def in_use(self, provider: str) -> int:
        return len(self._held.get(provider, []))

    def _streams(self, provider: str) -> int:
        return sum(1 for lease in self._held.get(provider, []) if lease.kind == "stream")

    def _notify(self, provider: str) -> None:
        ev = self._changed.pop(provider, None)
        if ev is not None:
            ev.set()

    async def _wait_change(self, provider: str, timeout: float) -> bool:
        ev = self._changed.setdefault(provider, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def _grant(self, provider: str, kind: LeaseKind, preemptible: bool = False) -> Lease:
        lease = Lease(self, provider, kind, preemptible)
        self._held.setdefault(provider, []).append(lease)
        return lease

    def _release(self, lease: Lease) -> None:
        held = self._held.get(lease.provider, [])
        if lease in held:
            held.remove(lease)
        self._notify(lease.provider)

    async def acquire_stream(self, provider: str, timeout: float, *, wait_for_streams: bool) -> Lease | None:
        """A slot for a stream input, or ``None`` if none came free within ``timeout``.

        Probes holding a slot are cancelled; slots held by closing inputs are waited
        for. Slots held by other streams are waited for only with ``wait_for_streams``
        (otherwise ``None`` is returned at once so the caller can try other sources).
        """
        limit = self.limits.get(provider)
        if limit is None:
            return Lease(None, provider, "stream")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        self._waiting_streams[provider] += 1
        try:
            while True:
                held = self._held.get(provider, [])
                if len(held) < limit:
                    return self._grant(provider, "stream")
                if self._streams(provider) >= limit and not wait_for_streams:
                    return None
                for lease in held:
                    if lease.kind == "probe" and lease.preemptible and lease.task is not None:
                        lease.task.cancel()
                remaining = deadline - loop.time()
                if remaining <= 0 or not await self._wait_change(provider, remaining):
                    return None
        finally:
            self._waiting_streams[provider] -= 1

    @asynccontextmanager
    async def probe_slot(self, provider: str, *, preemptible: bool = True) -> AsyncIterator[Lease | None]:
        """A slot for one probe, waiting behind other probes. Yields ``None`` (skip the
        probe) while the provider's slots are taken by streams or a stream is waiting."""
        limit = self.limits.get(provider)
        if limit is None:
            yield Lease(None, provider, "probe")
            return
        lease: Lease | None = None
        while lease is None:
            if self._waiting_streams[provider] or self._streams(provider) >= limit:
                break
            if self.in_use(provider) < limit:
                lease = self._grant(provider, "probe", preemptible)
            else:
                await self._wait_change(provider, 60.0)
        try:
            yield lease
        finally:
            if lease is not None:
                lease.release()
