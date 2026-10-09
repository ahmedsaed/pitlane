"""``directory`` provider (PROV-2, replay): a local folder of media files."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..models import Event
from ..replays.parsing import parse_name, resolve_events
from .base import BaseProvider, OpenedInput, ProviderError, ReplayItemSpec

MEDIA_SUFFIXES = {".ts", ".mkv", ".mp4", ".m4v", ".mov", ".m2ts", ".mts", ".avi", ".webm"}


class DirectoryProvider(BaseProvider):
    capabilities = {"replay"}

    def _files(self) -> list[Path]:
        root = Path(self.cfg.path)
        if not root.is_dir():
            return []
        it = root.rglob("*") if self.cfg.recursive else root.glob("*")
        return sorted(
            p
            for p in it
            if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES and not any(part.startswith(".") for part in p.relative_to(root).parts)
        )

    async def find_replays(self, event: Event) -> list[ReplayItemSpec]:
        """Files whose parsed name resolves to ``event`` (docs/providers.md)."""
        files = await asyncio.to_thread(self._files)
        out = []
        root = Path(self.cfg.path)
        for f in files:
            parsed = parse_name(f.name)
            if parsed.episode:
                season, ep = parsed.episode
                hit = season == event.season and ep // 100 == event.round
            else:
                hit = event in resolve_events(parsed, [event])
            if hit:
                out.append(
                    ReplayItemSpec(
                        provider_item_id=str(f.relative_to(root)),
                        session_type=parsed.session_type,
                        title=f.stem,
                        locator=str(f),
                        kind="file",
                    )
                )
        return out

    async def open_replay(self, locator: str) -> OpenedInput:
        root = Path(self.cfg.path).resolve()
        p = Path(locator).resolve()
        if root not in p.parents or not p.is_file():
            raise ProviderError("file is not inside the provider directory")
        return OpenedInput(url_or_path=str(p))
