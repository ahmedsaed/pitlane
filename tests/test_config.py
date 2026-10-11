"""OPS-3 / OPS-4 configuration tests."""

from __future__ import annotations

import io
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from pitlane.config import ConfigError, config_warnings, load_config, parse_config
from pitlane.durations import parse_duration
from pitlane.logging import JsonFormatter, secrets, setup_logging

ROOT = Path(__file__).parent.parent


def errors_of(raw, environ=None) -> list[str]:
    with pytest.raises(ConfigError) as ei:
        parse_config(raw, environ=environ or {})
    return ei.value.errors


def test_durations():
    assert parse_duration("10s") == timedelta(seconds=10)
    assert parse_duration("5m") == timedelta(minutes=5)
    assert parse_duration("6h") == timedelta(hours=6)
    assert parse_duration("1d") == timedelta(days=1)
    assert parse_duration("-90m") == timedelta(minutes=-90)
    for bad in ("10", 10, "ten minutes", "5x"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_defaults_minimal():
    cfg = parse_config({}, environ={})
    assert cfg.tuners.count == 2
    assert cfg.tuners.stall_timeout == timedelta(seconds=8)
    assert cfg.health.interval == timedelta(minutes=5)
    assert cfg.epg.days_back == 1 and cfg.epg.days_ahead == 14
    assert cfg.spoiler.enabled is True
    assert cfg.retention.enabled is False and cfg.retention.dry_run is True
    assert cfg.providers == [] and cfg.provider_cfgs == []  # PROV-3: nothing enabled by default
    from pitlane.models import SessionType

    assert cfg.epg.post_roll_for(SessionType.RACE) == timedelta(minutes=20)
    assert cfg.epg.post_roll_for(SessionType.FP1) == timedelta(minutes=10)


def test_example_config_is_valid(tmp_path):
    for name in ("ui_password", "iptv_url", "spoiler_token", "ntfy_password"):
        (tmp_path / name).write_text("s3cret-" + name)
    text = (ROOT / "config" / "pitlane.example.yaml").read_text().replace("/config/", f"{tmp_path}/")
    p = tmp_path / "pitlane.yaml"
    p.write_text(text)
    cfg = load_config(p, environ={"PITLANE_PLEX_TOKEN": "plex-token-xyz"})
    assert cfg.plex.token == "plex-token-xyz"
    assert [c.id for c in cfg.channels] == ["f1-main", "f1-alt"]
    assert cfg.provider_cfgs[0].url == "s3cret-iptv_url"


def test_duplicate_channel_id_and_guide_number():
    errs = errors_of(
        {
            "channels": [
                {"id": "f1-main", "guide_number": 101, "name": "A"},
                {"id": "f1-main", "guide_number": "102", "name": "B"},
                {"id": "f1-x", "guide_number": 101, "name": "C"},
            ]
        }
    )
    assert 'channels[1].id: duplicate "f1-main"' in errs
    assert "channels[2].guide_number: 101 already used by f1-main" in errs


def test_bad_channel_id():
    errs = errors_of({"channels": [{"id": "F1 Main", "guide_number": 1, "name": "x"}]})
    assert any(e.startswith("channels[0].id:") and "[a-z0-9-]+" in e for e in errs)


def test_unknown_map_channel():
    errs = errors_of(
        {
            "channels": [{"id": "f1-main", "guide_number": 101, "name": "A"}],
            "providers": [{"type": "m3u", "name": "x", "path": "/p.m3u", "map": [{"match": {"tvg-id": "a"}, "channel": "f1-man"}]}],
        }
    )
    assert 'providers[0].map[0].channel: unknown channel "f1-man"' in errs


def test_plex_required_with_replays():
    errs = errors_of({"replays": {"inboxes": ["/inbox"]}})
    assert "plex.token: required when replays.inboxes is set" in errs


def test_plugin_type_must_be_enabled():
    errs = errors_of({"providers": [{"type": "m3u", "name": "a", "path": "/x"}] * 3 + [{"type": "foo", "name": "b"}]})
    assert 'providers[3].type: "foo" not enabled' in errs
    cfg = parse_config({"plugins": {"enabled": ["foo"]}, "providers": [{"type": "foo", "name": "b", "x": 1}]}, environ={})
    assert cfg.provider_cfgs[0].type == "foo"


def test_unknown_keys_rejected():
    errs = errors_of({"server": {"bnd": "0.0.0.0:5004"}})
    assert "server.bnd: unknown key" in errs
    errs = errors_of({"providers": [{"type": "m3u", "name": "a", "path": "/x", "refrsh": "1h"}]})
    assert "providers[0].refrsh: unknown key" in errs


def test_secrets_must_use_indirection():
    errs = errors_of({"plex": {"token": "literal-token"}})
    assert any(e.startswith("plex.token: secrets must be given as") for e in errs)


def test_secret_env_and_file(tmp_path):
    f = tmp_path / "tok"
    f.write_text("file-secret-123\n")
    cfg = parse_config(
        {"plex": {"token": "${MY_TOKEN}", "spoiler_token": f"${{file:{f}}}"}}, environ={"MY_TOKEN": "env-secret-456"}
    )
    assert cfg.plex.token == "env-secret-456"
    assert cfg.plex.spoiler_token == "file-secret-123"
    errs = errors_of({"plex": {"token": "${MISSING}"}})
    assert "plex.token: environment variable MISSING is not set" in errs


def test_env_fallback_token_file(tmp_path):
    f = tmp_path / "plex_token"
    f.write_text("from-token-file")
    cfg = parse_config({}, environ={"PITLANE_PLEX_TOKEN_FILE": str(f)})
    assert cfg.plex.token == "from-token-file"


def test_secrets_redacted_in_logs():
    parse_config({"plex": {"token": "${T}"}}, environ={"T": "super-secret-token-value"})
    buf = io.StringIO()
    setup_logging("info", "json", stream=buf)
    logging.getLogger("pitlane.test").info("token is super-secret-token-value", extra={"url": "x?t=super-secret-token-value"})
    line = buf.getvalue().strip().splitlines()[-1]
    assert "super-secret-token-value" not in line
    rec = json.loads(line)
    assert rec["msg"] == "token is ***" and rec["level"] == "info"


def test_json_log_has_correlation_id():
    from pitlane.logging import correlation

    buf = io.StringIO()
    setup_logging("info", "json", stream=buf)
    with correlation("stream-abc"):
        logging.getLogger("pitlane.test").info("hello")
    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rec["cid"] == "stream-abc"


def test_load_config_reports_path_and_reason(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("tuners:\n  count: zero\n")
    with pytest.raises(ConfigError) as ei:
        load_config(p, environ={})
    assert ei.value.errors[0].startswith("tuners.count:")
    p.write_text("tuners: [\n")
    with pytest.raises(ConfigError) as ei:
        load_config(p, environ={})
    assert "YAML syntax error" in ei.value.errors[0]


def test_loopback_advertised_warning():
    cfg = parse_config({"server": {"bind": "0.0.0.0:5004", "advertised_url": "http://127.0.0.1:5004"}}, environ={})
    assert any("advertised_url" in w for w in config_warnings(cfg))


def teardown_module(_):
    secrets.clear()
    JsonFormatter  # noqa: B018 (import kept for clarity)
