"""Structured logging (OPS-5) with secret redaction (OPS-4).

Every record is one JSON object per line. A correlation ID is carried in a
context variable, so everything logged inside a stream session or replay job
carries the same ``cid``.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Iterator

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("pitlane_cid", default=None)

_REDACTED = "***"


class _SecretRegistry:
    """Holds every resolved secret value so it can be scrubbed from log output (OPS-4)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def add(self, value: str | None) -> None:
        if value and len(value) >= 4:
            with self._lock:
                self._values.add(value)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def redact(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for v in values:
            if v in text:
                text = text.replace(v, _REDACTED)
        return text


secrets = _SecretRegistry()


def redact(text: str) -> str:
    return secrets.redact(text)


def new_correlation_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def correlation(cid: str) -> Iterator[str]:
    token = correlation_id.set(cid)
    try:
        yield cid
    finally:
        correlation_id.reset(token)


_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = getattr(record, "cid", None) or correlation_id.get()
        if cid:
            payload["cid"] = cid
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and key != "cid" and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "cid", None) or correlation_id.get()
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and k != "cid" and not k.startswith("_")
        }
        line = f"{self.formatTime(record)} {record.levelname:<7} {record.name}"
        if cid:
            line += f" [{cid}]"
        line += f" {record.getMessage()}"
        if extras:
            line += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return redact(line)


def setup_logging(level: str = "info", fmt: str | None = None, stream: Any = None) -> None:
    fmt = (fmt or os.environ.get("PITLANE_LOG_FORMAT") or "json").lower()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(TextFormatter() if fmt == "text" else JsonFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_pitlane", False):
            root.removeHandler(h)
    handler._pitlane = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
    # OPS-4: request URLs may embed provider credentials; never log them per request.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # watchfiles logs "N changes detected" at INFO for every write; a DVR recording
    # growing in an inbox made that about one line per second.
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    # uvicorn installs its own handlers; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("pitlane") else f"pitlane.{name}")
