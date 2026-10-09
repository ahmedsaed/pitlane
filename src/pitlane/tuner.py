"""HDHomeRun-compatible HTTP API (LIVE-1, LIVE-4; docs/tuner.md "Endpoints")."""

from __future__ import annotations

from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .relay import RelayResponse, StreamSession

if TYPE_CHECKING:
    from .app import App


def tuner_router(app: "App") -> APIRouter:
    r = APIRouter()

    def discover(request: Request) -> dict:
        base = app.base_url(request)
        t = app.cfg.tuners
        return {
            "FriendlyName": t.device_name,
            "Manufacturer": "Pitlane",
            "ModelNumber": t.model_number,
            "FirmwareName": t.firmware_name,
            "FirmwareVersion": t.firmware_version,
            "DeviceID": app.state.device_id(),
            "DeviceAuth": "pitlane",
            "TunerCount": t.count,
            "BaseURL": base,
            "LineupURL": f"{base}/lineup.json",
        }

    @r.get("/discover.json")
    async def discover_json(request: Request):
        return JSONResponse(discover(request))

    @r.get("/lineup_status.json")
    async def lineup_status():
        return {"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]}

    def lineup(request: Request) -> list[dict]:
        base = app.base_url(request)
        return [
            {"GuideNumber": c.guide_number, "GuideName": c.name, "URL": f"{base}/auto/v{c.guide_number}"}
            for c in app.channel_map.lineup()
        ]

    @r.get("/lineup.json")
    async def lineup_json(request: Request):
        return JSONResponse(lineup(request))

    @r.get("/lineup.xml")  # QA-013: XML shape mirrors lineup.json
    async def lineup_xml(request: Request):
        items = "".join(
            f"<Program><GuideNumber>{escape(i['GuideNumber'])}</GuideNumber><GuideName>{escape(i['GuideName'])}</GuideName>"
            f"<URL>{escape(i['URL'])}</URL></Program>"
            for i in lineup(request)
        )
        return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n<Lineup>{items}</Lineup>\n', media_type="application/xml")

    @r.post("/lineup.post")
    async def lineup_post(scan: str | None = None):
        return PlainTextResponse("")  # config-driven lineup; nothing to scan

    @r.get("/device.xml")
    async def device_xml(request: Request):
        d = discover(request)
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
            "  <specVersion><major>1</major><minor>0</minor></specVersion>\n"
            f"  <URLBase>{escape(d['BaseURL'])}</URLBase>\n"
            "  <device>\n"
            "    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>\n"
            f"    <friendlyName>{escape(d['FriendlyName'])}</friendlyName>\n"
            f"    <manufacturer>{escape(d['Manufacturer'])}</manufacturer>\n"
            f"    <modelName>{escape(d['ModelNumber'])}</modelName>\n"
            f"    <modelNumber>{escape(d['ModelNumber'])}</modelNumber>\n"
            "    <serialNumber></serialNumber>\n"
            f"    <UDN>uuid:{escape(d['DeviceID'])}</UDN>\n"
            "  </device>\n"
            "</root>\n"
        )
        return Response(body, media_type="application/xml")

    @r.get("/auto/v{guide_number}")
    async def stream(guide_number: str, request: Request):
        ch = app.channel_map.by_guide_number(guide_number)
        if ch is None:
            return PlainTextResponse("Unknown channel\n", status_code=404)
        # LIVE-6: online → degraded → unprobed; offline ones are re-probed as a last resort.
        sources = app.channel_map.ordered_sources(ch.id)
        session = StreamSession(
            channel=ch,
            sources=sources,
            offline_sources=app.channel_map.offline_sources(ch.id),
            hooks=app.relay_hooks(),
            metrics=app.metrics,
            tracker=app.tracker,
            state=app.state,
            tune_timeout=app.cfg.tuners.tune_timeout.total_seconds(),
            stall_timeout=app.cfg.tuners.stall_timeout.total_seconds(),
            client_addr=request.client.host if request.client else "",
        )
        if not app.pool.try_acquire(session):
            app.metrics.tuner_rejections.inc()
            return PlainTextResponse("All tuners in use\n", status_code=503)  # LIVE-4
        app.metrics.streams_active.set(len(app.pool.in_use))
        return _AsgiAdapter(RelayResponse(session, app.pool))

    return r


class _AsgiAdapter(Response):
    """Lets FastAPI return a raw ASGI callable as the response."""

    def __init__(self, inner: RelayResponse) -> None:
        super().__init__()
        self.inner = inner

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        await self.inner(scope, receive, send)
