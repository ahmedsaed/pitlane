"""Prometheus metrics (OPS-2, docs/tuner.md "Metrics"). Labels never carry secrets (OPS-4)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

STATE_VALUE = {"offline": 0, "degraded": 1, "online": 2}


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.streams_active = Gauge("pitlane_streams_active", "Active client streams", registry=r)
        self.failovers = Counter("pitlane_stream_failovers_total", "Mid-stream source switches", ["channel"], registry=r)
        self.tune_seconds = Histogram(
            "pitlane_tune_seconds",
            "Time from stream request to first TS byte",
            buckets=(0.25, 0.5, 1, 2, 3, 5, 10, 20),
            registry=r,
        )
        self.source_state = Gauge(
            "pitlane_source_state", "Source state: 0 offline, 1 degraded, 2 online", ["source", "channel"], registry=r
        )
        self.tuner_rejections = Counter("pitlane_tuner_rejections_total", "Requests rejected with 503", registry=r)
        self.replay_filed = Counter("pitlane_replays_filed_total", "Files placed into the library", registry=r)
        self.replay_review = Counter("pitlane_replays_needs_review_total", "Files sent to needs-review", registry=r)
        self.ffmpeg_running = Gauge("pitlane_ffmpeg_processes", "Running ffmpeg child processes", registry=r)

    def render(self) -> bytes:
        return generate_latest(self.registry)
