"""OPS-7: warnings and errors as ntfy notifications."""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import httpx
import pytest

from pitlane.config import ConfigError, parse_config
from pitlane.logging import secrets
from pitlane.notify import NtfyHandler, handler_from_config, scrub


class FakeNtfy:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"id": "x"})

    @property
    def messages(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


async def run_handler(handler: NtfyHandler, fn) -> None:
    root = logging.getLogger()
    handler.start()
    root.addHandler(handler)
    try:
        fn()
        await asyncio.sleep(0)  # let call_soon_threadsafe enqueue
        await handler.stop()
    finally:
        root.removeHandler(handler)


async def test_warnings_become_notifications_with_auth():
    ntfy = FakeNtfy()
    h = NtfyHandler("https://ntfy.example.invalid/", "pitlane", username="me", password="pw-123", transport=httpx.MockTransport(ntfy))
    relay = logging.getLogger("pitlane.relay")

    def go():
        relay.info("stream requested", extra={"channel": "f1-main"})  # below level
        relay.warning("tune failed", extra={"channel": "f1-main", "reason": "no usable sources"})
        logging.getLogger("pitlane.app").error("config reload rejected", extra={"error": "bad key"})
        logging.getLogger("uvicorn.error").warning("not ours")  # only pitlane.* loggers

    await run_handler(h, go)
    assert [m["title"] for m in ntfy.messages] == ["Pitlane: tune failed", "Pitlane: config reload rejected"]
    first, second = ntfy.messages
    assert first == {
        "topic": "pitlane",
        "title": "Pitlane: tune failed",
        "message": "channel: f1-main\nreason: no usable sources",
        "priority": 3,
        "tags": ["warning"],
    }
    assert second["priority"] == 4 and second["tags"] == ["rotating_light"]
    req = ntfy.requests[0]
    assert str(req.url) == "https://ntfy.example.invalid"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"me:pw-123").decode()


async def test_throttle_and_held_count():
    ntfy = FakeNtfy()
    h = NtfyHandler("https://ntfy.example.invalid", "pitlane", throttle_s=60, transport=httpx.MockTransport(ntfy))
    lg = logging.getLogger("pitlane.relay")

    def go():
        for _ in range(3):
            lg.warning("tune failed", extra={"channel": "f1-main"})
        lg.warning("tune failed", extra={"channel": "f1-alt"})  # different channel → own window

    await run_handler(h, go)
    assert [m["message"] for m in ntfy.messages] == ["channel: f1-main", "channel: f1-alt"]
    # window over → the next one reports what was held back
    h2_msgs = FakeNtfy()
    h._client = httpx.AsyncClient(transport=httpx.MockTransport(h2_msgs))
    h._last = {k: v - 61 for k, v in h._last.items()}
    await run_handler(h, lambda: lg.warning("tune failed", extra={"channel": "f1-main"}))
    assert h2_msgs.messages[0]["message"] == "channel: f1-main\n(+2 more like this in the last 1 min)"


async def test_credentials_never_sent():
    ntfy = FakeNtfy()
    secrets.add("plex-secret-token")
    h = NtfyHandler("https://ntfy.example.invalid", "pitlane", transport=httpx.MockTransport(ntfy))
    err = "Error opening input file http://panel.example.invalid:80/live/user1/pass1/5396.ts. | token plex-secret-token"

    await run_handler(h, lambda: logging.getLogger("pitlane.health").warning("source failed", extra={"error": err}))
    body = ntfy.messages[0]["message"]
    assert "user1" not in body and "pass1" not in body and "plex-secret-token" not in body
    assert "http://panel.example.invalid:80/…" in body
    assert scrub("see https://h.invalid/a/b?c=d and ftp://x.invalid/y") == "see https://h.invalid/… and ftp://x.invalid/…"


async def test_failed_send_is_logged_once_not_looped(caplog):
    ntfy = FakeNtfy(status=401)
    h = NtfyHandler("https://ntfy.example.invalid", "pitlane", transport=httpx.MockTransport(ntfy))
    await run_handler(h, lambda: logging.getLogger("pitlane.relay").warning("tune failed"))
    assert len(ntfy.requests) == 1  # 4xx is not retried
    assert "ntfy notification failed" in caplog.text  # logged, but not forwarded to ntfy again


def test_config():
    cfg = parse_config(
        {"notify": {"ntfy": {"url": "https://ntfy.example.invalid", "username": "a", "password": "b", "throttle": "10m"}}},
        environ={},
    )
    h = handler_from_config(cfg)
    assert h is not None and h.topic == "pitlane" and h.throttle_s == 600 and h.level == logging.WARNING
    assert handler_from_config(parse_config({}, environ={})) is None
    for bad in (
        {"url": "ntfy.example.invalid"},
        {"url": "https://x.invalid", "topic": "a b"},
        {"url": "https://x.invalid", "token": "t", "username": "a", "password": "b"},
        {"url": "https://x.invalid", "username": "a"},
    ):
        with pytest.raises(ConfigError):
            parse_config({"notify": {"ntfy": bad}}, environ={})


async def test_app_forwards_startup_warnings(tmp_path):
    from pitlane.app import App

    ntfy = FakeNtfy()
    cfg = parse_config(
        {
            "server": {"bind": "127.0.0.1:5999", "data_dir": str(tmp_path / "data")},  # no advertised_url → warning
            "notify": {"ntfy": {"url": "https://ntfy.example.invalid", "topic": "home"}},
        },
        environ={},
    )
    app = App(cfg, start_background=False, notify_transport=httpx.MockTransport(ntfy))
    await app.startup()
    await app.shutdown()  # flushes the queue
    assert any("advertised_url" in m["title"] and m["topic"] == "home" for m in ntfy.messages)
    assert not any(isinstance(h, NtfyHandler) for h in logging.getLogger().handlers)
