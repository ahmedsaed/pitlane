# Pitlane – Requirements Specification

Version 0.1 (draft) · 2026-10-09

## 1. Scope

Pitlane is a single long-running service that connects operator-configured motorsport video sources to Plex Media Server (PMS). It covers live viewing through Plex Live TV & DVR, a programme guide generated from the F1 calendar, and an organized replay library.

### 1.1 Out of scope

- Shipping, bundling, or recommending any content source. Providers are generic; the operator supplies the inputs (see `docs/providers.md`).
- Playing web-page embeds (iframes). Plex needs a direct media stream. A source that only exists as a web player is unsupported.
- A Plex client plugin. Plex removed channel plugins; Pitlane integrates only through documented PMS surfaces (tuner emulation, XMLTV, library folders, PMS HTTP API).
- Series other than Formula 1 in v1. The data model MUST NOT preclude adding them (F2, F3, MotoGP…) later.

### 1.2 Glossary

| Term | Meaning |
|---|---|
| **Source** | One concrete media URL or file (HLS playlist, MPEG-TS URL, local file, tuner channel). |
| **Logical channel** | What the user sees in Plex ("F1 Main Feed"). Backed by 1..n ordered sources for failover. |
| **Provider** | A plugin that yields sources (live) or replay items (replays). |
| **Event** | A race weekend (season + round). |
| **Session** | One timed part of an event: FP1, FP2, FP3, Sprint Qualifying, Sprint, Qualifying, Race. Also non-session programming (pre-race, post-race, analysis) as *supplementary* items. |
| **PMS** | Plex Media Server. |

## 2. Functional requirements

### 2.1 Providers (PROV)

- **PROV-1** Pitlane MUST define a provider interface with two capabilities, `live` and `replay`. A provider MAY implement either or both (contract: `docs/providers.md`).
- **PROV-2** Pitlane MUST ship these generic providers: `m3u` (local file or URL), `channel-json` (the JSON schema in `docs/providers.md`), `directory` (local folder of media files), and `hdhomerun` (passthrough of a real HDHomeRun device).
- **PROV-3** Providers MUST be enabled and configured only through the configuration file. No provider is enabled by default.
- **PROV-4** Provider fetches MUST use configurable timeouts, retry with exponential backoff, and per-host rate limits.
- **PROV-5** Pitlane MUST NOT execute code obtained from a provider (no `eval` of remote JavaScript, no downloaded plugins).
- **PROV-6** Third-party provider plugins MAY be loaded from a local plugin directory using the same interface. Pitlane loads them from disk only, never from the network.

### 2.2 Live (LIVE)

- **LIVE-1** Pitlane MUST expose an HDHomeRun-compatible HTTP API that PMS can add as a tuner (endpoints in `docs/tuner.md`).
- **LIVE-2** Each logical channel MUST have a stable guide number and ID that do not change across restarts or config reloads, unless the operator changes them.
- **LIVE-3** When PMS requests a channel stream, Pitlane MUST deliver MPEG-TS over HTTP, remuxing HLS or other inputs with `ffmpeg` (stream copy by default, no re-encode).
- **LIVE-4** Pitlane MUST enforce a configurable tuner count and reject requests beyond it with HTTP 503, which PMS reports as "all tuners in use".
- **LIVE-5** Pitlane MUST run a health checker that probes every source on a schedule (default 5 min, and 1 min within 30 min of a session) and records `online | degraded | offline`, latency, and the last error.
- **LIVE-6** Pitlane MUST try sources in this order: `online`, then `degraded`, then not yet probed, each tier in configured priority order. If none of those is left, Pitlane MUST re-probe the `offline` sources (in priority order, within `tune_timeout`) and start the first one that passes. It returns 503 only when that also fails.
- **LIVE-7** Pitlane SHOULD fail over mid-stream (Plex continuing playback across a switch without re-tuning is **[verified 2026-10-09, real PMS]**). If no bytes arrive from the input for `stall_timeout` (default 8 s), switch to the next healthy source without closing the client's HTTP response, and log the switch.
- **LIVE-8** Pitlane MAY offer an optional transcode profile per channel (e.g. a 720p H.264 fallback) for weak clients. It is off by default.
- **LIVE-9** All active streams MUST stop within 5 s of the client disconnecting. No orphaned `ffmpeg` processes.
- **LIVE-10** A provider MAY set `max_connections`. Pitlane MUST then never hold more upstream connections to that provider (stream inputs and health probes together) than the limit: streams take precedence over probes, a provider whose connections are all streaming is not probed, and on failover the old connection is closed before the next one to the same provider opens. A tune that finds the provider busy tries the channel's other sources first, then waits for a connection to free up within `tune_timeout`. Many IPTV accounts allow one connection, and a second one kicks the first off **[verified 2026-10-10, Xtream Codes panel]**.
- **LIVE-11** A provider MAY set `health_checks: false`. Pitlane MUST then send no probe to its sources (scheduled, after a failure, or at tune time). Its sources stay in their last state, and at tune time they are tried directly in priority order, so opening the stream is the check. This keeps request volume to a provider at one connection per tune.

### 2.3 Guide (EPG)

- **EPG-1** Pitlane MUST fetch the season calendar from the Jolpica F1 API, cache it on disk, and refresh it every 6 h (and on demand).
- **EPG-2** Pitlane MUST serve an XMLTV document at `/xmltv.xml` covering a configurable window (default: 1 day back, 14 days ahead).
- **EPG-3** Each session MUST become a `<programme>` on every logical channel tagged for that series, with series title, session sub-title, description, category, and `xmltv_ns` and `onscreen` episode numbers (rules in `docs/epg.md`).
- **EPG-4** Programme windows MUST use the official session start plus configurable pre-roll and post-roll padding and a per-session-type nominal duration.
- **EPG-5** Gaps between sessions MUST be filled with filler programmes so that Plex never shows "no guide data" for a channel.
- **EPG-6** Every session programme MUST use the same series title (default `Formula 1`) so that a single Plex "record series" rule captures all sessions.
- **EPG-7** Operators MAY add manual programmes (e.g. a pre-race show) through config. These appear as supplementary items.

### 2.4 Replays (REP)

- **REP-1** Pitlane MUST watch one or more inbox folders, including the Plex DVR recording folder, and match each new file to an event and session.
- **REP-2** Matching MUST use, in order: (a) episode numbers in the file's embedded metadata or in Plex DVR's file naming, (b) filename parsing (patterns in `docs/replays.md`), (c) file start time against the calendar. Unmatched files MUST go to a `needs-review` folder and appear in the UI. Pitlane MUST NOT guess.
- **REP-3** Matched files MUST be moved (or hard-linked, if configured) into the library using the naming scheme in `docs/replays.md`. The scheme MUST be deterministic and idempotent. An incoming file MUST NOT be deleted unless it is byte-identical to the file already at its own target path; a content duplicate of a different library item goes to `needs-review`.
- **REP-4** A `replay`-capable provider MAY return items for an event. Pitlane MUST download them with `ffmpeg` (stream copy) into the inbox, then process them as in REP-1. Downloads MUST be restartable, de-duplicated by `(event, session, provider_item_id)`, and limited by a concurrency setting.
- **REP-5** After a file is placed, Pitlane MUST trigger a targeted PMS scan of that folder only, then apply metadata through the PMS HTTP API: title, summary, originally-available date, poster, and lock flags.
- **REP-6** Spoiler mode (on by default) MUST keep results out of every Pitlane-written summary, title, and poster. Results MAY be written into the summary only after PMS reports the item as watched (`viewCount > 0`) for the configured user.
- **REP-7** Supplementary items (build-up, analysis, shows) MUST be stored either as episodes numbered after the core sessions or as extras. The operator chooses which (default: episodes, see `docs/replays.md`).
- **REP-8** Retention: Pitlane MAY delete library items older than N seasons per session type (e.g. keep races forever, drop FP after 1 season). It is off by default and MUST support dry-run.

### 2.5 Interface and operations (OPS)

- **OPS-1** Pitlane MUST serve a small web UI with: channel and source health, current streams, guide preview, replay queue, `needs-review` items (with a manual "assign to session" action), and a "reload config" button.
- **OPS-2** Pitlane MUST expose `/healthz` (liveness) and `/metrics` (Prometheus text format).
- **OPS-3** Configuration MUST be a single YAML file, validated on load. Invalid config MUST refuse to start and print the exact path and reason. Hot reload MUST keep the old config if the new one is invalid.
- **OPS-4** Secrets (Plex token, provider credentials) MUST be accepted from environment variables or files and MUST NOT appear in logs, the UI, or `/metrics`.
- **OPS-5** Logs MUST be structured JSON lines with a correlation ID per stream session and per replay job.
- **OPS-6** Pitlane MUST be distributed as a single container image that includes `ffmpeg`, plus a plain install path.
- **OPS-7** Pitlane MAY forward warnings and errors to an ntfy server (`notify.ntfy`). Forwarding MUST NOT block streaming; repeats of the same message for the same channel, source or file MUST be throttled; and messages MUST NOT contain secrets (OPS-4) or URL paths, which may carry provider credentials.

## 3. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | Channel tune time (PMS request to first TS byte) ≤ 3 s at p95 for a healthy HLS source on a LAN-hosted instance. |
| NFR-2 | Pitlane adds ≤ 1 s of latency on top of the source's own HLS latency. |
| NFR-3 | Idle footprint ≤ 150 MB RSS. Per stream (stream copy) ≤ 25% of one vCPU and ≤ 80 MB. |
| NFR-4 | Survives source flaps: 1,000 synthetic source up/down transitions in a test run leak no processes or file descriptors. |
| NFR-5 | Every external call has a timeout. No component blocks the event loop for more than 100 ms. |
| NFR-6 | Works with PMS on the same host, on another LAN host, or in Docker with bridge networking (the advertised base URL is configurable). |
| NFR-7 | Runs on linux/amd64 and linux/arm64. |
| NFR-8 | Binds to the LAN by default. The HDHomeRun API has no authentication, so Pitlane MUST warn loudly when bound to a public interface. |

## 4. Technology (recommended, not mandated)

- Python 3.12, FastAPI or Starlette with uvicorn, httpx, pydantic v2 for config and models, and SQLite for state.
- `ffmpeg` ≥ 6 as a subprocess for all media handling.
- `watchfiles` for inbox monitoring. A plain asyncio scheduler (or APScheduler) for jobs.

An implementation in Go or Rust is equally acceptable if it meets the same interfaces.

## 5. Acceptance criteria

1. **Tuner discovery** – PMS (current release) adds Pitlane by `http://<host>:<port>` as a tuner, lists all configured logical channels, and accepts `/xmltv.xml` as the guide.
2. **Live playback** – Playing a channel backed by the local HLS test stream (§6) starts within NFR-1 in Plex Web and one TV client.
3. **Failover** – Killing source A mid-stream switches to source B within `stall_timeout + 3 s`. The client keeps playing or recovers by itself without the user re-tuning.
4. **Guide correctness** – For the fixture calendars (sprint weekend and normal weekend), the XMLTV output matches the golden file byte for byte. Times are correct across a DST change.
5. **Series recording** – A single Plex "record new episodes" rule on `Formula 1` records every session in the fixture window.
6. **Replay filing** – A Plex DVR recording, a filename-only file, and an unmatched file are, respectively, filed by episode number, filed by filename, and sent to `needs-review`. Re-running the job changes nothing.
7. **Spoilers** – With spoiler mode on, no Pitlane-written string contains a driver or team name from the results until the item is marked watched.
8. **Hygiene** – After the soak test (NFR-4) there are no `ffmpeg` processes left, and open file descriptors are back to their baseline.

## 6. Test fixtures

The repository MUST include:

- Recorded Jolpica responses for one normal weekend and one sprint weekend, plus a season list. Tests never hit the live API.
- A script that produces a local HLS test stream (`ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=25 -f lavfi -i sine … -f hls …`) and a "flaky" HTTP proxy that stalls or drops the connection on command.
- Golden XMLTV and golden library-tree outputs.
- A fake PMS (HTTP stub) that records scan and metadata calls, for REP-5/REP-6 tests.
