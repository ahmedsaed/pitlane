"""``hdhomerun`` provider (PROV-2): passthrough of a real HDHomeRun device's lineup."""

from __future__ import annotations

import re
import time

from .base import BaseProvider, OpenedInput, ProbeResult, ProviderError, SourceKind, SourceSpec


class HdhomerunProvider(BaseProvider):
    capabilities = {"live"}

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.lineup: dict[str, dict] = {}  # locator (URL) → lineup entry

    @property
    def device(self) -> str:
        return self.cfg.device.rstrip("/")

    async def refresh(self) -> None:
        try:
            resp = await self.ctx.http.get(f"{self.device}/lineup.json", policy=self.ctx.policy)
            data = resp.json()
            if not isinstance(data, list):
                raise ProviderError("lineup.json is not a list")
        except Exception as e:
            self.ctx.logger.warning("hdhomerun lineup failed", extra={"provider": self.name, "error": str(e)})
            if not self.lineup:
                raise ProviderError(f"{self.name}: cannot read lineup: {e}") from None
            return
        self.lineup = {
            str(item["URL"]): item
            for item in data
            if isinstance(item, dict) and isinstance(item.get("URL"), str) and item["URL"].startswith("http")
        }

    async def list_sources(self) -> list[SourceSpec]:
        if not self.lineup:
            await self.refresh()
        out = []
        for url, item in self.lineup.items():
            fields = {"GuideNumber": str(item.get("GuideNumber", "")), "GuideName": str(item.get("GuideName", ""))}
            for rule in self.cfg.map:
                if all(re.search(p, fields.get(k, "")) for k, p in rule.match.items()):
                    out.append(
                        SourceSpec(
                            channel=rule.channel,
                            locator=url,
                            kind="tuner",
                            label=f"{fields['GuideNumber']} {fields['GuideName']}".strip(),
                            priority=rule.priority if rule.priority is not None else 100,
                        )
                    )
        return out

    def kind_of(self, locator: str) -> SourceKind:
        return "tuner"

    async def open(self, locator: str) -> OpenedInput:
        if locator not in self.lineup:
            raise ProviderError(f"{self.name}: channel no longer in device lineup")
        return OpenedInput(url_or_path=locator)

    async def probe(self, locator: str) -> ProbeResult:
        """Check the device answers, without opening a stream (which would take a real tuner)."""
        t0 = time.monotonic()
        try:
            resp = await self.ctx.http.raw.get(f"{self.device}/discover.json", timeout=self.ctx.probe_timeout)
            if resp.status_code != 200:
                raise ProviderError(f"discover.json HTTP {resp.status_code}")
        except Exception as e:
            return ProbeResult("offline", None, str(e) or e.__class__.__name__)
        if locator not in self.lineup:
            return ProbeResult("offline", None, "channel not in device lineup")
        ms = int((time.monotonic() - t0) * 1000)
        return ProbeResult("degraded" if ms > self.ctx.degraded_latency_ms else "online", ms, None)
