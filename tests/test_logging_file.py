"""OPS-5: the log is also written to a rotated file under the data folder."""

from __future__ import annotations

import io
import json
import logging

from pitlane.config import parse_config
from pitlane.logging import secrets, setup_logging


def test_log_file_json_redacted_and_rotated(tmp_path):
    path = tmp_path / "logs" / "pitlane.log"
    secrets.add("hunter2-secret")
    try:
        setup_logging("info", stream=io.StringIO(), file=str(path), max_bytes=2000, backups=2)
        lg = logging.getLogger("pitlane.test")
        lg.info("stream requested", extra={"channel": "f1-main", "url": "http://p.invalid/live/u/hunter2-secret/1.ts"})
        first = json.loads(path.read_text().splitlines()[0])
        assert first["msg"] == "stream requested" and first["channel"] == "f1-main"
        assert "hunter2-secret" not in path.read_text()
        for i in range(100):
            lg.info("filler", extra={"i": i})
        rotated = sorted(p.name for p in path.parent.iterdir())
        assert rotated == ["pitlane.log", "pitlane.log.1", "pitlane.log.2"]  # at most 2 kept
    finally:
        setup_logging("info", stream=io.StringIO())  # back to console only; closes the file


def test_log_path_setting():
    cfg = parse_config({"server": {"data_dir": "/data"}}, environ={})
    assert cfg.server.log_path == "/data/logs/pitlane.log"
    assert parse_config({"server": {"log_file": None}}, environ={}).server.log_path is None
    assert parse_config({"server": {"log_file": "/var/log/p.log"}}, environ={}).server.log_path == "/var/log/p.log"


def test_check_config_writes_no_log(tmp_path, monkeypatch):
    from pitlane.cli import main

    cfg = tmp_path / "pitlane.yaml"
    cfg.write_text(f"server: {{data_dir: {tmp_path / 'data'}}}\n")
    assert main(["--config", str(cfg), "check-config"]) == 0
    assert not (tmp_path / "data" / "logs").exists()
    setup_logging("info", stream=io.StringIO())
