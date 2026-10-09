"""Pitlane's own state in SQLite (architecture.md: "Pitlane's own state lives in SQLite").

All statements are small, indexed and local; they run inline under a lock.
"""

from __future__ import annotations

import json
import secrets as _secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, channel_id TEXT, provider TEXT, priority INTEGER, kind TEXT, label TEXT,
    state TEXT, last_ok_at TEXT, last_error TEXT, latency_ms INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS auto_channels (id TEXT PRIMARY KEY, guide_number TEXT UNIQUE NOT NULL, name TEXT);
CREATE TABLE IF NOT EXISTS stream_runs (
    id TEXT PRIMARY KEY, channel_id TEXT, source_id TEXT, client_addr TEXT, started_at TEXT, ended_at TEXT,
    bytes INTEGER DEFAULT 0, failovers INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS replay_jobs (
    key TEXT PRIMARY KEY, event_id TEXT, session_type TEXT, provider TEXT, provider_item_id TEXT, title TEXT,
    locator TEXT, kind TEXT, status TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS library_items (
    path TEXT PRIMARY KEY, session_id TEXT, season INTEGER, episode_no INTEGER, session_type TEXT, sha256 TEXT,
    title TEXT, plex_rating_key TEXT, metadata_applied_at TEXT, spoiler_state TEXT, filed_at TEXT);
CREATE TABLE IF NOT EXISTS review_items (
    path TEXT PRIMARY KEY, reason TEXT, candidates TEXT, detected_at TEXT, resolved_session_id TEXT, sha256 TEXT);
CREATE TABLE IF NOT EXISTS inbox_seen (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, outcome TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS assignments (sha256 TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT, created_at TEXT);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------- kv
    def get(self, key: str) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def device_id(self) -> str:
        """8 hex chars, generated once and kept (docs/tuner.md)."""
        with self._lock:
            v = self.get("device_id")
            if v is None:
                v = _secrets.token_hex(4).upper()
                self.set("device_id", v)
            return v

    # ------------------------------------------------------------- auto channels (LIVE-2)
    def auto_channel_number(self, channel_id: str, name: str, taken: set[str]) -> str:
        with self._lock:
            row = self.one("SELECT guide_number FROM auto_channels WHERE id=?", (channel_id,))
            if row:
                return row["guide_number"]
            used = {r["guide_number"] for r in self.query("SELECT guide_number FROM auto_channels")} | taken
            n = 1000
            while str(n) in used:
                n += 1
            self.execute("INSERT INTO auto_channels(id, guide_number, name) VALUES(?,?,?)", (channel_id, str(n), name))
            return str(n)

    # ------------------------------------------------------------- review / assignments
    def add_review(self, path: str, reason: str, candidates: list[str], sha256: str | None) -> None:
        self.execute(
            "INSERT INTO review_items(path, reason, candidates, detected_at, sha256) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET reason=excluded.reason, candidates=excluded.candidates, sha256=excluded.sha256",
            (path, reason, json.dumps(candidates), now_iso(), sha256),
        )

    def save_assignment(self, sha256: str, session_id: str, title: str | None = None) -> None:
        self.execute(
            "INSERT INTO assignments(sha256, session_id, title, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(sha256) DO UPDATE SET session_id=excluded.session_id, title=excluded.title",
            (sha256, session_id, title, now_iso()),
        )

    def assignment(self, sha256: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM assignments WHERE sha256=?", (sha256,))
