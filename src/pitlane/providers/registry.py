"""Provider registry: instantiate configured providers, load local plugins (PROV-3, PROV-6)."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

from ..config import Config
from ..http import FetchClient, FetchPolicy
from ..logging import get_logger
from .base import BaseProvider, ProviderContext, ProviderError
from .channel_json import ChannelJsonProvider
from .directory import DirectoryProvider
from .hdhomerun import HdhomerunProvider
from .m3u import M3uProvider
from .xtream import XtreamProvider

log = get_logger("providers")

BUILTINS: dict[str, type] = {
    "m3u": M3uProvider,
    "channel-json": ChannelJsonProvider,
    "directory": DirectoryProvider,
    "hdhomerun": HdhomerunProvider,
    "xtream": XtreamProvider,
}


def _declared_type(path: Path) -> str | None:
    """Read ``PROVIDER_TYPE = "..."`` statically, without executing the module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as e:
        log.warning("plugin unreadable", extra={"plugin": str(path), "error": str(e)})
        return None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "PROVIDER_TYPE" for t in targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
    return None


def load_plugins(plugins_dir: Path, enabled: list[str]) -> dict[str, type]:
    """Load ``<plugins_dir>/*.py`` modules whose PROVIDER_TYPE is in ``enabled`` (PROV-6).

    Only local files are considered; nothing is downloaded. Modules for types
    that are not enabled are never imported.
    """
    found: dict[str, type] = {}
    if not enabled or not plugins_dir.is_dir():
        return found
    for path in sorted(plugins_dir.glob("*.py")):
        ptype = _declared_type(path)
        if ptype is None or ptype not in enabled:
            continue
        if ptype in BUILTINS:
            log.warning("plugin type shadows a built-in; ignored", extra={"plugin": str(path), "type": ptype})
            continue
        mod_name = f"pitlane_plugin_{path.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            log.error("plugin failed to load", extra={"plugin": str(path), "error": str(e)})
            sys.modules.pop(mod_name, None)
            continue
        cls = getattr(module, "PROVIDER_CLASS", None)
        if cls is None:
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if obj.__module__ == mod_name and hasattr(obj, "start") and (
                    hasattr(obj, "list_sources") or hasattr(obj, "find_replays")
                ):
                    cls = obj
                    break
        if cls is None:
            log.error("plugin has no provider class", extra={"plugin": str(path)})
            continue
        found[ptype] = cls
        log.info("plugin loaded", extra={"plugin": str(path), "type": ptype})
    return found


class ProviderRegistry:
    def __init__(self, cfg: Config, http: FetchClient, data_dir: Path, calendar: Any = None) -> None:
        self.cfg = cfg
        self.http = http
        self.data_dir = data_dir
        self.calendar = calendar
        self.providers: dict[str, Any] = {}
        self.errors: dict[str, str] = {}

    def _ctx(self, pcfg: Any) -> ProviderContext:
        from ..logging import secrets

        def resolve_secret(ref: str) -> str:
            # Same syntax as the config file (${ENV} / ${file:/path}).
            from ..config import _Resolver
            import os

            r = _Resolver(dict(os.environ))
            value = r.resolve(ref, "provider-secret")
            if r.errors:
                raise ProviderError(r.errors[0])
            secrets.add(value)
            return value

        cache = self.data_dir / "providers" / pcfg.name
        cache.mkdir(parents=True, exist_ok=True)
        return ProviderContext(
            http=self.http,
            policy=FetchPolicy.from_cfg(pcfg.http),
            logger=get_logger(f"provider.{pcfg.name}"),
            cache_dir=cache,
            resolve_secret=resolve_secret,
            probe_timeout=self.cfg.health.probe_timeout.total_seconds(),
            degraded_latency_ms=self.cfg.health.degraded_latency_ms,
            calendar=self.calendar,
        )

    async def start(self) -> None:
        plugin_types = load_plugins(self.cfg.plugins_dir, self.cfg.plugins.enabled)
        for pcfg in self.cfg.provider_cfgs:
            cls = BUILTINS.get(pcfg.type) or plugin_types.get(pcfg.type)
            if cls is None:
                self.errors[pcfg.name] = f'provider type "{pcfg.type}" not available'
                log.error("provider type not available", extra={"provider": pcfg.name, "type": pcfg.type})
                continue
            try:
                inst = cls(pcfg.name) if issubclass(cls, BaseProvider) else cls()
                if not getattr(inst, "name", None):
                    inst.name = pcfg.name
                await inst.start(pcfg, self._ctx(pcfg))
                vod = self.cfg.vod
                if vod is not None and vod.play_channel is not None and vod.provider == pcfg.name and hasattr(inst, "play_channel"):
                    from ..config import PLAY_CHANNEL_ID

                    inst.play_channel = PLAY_CHANNEL_ID
                self.providers[pcfg.name] = inst
            except Exception as e:
                self.errors[pcfg.name] = str(e)
                log.error("provider failed to start", extra={"provider": pcfg.name, "error": str(e)})

    async def stop(self) -> None:
        for p in self.providers.values():
            try:
                await p.stop()
            except Exception as e:  # pragma: no cover
                log.warning("provider stop failed", extra={"provider": p.name, "error": str(e)})
        self.providers.clear()

    def live(self) -> list[Any]:
        return [p for p in self.providers.values() if "live" in p.capabilities]

    def replay(self) -> list[Any]:
        return [p for p in self.providers.values() if "replay" in p.capabilities]

    def get(self, name: str) -> Any:
        return self.providers.get(name)
