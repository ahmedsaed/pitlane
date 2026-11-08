"""F1 live timing: which session is on and whether it is still running (EPG-8).

Formula 1's live timing service (SignalR Core over a websocket, no login needed as of
2026-11) pushes ``SessionInfo`` (the session on track now) and ``SessionStatus``
(``Inactive`` before the start and during breaks, ``Started``, ``Aborted`` for a red
flag, ``Finished``, then ``Finalised`` and ``Ends`` once results are official) and
``SessionData`` (``QualifyingPart``: qualifying shows ``Finished`` after every part,
also when the next part is delayed for a long time **[observed 2026-11-07]**).

The service is unofficial for third parties and may change or require a login; callers
treat "no data" as "fall back to the published schedule".
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .logging import get_logger

log = get_logger("livetiming")

BASE = "https://livetiming.formula1.com/signalrcore"
TOPICS = ["SessionInfo", "SessionStatus", "SessionData"]
RS = "\x1e"  # SignalR record separator
PING_EVERY_S = 15.0
RETRY_S = 30.0
ENDED = ("Finalised", "Ends")


@dataclass(frozen=True)
class LiveSession:
    """What the feed says about the session on track."""

    key: int | None
    type: str  # "Practice", "Qualifying", "Race", …
    name: str  # "Practice 1", "Sprint Qualifying", …
    starts_at: datetime | None  # scheduled start, UTC
    status: str  # SessionStatus.Status
    part: int | None = None  # qualifying part (1-3), from SessionData

    @property
    def qualifying(self) -> bool:
        return self.type == "Qualifying" or "Qualifying" in self.name or "Shootout" in self.name

    @property
    def over(self) -> bool:
        """Results are official, or the chequered flag fell on the last part."""
        if self.status in ENDED:
            return True
        return self.status == "Finished" and (not self.qualifying or (self.part or 0) >= 3)


def _utc_start(info: dict[str, Any]) -> datetime | None:
    """``StartDate`` is local time without offset; ``GmtOffset`` is e.g. ``-03:00:00``."""
    try:
        local = datetime.fromisoformat(info["StartDate"])
        sign = -1 if str(info.get("GmtOffset", "")).startswith("-") else 1
        h, m, s = (int(x) for x in str(info.get("GmtOffset", "0:0:0")).lstrip("+-").split(":"))
    except (KeyError, ValueError, TypeError):
        return None
    return (local - sign * timedelta(hours=h, minutes=m, seconds=s)).replace(tzinfo=UTC)


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class LiveTiming:
    """One websocket to the live timing service while :meth:`run` runs."""

    def __init__(self, url: str = BASE) -> None:
        self.url = url
        self.info: dict[str, Any] = {}
        self.status: str | None = None
        self.part: int | None = None
        self.updated: float | None = None  # monotonic time of the last message
        self.connected = False

    @property
    def session(self) -> LiveSession | None:
        if not self.info or self.status is None:
            return None
        return LiveSession(
            key=self.info.get("Key"),
            type=str(self.info.get("Type", "")),
            name=str(self.info.get("Name", "")),
            starts_at=_utc_start(self.info),
            status=self.status,
            part=self.part,
        )

    def fresh(self, max_age_s: float = 90.0) -> bool:
        """Connected and heard from recently (the server pings every ~15 s)."""
        return self.connected and self.updated is not None and time.monotonic() - self.updated < max_age_s

    def handle(self, raw: str) -> None:
        """Apply one websocket frame (one or more SignalR messages)."""
        for part in raw.split(RS):
            if not part.strip():
                continue
            try:
                msg = json.loads(part)
            except ValueError:
                continue
            self.updated = time.monotonic()
            kind = msg.get("type")
            if kind == 3 and isinstance(msg.get("result"), dict):  # Subscribe result: full state
                for topic, data in msg["result"].items():
                    self._apply(topic, data, replace=True)
            elif kind == 1 and msg.get("target") == "feed":
                args = msg.get("arguments") or []
                if len(args) >= 2:
                    self._apply(args[0], args[1], replace=False)

    def _apply(self, topic: str, data: Any, replace: bool) -> None:
        if not isinstance(data, dict):
            return
        if topic == "SessionInfo":
            if replace or data.get("Key") not in (None, self.info.get("Key")):
                self.info = {}
                self.part = None
            _merge(self.info, data)
            log.info(
                "live timing session",
                extra={"session": self.info.get("Name"), "type": self.info.get("Type"), "start": str(_utc_start(self.info))},
            )
        elif topic == "SessionStatus" and data.get("Status"):
            if data["Status"] != self.status:
                log.info("live timing status", extra={"session": self.info.get("Name"), "status": data["Status"]})
            self.status = data["Status"]
        elif topic == "SessionData":
            series = data.get("Series")
            for item in series.values() if isinstance(series, dict) else series or []:
                part = item.get("QualifyingPart") if isinstance(item, dict) else None
                if isinstance(part, int) and part != self.part:
                    log.info("live timing qualifying part", extra={"session": self.info.get("Name"), "part": part})
                    self.part = part

    async def run(self) -> None:
        """Connect, subscribe and keep the state current until cancelled."""
        import websockets

        while True:
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.post(f"{self.url}/negotiate", params={"negotiateVersion": "1"})
                    r.raise_for_status()
                    token = r.json()["connectionToken"]
                    cookie = "; ".join(f"{k}={v}" for k, v in r.cookies.items())
                ws_url = self.url.replace("https://", "wss://", 1) + f"?id={token}"
                headers = {"Cookie": cookie} if cookie else {}
                async with websockets.connect(ws_url, additional_headers=headers, open_timeout=15) as ws:
                    await ws.send(json.dumps({"protocol": "json", "version": 1}) + RS)
                    await ws.send(json.dumps({"type": 1, "target": "Subscribe", "arguments": [TOPICS], "invocationId": "0"}) + RS)
                    self.connected = True
                    log.info("live timing connected")
                    pinger = asyncio.get_running_loop().create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self.handle(raw if isinstance(raw, str) else raw.decode(errors="replace"))
                    finally:
                        pinger.cancel()
                log.info("live timing closed by the server")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("live timing unavailable", extra={"error": f"{e.__class__.__name__}: {e}"[:200]})
            finally:
                self.connected = False
            await asyncio.sleep(RETRY_S)

    @staticmethod
    async def _ping(ws: Any) -> None:
        while True:
            await asyncio.sleep(PING_EVERY_S)
            await ws.send(json.dumps({"type": 6}) + RS)
