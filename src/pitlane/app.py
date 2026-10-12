"""Application wiring: one asyncio process (architecture.md "Process model")."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import signal
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from .calendar.jolpica import BASE_URL as JOLPICA_URL
from .calendar.jolpica import Calendar, JolpicaClient
from .channels import ChannelMap, Source
from .connections import ConnectionLimiter
from .config import Config, ConfigError, config_warnings, is_loopback_host, load_config, split_bind
from .health import HealthChecker, near_session
from .http import FetchClient
from .logging import get_logger
from .metrics import Metrics
from .providers.registry import ProviderRegistry
from .relay import ProcessTracker, RelayHooks, TunerPool
from .state import State
from .tuner import tuner_router
from .xmltv import etag_for, render_xmltv, session_window

log = get_logger("app")

CALENDAR_REFRESH = timedelta(hours=6)


def public_bind_addresses(bind_host: str) -> list[str]:
    """NFR-8: addresses Pitlane would listen on that are publicly routable."""
    if bind_host in ("", "0.0.0.0", "::", "[::]"):
        try:
            import psutil

            addrs = [a.address.split("%")[0] for ifs in psutil.net_if_addrs().values() for a in ifs]
        except Exception:  # pragma: no cover
            return []
    else:
        addrs = [bind_host]
    out = []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_global:
            out.append(a)
    return out


class App:
    def __init__(
        self,
        cfg: Config,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        jolpica_url: str = JOLPICA_URL,
        start_background: bool = True,
        notify_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.notify_transport = notify_transport
        self.clock = clock or (lambda: datetime.now(UTC))
        self.jolpica_url = jolpica_url
        self.start_background = start_background
        self.data_dir = Path(cfg.server.data_dir)
        self.metrics = Metrics()
        self.tracker = ProcessTracker(self.metrics)
        self.http = FetchClient(transport=http_transport)
        self.state: State = None  # type: ignore[assignment]
        self.calendars: dict[str, Calendar] = {}
        self.registry: ProviderRegistry = None  # type: ignore[assignment]
        self.channel_map: ChannelMap = None  # type: ignore[assignment]
        self.health: HealthChecker = None  # type: ignore[assignment]
        self.pool = TunerPool(cfg.tuners.count)
        self.connections = ConnectionLimiter(cfg.connection_limits())
        self.replays: Any = None
        self.vod: Any = None
        self.jolpica: JolpicaClient = None  # type: ignore[assignment]
        self.reload_error: list[str] | None = None
        self.notifier: Any = None
        self._tasks: list[asyncio.Task] = []
        self._xmltv_cache: tuple[Any, bytes, str] | None = None
        self._reload_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        self._start_notifier()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = State(self.data_dir / "pitlane.db")
        self.jolpica = JolpicaClient(self.http, self.data_dir / "jolpica", base_url=self.jolpica_url)
        for w in config_warnings(self.cfg):
            log.warning(w)
        self._check_bind()
        await self._load_calendars(use_network=False)
        self.channel_map = ChannelMap(self.state)
        await self._start_providers()
        self.health = HealthChecker(
            self.channel_map,
            self.registry,
            self.metrics,
            self.cfg.health.interval,
            self.cfg.health.interval_near_session,
            is_near_session=self._near_session,
            limiter=self.connections,
        )
        if self.cfg.replays_enabled:
            from .replays.pipeline import ReplayPipeline

            self.replays = ReplayPipeline(self)
            await self.replays.start(background=self.start_background)
        await self._start_vod()
        if self.start_background:
            self.health.start()
            self._spawn(self._calendar_loop(), "calendar")
            self._spawn(self._provider_refresh_loop(), "provider-refresh")
        log.info("pitlane started", extra={"channels": len(self.cfg.channels), "providers": len(self.cfg.provider_cfgs)})

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self.health:
            await self.health.stop()
        if self.replays:
            await self.replays.stop()
        if self.vod:
            await self.vod.stop()
        await self.tracker.kill_all()  # LIVE-9: never leave ffmpeg behind
        if self.registry:
            await self.registry.stop()
        await self.http.aclose()
        await self._stop_notifier()
        if self.state:
            self.state.close()

    def _start_notifier(self) -> None:
        """OPS-7: forward warnings and errors to ntfy, if configured."""
        from .notify import handler_from_config

        self.notifier = handler_from_config(self.cfg, transport=self.notify_transport)
        if self.notifier is not None:
            self.notifier.start()
            logging.getLogger().addHandler(self.notifier)

    async def _stop_notifier(self) -> None:
        if self.notifier is not None:
            logging.getLogger().removeHandler(self.notifier)
            notifier, self.notifier = self.notifier, None
            await notifier.stop()

    async def _start_vod(self) -> None:
        """docs/vod.md: movies and shows, when ``vod`` is configured."""
        if self.cfg.vod is None or self.vod is not None:
            return
        from .vod.manager import VodManager

        self.vod = VodManager(self)
        await self.vod.start(background=self.start_background)

    def _spawn(self, coro: Any, name: str) -> None:
        self._tasks.append(asyncio.get_running_loop().create_task(coro, name=name))

    def _check_bind(self) -> None:
        host, _ = split_bind(self.cfg.server.bind)
        public = public_bind_addresses(host)
        if public:
            # NFR-8: the HDHomeRun API has no authentication.
            log.warning(
                "!!! PITLANE IS LISTENING ON A PUBLIC INTERFACE !!! The tuner API is unauthenticated; "
                "bind to a LAN address or firewall the port.",
                extra={"bind": self.cfg.server.bind, "public_addresses": public},
            )
        # QA-012: warn only; reverse-proxy auth cannot be detected.
        if not is_loopback_host(host) and not self.cfg.server.ui_password:
            log.warning("web UI is reachable beyond localhost without server.ui_password; use reverse-proxy auth")

    async def _start_providers(self) -> None:
        self.registry = ProviderRegistry(self.cfg, self.http, self.data_dir, calendar=self.calendar)
        await self.registry.start()
        self.channel_map.load_channels(self.cfg)
        await self.channel_map.refresh_from(self.registry)

    @property
    def calendar(self) -> Calendar:
        """Calendar of the first configured series (v1: Formula 1 only)."""
        return next(iter(self.calendars.values()), Calendar())

    def _nominal(self, t, default):  # type: ignore[no-untyped-def]
        return self.cfg.epg.nominal_minutes(t, default)

    async def _load_calendars(self, use_network: bool) -> None:
        cals = {}
        for key, series in self.cfg.series.items():
            try:
                cal = await self.jolpica.load_calendar(
                    series.seasons, now=self.clock(), nominal=self._nominal, use_network=use_network
                )
            except Exception as e:
                log.error("calendar load failed", extra={"series": key, "error": str(e)})
                cal = self.calendars.get(key, Calendar())
            if not cal.events and key in self.calendars:
                cal = self.calendars[key]  # keep what we had
            cals[key] = cal
        self.calendars = cals
        self._xmltv_cache = None
        if self.registry:
            self.registry.calendar = self.calendar
            for p in self.registry.providers.values():
                if hasattr(p, "ctx"):
                    p.ctx.calendar = self.calendar

    async def refresh_calendar(self) -> None:
        """EPG-1: refresh now (also used by the UI's on-demand action)."""
        await self._load_calendars(use_network=True)

    async def _calendar_loop(self) -> None:
        while True:
            try:
                await self.refresh_calendar()
            except Exception:  # pragma: no cover
                log.exception("calendar refresh failed")
            await asyncio.sleep(CALENDAR_REFRESH.total_seconds())

    async def _provider_refresh_loop(self) -> None:
        intervals = [getattr(p, "refresh", None) for p in self.cfg.provider_cfgs]
        every = min([i.total_seconds() for i in intervals if isinstance(i, timedelta)] or [6 * 3600])
        while True:
            await asyncio.sleep(max(60.0, every))
            try:
                await self.channel_map.refresh_from(self.registry)
                self.health.wake()
            except Exception:  # pragma: no cover
                log.exception("provider refresh failed")

    def _near_session(self, now: datetime) -> bool:
        return near_session(now, self.calendar.sessions(), lambda s: session_window(s, self.cfg))

    # ------------------------------------------------------------------ reload (OPS-3)
    async def reload(self, path: str | None = None) -> list[str]:
        """Validate the new file first; keep the old config if it is invalid."""
        async with self._reload_lock:
            try:
                new = load_config(path or self.cfg.source_path)
            except ConfigError as e:
                self.reload_error = e.errors
                for err in e.errors:
                    log.error("config reload rejected", extra={"error": err})
                return e.errors
            self.reload_error = None
            old_registry = self.registry
            old_notify = self.cfg.notify
            self.cfg = new
            if new.notify != old_notify:
                await self._stop_notifier()
                self._start_notifier()
            self.pool.count = new.tuners.count
            self.connections.configure(new.connection_limits())
            self.health.interval = new.health.interval
            self.health.interval_near = new.health.interval_near_session
            await self._load_calendars(use_network=False)
            # Active streams keep their current source objects and processes.
            self.registry = ProviderRegistry(self.cfg, self.http, self.data_dir, calendar=self.calendar)
            await self.registry.start()
            self.health.registry = self.registry
            self.channel_map.load_channels(self.cfg)
            await self.channel_map.refresh_from(self.registry)
            if old_registry:
                await old_registry.stop()
            if self.replays:
                self.replays.reconfigure()
            if self.vod and new.vod is None:
                await self.vod.stop()
                self.vod = None
            elif self.vod:
                self.vod.wake()  # reads app.cfg.vod and the new registry on its next pass
            else:
                await self._start_vod()
            self.health.wake()
            self._xmltv_cache = None
            log.info("config reloaded")
            return []

    # ------------------------------------------------------------------ helpers
    def base_url(self, request: Request | None = None) -> str:
        if self.cfg.server.advertised_url:
            return self.cfg.server.advertised_url
        if request is not None:
            return str(request.base_url).rstrip("/")
        host, port = split_bind(self.cfg.server.bind)
        return f"http://{host}:{port}"

    def relay_hooks(self) -> RelayHooks:
        def mark(src: Source, state: str, error: str | None) -> None:
            self.health.mark(src, state, error)

        return RelayHooks(
            get_provider=lambda name: self.registry.get(name),
            mark_source=mark,
            reprobe=lambda src: self.health.reprobe_soon(src),
            http_client=self.http.raw,
            probe=lambda src: self.health.probe_source(src, preemptible=False),
            limiter=self.connections,
        )

    def xmltv(self, base_url: str) -> tuple[bytes, str]:
        now = self.clock()
        key = (now.date(), now.hour, base_url, id(self.cfg), tuple(c.updated_at for c in self.calendars.values()))
        if self._xmltv_cache and self._xmltv_cache[0] == key:
            return self._xmltv_cache[1], self._xmltv_cache[2]
        body = render_xmltv(self.cfg, self.calendars, now, base_url)
        etag = etag_for(body)
        self._xmltv_cache = (key, body, etag)
        return body, etag


def create_app(
    cfg: Config | None = None,
    *,
    config_path: str | None = None,
    app_obj: App | None = None,
    install_signal_handlers: bool = True,
    **kwargs: Any,
) -> FastAPI:
    if app_obj is None:
        if cfg is None:
            cfg = load_config(config_path)
        app_obj = App(cfg, **kwargs)
    pl = app_obj

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await pl.startup()
        loop = asyncio.get_running_loop()
        if install_signal_handlers:
            try:
                loop.add_signal_handler(signal.SIGHUP, lambda: loop.create_task(pl.reload()))
            except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
                pass
        try:
            yield
        finally:
            if install_signal_handlers:
                try:
                    loop.remove_signal_handler(signal.SIGHUP)
                except Exception:  # pragma: no cover
                    pass
            await pl.shutdown()

    api = FastAPI(title="Pitlane", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.state.pitlane = pl
    api.include_router(tuner_router(pl))

    @api.get("/xmltv.xml")
    async def xmltv(request: Request):
        body, etag = pl.xmltv(pl.base_url(request))
        headers = {"Cache-Control": "max-age=900", "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/xml; charset=utf-8", headers=headers)

    @api.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok"})  # OPS-2 liveness

    @api.get("/metrics")
    async def metrics():
        return Response(pl.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @api.get("/static/logos/{filename}")
    async def logo(filename: str):
        stem, _, _ = filename.partition(".")
        ch = pl.cfg.channel(stem)
        if ch is None or not ch.logo or ch.logo.startswith(("http://", "https://")):
            return PlainTextResponse("not found", status_code=404)
        p = Path(ch.logo)
        if not p.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(p)

    from .web.ui import ui_router
    from .web.vod_ui import vod_router

    api.include_router(ui_router(pl))
    api.include_router(vod_router(pl))
    return api
