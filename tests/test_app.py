"""OPS-1 … OPS-4, NFR-8 and app wiring tests."""

from __future__ import annotations

import base64
import io
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml

from conftest import FIXTURES
from pitlane.app import App, create_app, public_bind_addresses
from pitlane.cli import main as cli_main
from pitlane.config import load_config

SECRET = "s3cr3t-plex-token-value"
UI_PW = "ui-password-123"


def base_raw(tmp_path: Path, **extra) -> dict:
    (tmp_path / "chan.json").write_text(
        json.dumps(
            {
                "version": 1,
                "channels": [
                    {"channel": "f1-main", "label": "<script>alert(1)</script>", "kind": "hls", "url": "https://user:pw@example.invalid/a.m3u8"}
                ],
            }
        )
    )
    d = tmp_path / "data" / "jolpica"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-races.json").write_bytes((FIXTURES / "jolpica" / "2026-races.json").read_bytes())
    (tmp_path / "pw").write_text(UI_PW)
    raw = {
        "server": {"bind": "127.0.0.1:5004", "data_dir": str(tmp_path / "data"), "ui_password": f"${{file:{tmp_path / 'pw'}}}"},
        "series": {"f1": {"seasons": [2026]}},
        "channels": [{"id": "f1-main", "guide_number": 101, "name": "F1 Main Feed", "series": ["f1"]}],
        "providers": [{"type": "channel-json", "name": "mine", "path": str(tmp_path / "chan.json")}],
        "plex": {"token": "${PITLANE_PLEX_TOKEN}"},
    }
    raw.update(extra)
    return raw


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PITLANE_PLEX_TOKEN", SECRET)
    cfg_path = tmp_path / "pitlane.yaml"
    cfg_path.write_text(yaml.safe_dump(base_raw(tmp_path)))
    cfg = load_config(cfg_path)

    def offline(req):
        raise httpx.ConnectError("offline in tests")

    pl = App(cfg, http_transport=httpx.MockTransport(offline), start_background=False,
             clock=lambda: datetime(2026, 10, 9, 8, tzinfo=UTC))  # fmt: skip
    api = create_app(app_obj=pl, install_signal_handlers=False)
    await pl.startup()
    auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{UI_PW}".encode()).decode()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://pitlane.test") as c:
        c.pl = pl  # type: ignore[attr-defined]
        c.hdrs = auth  # type: ignore[attr-defined]
        c.cfg_path = cfg_path  # type: ignore[attr-defined]
        yield c
    await pl.shutdown()


async def test_ui_requires_password_and_escapes(client):
    assert (await client.get("/")).status_code == 401
    r = await client.get("/", headers=client.hdrs)
    assert r.status_code == 200
    html = r.text
    assert "F1 Main Feed" in html and "Guide preview" in html and "Needs review" in html and "Reload config" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "user:pw" not in html and SECRET not in html  # locators & secrets never shown
    # tuner endpoints stay open for PMS
    assert (await client.get("/discover.json")).status_code == 200
    st = (await client.get("/api/status", headers=client.hdrs)).json()
    assert st["channels"][0]["sources"][0]["state"] == "unknown"


async def test_metrics_and_healthz(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    text = (await client.get("/metrics")).text
    for name in ("pitlane_streams_active", "pitlane_tuner_rejections_total", "pitlane_tune_seconds"):
        assert name in text
    assert SECRET not in text and UI_PW not in text


async def test_reload_valid_and_invalid(client):
    pl = client.pl
    raw = yaml.safe_load(client.cfg_path.read_text())
    raw["channels"].append({"id": "f1-alt", "guide_number": 102, "name": "F1 Alternate"})
    client.cfg_path.write_text(yaml.safe_dump(raw))
    assert await pl.reload() == []
    assert pl.cfg.channel("f1-alt") is not None

    client.cfg_path.write_text("channels:\n  - id: BAD ID\n    guide_number: 1\n    name: x\n")
    errors = await pl.reload()
    assert errors and errors[0].startswith("channels[0].id")
    assert pl.cfg.channel("f1-alt") is not None  # old config kept
    html = (await client.get("/", headers=client.hdrs)).text
    assert "Config reload rejected" in html
    r = await client.post("/ui/reload", headers=client.hdrs)
    assert r.status_code == 303


async def test_xmltv_endpoint(client):
    r = await client.get("/xmltv.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert b'<channel id="f1-main.pitlane">' in r.content and b"S2026E1705" in r.content


async def test_title_card(client):
    r = await client.get("/static/cards/2026-17/1705.png")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image

    assert Image.open(io.BytesIO(r.content)).size == (1280, 720)


def test_public_bind_detection():
    assert public_bind_addresses("8.8.8.8") == ["8.8.8.8"]
    assert public_bind_addresses("192.168.1.10") == []
    assert public_bind_addresses("127.0.0.1") == []


async def test_public_bind_warns_loudly(tmp_path, monkeypatch, caplog):
    raw = base_raw(tmp_path)
    raw["server"]["bind"] = "203.0.113.5:5004"  # documentation range is not global; patch the check
    monkeypatch.setattr("pitlane.app.public_bind_addresses", lambda host: ["198.51.100.1"])
    from pitlane.config import parse_config

    (tmp_path / "pw").write_text(UI_PW)
    cfg = parse_config(raw, environ={"PITLANE_PLEX_TOKEN": SECRET})
    pl = App(cfg, http_transport=httpx.MockTransport(lambda r: httpx.Response(503)), start_background=False)
    with caplog.at_level(logging.WARNING):
        await pl.startup()
    await pl.shutdown()
    assert any("PUBLIC INTERFACE" in r.getMessage() for r in caplog.records)


def test_cli_check_config(tmp_path, capsys):
    p = tmp_path / "bad.yaml"
    p.write_text("tuners:\n  count: -1\nbogus: 1\n")
    assert cli_main(["--config", str(p), "check-config"]) == 2
    err = capsys.readouterr().err
    assert "tuners.count:" in err and "bogus: unknown key" in err
    good = tmp_path / "good.yaml"
    good.write_text("channels: []\n")
    assert cli_main(["--config", str(good), "check-config"]) == 0


def test_cli_entrypoint_runs():
    r = subprocess.run([sys.executable, "-m", "pitlane", "--config", "/nonexistent.yaml", "check-config"], capture_output=True, text=True)
    assert r.returncode == 2 and "/nonexistent.yaml" in r.stderr


async def test_title_cards_reachable_without_ui_password(client):
    """REP-5 step 4: PMS fetches cards from advertised_url without credentials, so
    /static/cards/… must stay outside the dashboard password (cards are spoiler-safe)."""
    assert client.pl.cfg.server.ui_password  # the fixture sets a UI password
    assert (await client.get("/")).status_code == 401
    r = await client.get("/static/cards/2026-17/1705.png")  # no Authorization header
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
