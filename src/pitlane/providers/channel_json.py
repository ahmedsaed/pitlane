"""``channel-json`` provider (PROV-2) – the documented JSON format, schema in
``schemas/channel-json.v1.json``."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .base import BaseProvider, OpenedInput, ProviderError, SourceKind, SourceSpec


class ChannelEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")  # "Unknown fields are ignored."

    channel: str
    label: str
    kind: Literal["hls", "ts"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    priority: int = 100
    enabled: bool = True

    @field_validator("channel")
    @classmethod
    def _ch(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9-]+$", v):
            raise ValueError("must match [a-z0-9-]+")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not re.match(r"^https?://", v, re.IGNORECASE):
            raise ValueError("only http/https URLs are allowed")
        return v

    @field_validator("headers", mode="before")
    @classmethod
    def _headers(cls, v: Any) -> Any:
        if isinstance(v, dict) and not all(isinstance(x, str) for x in v.values()):
            raise ValueError("header values must be strings")
        return v


class ChannelDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: Literal[1]
    channels: list[dict[str, Any]]


def parse_channel_json(text: str) -> tuple[list[ChannelEntry], list[str]]:
    """Return (valid entries, errors). A wrong ``version`` rejects the whole document."""
    try:
        doc = ChannelDoc.model_validate(json.loads(text))
    except json.JSONDecodeError as e:
        raise ProviderError(f"invalid JSON: {e}") from None
    except ValidationError as e:
        raise ProviderError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from None
    entries, errors = [], []
    for i, raw in enumerate(doc.channels):
        try:
            entries.append(ChannelEntry.model_validate(raw))
        except ValidationError as e:
            errors.extend(f"channels[{i}].{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
    return entries, errors


class ChannelJsonProvider(BaseProvider):
    capabilities = {"live"}

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.entries: dict[str, ChannelEntry] = {}

    async def refresh(self) -> None:
        try:
            if self.cfg.path:
                text = await asyncio.to_thread(Path(self.cfg.path).read_text, encoding="utf-8")
            else:
                text = (await self.ctx.http.get(self.cfg.url, policy=self.ctx.policy)).text
            entries, errors = parse_channel_json(text)
        except (OSError, ProviderError, Exception) as e:
            self.ctx.logger.warning("channel-json load failed", extra={"provider": self.name, "error": str(e)})
            if not self.entries:
                raise ProviderError(f"{self.name}: {e}") from None
            return
        for err in errors:
            self.ctx.logger.warning("channel-json entry skipped", extra={"provider": self.name, "error": err})
        # Locator = index-independent key: "<channel>|<url>" (opaque to others).
        self.entries = {f"{e.channel}|{e.url}": e for e in entries if e.enabled}

    async def list_sources(self) -> list[SourceSpec]:
        if not self.entries:
            await self.refresh()
        return [
            SourceSpec(channel=e.channel, locator=loc, kind=e.kind, label=e.label, priority=e.priority)
            for loc, e in self.entries.items()
        ]

    def kind_of(self, locator: str) -> SourceKind:
        e = self.entries.get(locator)
        if e is None:
            raise ProviderError("unknown locator")
        return e.kind

    async def open(self, locator: str) -> OpenedInput:
        e = self.entries.get(locator)
        if e is None:
            raise ProviderError(f"{self.name}: source no longer listed")
        return OpenedInput(url_or_path=e.url, headers=dict(e.headers))
