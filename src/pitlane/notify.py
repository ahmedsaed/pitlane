"""Push notifications for warnings and errors via ntfy (OPS-7; docs/configuration.md "notify").

A logging handler on the root logger turns ``pitlane.*`` records at or above the
configured level into ntfy messages. Sending happens in a background task, so a
slow or unreachable ntfy server never blocks streaming or the replay pipeline.

- The same message for the same channel/source/file is sent at most once per
  ``throttle``; the next one says how many were held back.
- Messages are redacted twice: configured secrets (OPS-4) and URL paths, which
  for IPTV sources carry the account's username and password.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from .logging import _STD_ATTRS, redact

log = logging.getLogger("pitlane.notify")

PRIORITY = {logging.WARNING: 3, logging.ERROR: 4, logging.CRITICAL: 5}
TAGS = {logging.WARNING: ["warning"], logging.ERROR: ["rotating_light"], logging.CRITICAL: ["rotating_light"]}
THROTTLE_KEYS = ("channel", "source", "provider", "file")
SKIP_FIELDS = {"cid", "color_message"}
_URL_PATH = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s\"'|]+)/[^\s\"'|]*", re.IGNORECASE)
QUEUE_MAX = 100


def scrub(text: str) -> str:
    """Secrets and URL paths out (hosts stay, so the message still says where)."""
    return _URL_PATH.sub(r"\1/…", redact(text))


class NtfyHandler(logging.Handler):
    def __init__(
        self,
        url: str,
        topic: str,
        *,
        level: int = logging.WARNING,
        throttle_s: float = 1800.0,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(level)
        self.endpoint = url.rstrip("/")
        self.topic = topic
        self.throttle_s = throttle_s
        auth: Any = None
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif username:
            auth = httpx.BasicAuth(username, password or "")
        self._client = httpx.AsyncClient(transport=transport, auth=auth, headers=headers, timeout=10)
        self._queue: asyncio.Queue[dict] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._last: dict[tuple, float] = {}
        self._held: dict[tuple, int] = {}
        self.sent = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(QUEUE_MAX)
        self._task = self._loop.create_task(self._worker(), name="ntfy")

    async def stop(self, timeout: float = 5.0) -> None:
        if self._queue is not None and self._task is not None:
            await asyncio.sleep(0)  # run enqueues scheduled from other threads
            try:
                await asyncio.wait_for(self._queue.join(), timeout)
            except TimeoutError:
                pass
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._client.aclose()

    def notify(self, title: str, message: str, priority: int = 2, tags: list[str] | None = None) -> None:
        """A message that is not a log record (e.g. VOD "ready to watch"). Same redaction."""
        if self._loop is None or self._queue is None or self._loop.is_closed():
            return
        msg = {
            "topic": self.topic,
            "title": scrub(title)[:250],
            "message": scrub(message)[:3500],
            "priority": priority,
            "tags": tags or ["clapper"],
        }
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._enqueue(msg)
        else:
            self._loop.call_soon_threadsafe(self._enqueue, msg)

    # ------------------------------------------------------------------ logging
    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("pitlane") or record.name.startswith("pitlane.notify"):
            return
        if self._loop is None or self._queue is None or self._loop.is_closed():
            return
        try:
            message = self._message(record)
        except Exception:  # never let a notification break the code that logged
            return
        if message is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._enqueue(message)  # logged from the event loop itself: queue it now
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, message)
        except RuntimeError:
            pass

    def _enqueue(self, message: dict) -> None:
        assert self._queue is not None
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            pass

    def _message(self, record: logging.LogRecord) -> dict | None:
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and k not in SKIP_FIELDS and not k.startswith("_")
        }
        msg = record.getMessage()
        key = (record.name, msg, *(str(extras.get(k, "")) for k in THROTTLE_KEYS))
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self.throttle_s:
            self._held[key] = self._held.get(key, 0) + 1
            return None
        self._last[key] = now
        held = self._held.pop(key, 0)
        lines = [f"{k}: {v}" for k, v in extras.items()]
        if record.exc_info and record.exc_info[1] is not None:
            lines.append(f"exception: {record.exc_info[1]!r}")
        if held:
            lines.append(f"(+{held} more like this in the last {self.throttle_s / 60:g} min)")
        return {
            "topic": self.topic,
            "title": scrub(f"Pitlane: {msg}")[:250],
            "message": scrub("\n".join(lines) or msg)[:3500],
            "priority": PRIORITY.get(record.levelno, 3 if record.levelno < logging.ERROR else 4),
            "tags": TAGS.get(record.levelno, ["warning"]),
        }

    # ------------------------------------------------------------------ sending
    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            message = await self._queue.get()
            try:
                await self._send(message)
            finally:
                self._queue.task_done()

    async def _send(self, message: dict) -> None:
        for attempt in range(2):
            try:
                resp = await self._client.post(self.endpoint, json=message)
                if resp.status_code < 300:
                    self.sent += 1
                    return
                error = f"HTTP {resp.status_code}"
                if resp.status_code < 500:
                    break  # auth/topic problems will not fix themselves
            except httpx.HTTPError as e:
                error = str(e) or e.__class__.__name__
            await asyncio.sleep(2 * (attempt + 1))
        log.warning("ntfy notification failed", extra={"error": error})


def handler_from_config(cfg: Any, transport: httpx.AsyncBaseTransport | None = None) -> NtfyHandler | None:
    n = cfg.notify.ntfy
    if n is None:
        return None
    return NtfyHandler(
        n.url,
        n.topic,
        level=logging.getLevelName(n.level.upper()),
        throttle_s=n.throttle.total_seconds(),
        username=n.username,
        password=n.password,
        token=n.token,
        transport=transport,
    )
