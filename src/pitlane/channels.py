"""Channel map: logical channel → ordered sources, stable guide numbers (LIVE-2, LIVE-6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .logging import get_logger
from .providers.base import SourceKind, SourceSpec, source_id
from .state import State, now_iso

log = get_logger("channels")


@dataclass
class LogicalChannel:
    id: str
    guide_number: str
    name: str
    logo: str | None = None
    series: list[str] = field(default_factory=list)
    transcode: str | None = None
    loop: bool = False
    auto: bool = False


@dataclass
class Source:
    id: str
    channel_id: str
    provider: str
    locator: str
    kind: SourceKind
    label: str
    priority: int
    state: str = "unknown"  # online | degraded | offline | unknown (not yet probed)
    latency_ms: int | None = None
    last_error: str | None = None
    last_ok_at: str | None = None
    last_probe_at: str | None = None


_TIER = {"online": 0, "degraded": 1, "unknown": 2}


class ChannelMap:
    def __init__(self, state: State) -> None:
        self.state = state
        self.channels: dict[str, LogicalChannel] = {}
        self.sources: dict[str, Source] = {}

    def load_channels(self, cfg: Config) -> None:
        chans = {
            c.id: LogicalChannel(c.id, c.guide_number, c.name, c.logo, list(c.series), c.transcode, c.loop)
            for c in cfg.channels
        }
        for ch in self.channels.values():
            if ch.auto and ch.id not in chans:
                chans[ch.id] = ch
        self.channels = chans

    def apply_sources(self, specs: list[tuple[str, SourceSpec]]) -> None:
        """Replace the source list, keeping health state for sources whose id is unchanged."""
        new: dict[str, Source] = {}
        taken = {c.guide_number for c in self.channels.values()}
        for provider, spec in specs:
            if spec.channel not in self.channels:
                if spec.auto_channel_name:
                    gn = self.state.auto_channel_number(spec.channel, spec.auto_channel_name, taken)
                    taken.add(gn)
                    self.channels[spec.channel] = LogicalChannel(spec.channel, gn, spec.auto_channel_name, auto=True)
                else:
                    log.warning(
                        "source refers to an unknown channel; ignored",
                        extra={"provider": provider, "channel": spec.channel},
                    )
                    continue
            sid = source_id(provider, spec.locator)
            old = self.sources.get(sid)
            src = Source(sid, spec.channel, provider, spec.locator, spec.kind, spec.label, spec.priority)
            if old is not None:
                src.state, src.latency_ms, src.last_error = old.state, old.latency_ms, old.last_error
                src.last_ok_at, src.last_probe_at = old.last_ok_at, old.last_probe_at
            new[sid] = src
        self.sources = new
        self._persist()

    async def refresh_from(self, registry: Any) -> None:
        specs: list[tuple[str, SourceSpec]] = []
        for p in registry.live():
            try:
                if hasattr(p, "refresh"):
                    await p.refresh()
                for spec in await p.list_sources():
                    specs.append((p.name, spec))
            except Exception as e:
                log.error("provider list_sources failed", extra={"provider": p.name, "error": str(e)})
                # keep that provider's previous sources
                specs.extend(
                    (s.provider, SourceSpec(s.channel_id, s.locator, s.kind, s.label, s.priority))
                    for s in self.sources.values()
                    if s.provider == p.name
                )
        self.apply_sources(specs)

    def _persist(self) -> None:
        for s in self.sources.values():
            self.state.execute(
                "INSERT INTO sources(id, channel_id, provider, priority, kind, label, state, last_ok_at, last_error, latency_ms, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET channel_id=excluded.channel_id, "
                "priority=excluded.priority, label=excluded.label, state=excluded.state, last_ok_at=excluded.last_ok_at, "
                "last_error=excluded.last_error, latency_ms=excluded.latency_ms, updated_at=excluded.updated_at",
                (s.id, s.channel_id, s.provider, s.priority, s.kind, s.label, s.state, s.last_ok_at, s.last_error, s.latency_ms, now_iso()),
            )

    def persist_source(self, s: Source) -> None:
        self.state.execute(
            "UPDATE sources SET state=?, last_ok_at=?, last_error=?, latency_ms=?, updated_at=? WHERE id=?",
            (s.state, s.last_ok_at, s.last_error, s.latency_ms, now_iso(), s.id),
        )

    def by_guide_number(self, gn: str) -> LogicalChannel | None:
        for c in self.channels.values():
            if c.guide_number == gn:
                return c
        return None

    def channel_sources(self, channel_id: str) -> list[Source]:
        return sorted((s for s in self.sources.values() if s.channel_id == channel_id), key=lambda s: (s.priority, s.id))

    def ordered_sources(self, channel_id: str) -> list[Source]:
        """LIVE-6: first ``online`` sources in priority order, then ``degraded``.

        Sources not yet probed (``unknown``) come last. ``offline`` sources are only
        re-probed as a last resort at tune time (see :meth:`offline_sources`).
        """
        # LIVE-6 (QA-008 answered): offline sources are handled by offline_sources().
        return sorted(
            (s for s in self.channel_sources(channel_id) if s.state in _TIER),
            key=lambda s: (_TIER[s.state], s.priority, s.id),
        )

    def offline_sources(self, channel_id: str) -> list[Source]:
        """LIVE-6: offline sources in priority order, for re-probing at tune time."""
        return [s for s in self.channel_sources(channel_id) if s.state == "offline"]

    def lineup(self) -> list[LogicalChannel]:
        """Channels with at least one source, including all-offline ones (docs/tuner.md)."""
        with_sources = {s.channel_id for s in self.sources.values()}
        return sorted(
            (c for c in self.channels.values() if c.id in with_sources),
            key=lambda c: (float(c.guide_number), c.guide_number),
        )
