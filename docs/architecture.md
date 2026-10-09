# Architecture

## Component overview

```
                         ┌──────────────────────── Pitlane ───────────────────────────┐
  operator config ──────▶│ Config loader ──▶ Provider registry                        │
                         │                      │                                     │
  M3U / JSON / dir /     │        ┌─────────────┼───────────────┐                     │
  HDHomeRun / plugins ──▶│        ▼             ▼               ▼                     │
                         │  Live providers   Health checker   Replay providers         │
                         │        │             │               │                     │
                         │        ▼             ▼               ▼                     │
                         │   Channel map ◀── Source state    Download queue ──┐       │
                         │        │                                           ▼       │
                         │        ▼                                    Inbox watcher  │
                         │  Tuner API (HDHomeRun) ◀── Stream relay        │           │
                         │        │                 (ffmpeg per stream)    ▼           │
  Jolpica F1 API ───────▶│  Calendar cache ──▶ XMLTV builder        Matcher ──▶ Filer │
                         │        │                                           │       │
                         │        └────────────────────────────┐              ▼       │
                         │                                     ▼        Plex API client│
                         │                     Web UI / healthz / metrics             │
                         └─────────────────────────────────────────────────────────────┘
                                   │ tuner + xmltv                  │ scan + metadata
                                   ▼                                ▼
                               Plex Media Server (Live TV & DVR, TV library)
```

## Components

| Component | Responsibility | Spec |
|---|---|---|
| Config loader | Parse and validate YAML, resolve secrets, hot reload | OPS-3, OPS-4 |
| Provider registry | Instantiate providers and expose `list_sources()` / `find_replays()` | PROV-* |
| Health checker | Periodic probe of each source and state transitions | LIVE-5 |
| Channel map | Logical channel → ordered sources, stable guide numbers | LIVE-2, LIVE-6 |
| Tuner API | HDHomeRun HTTP endpoints and tuner slot accounting | LIVE-1, LIVE-4 |
| Stream relay | One `ffmpeg` pipeline per active stream, failover, cleanup | LIVE-3, LIVE-7, LIVE-9 |
| Calendar cache | Jolpica fetch, normalization to `Event` and `Session` | EPG-1 |
| XMLTV builder | Programmes, filler, episode numbering | EPG-2..7 |
| Download queue | Replay items → files in the inbox | REP-4 |
| Inbox watcher | Notices new or finished files | REP-1 |
| Matcher | File → (event, session) or `needs-review` | REP-2 |
| Filer | Move or link into the library tree, idempotently | REP-3, REP-7 |
| Plex API client | Targeted scans, metadata writes, watched-state reads | REP-5, REP-6 |
| Web UI | Operator views and actions | OPS-1 |

## Process model

- One asyncio process. Each `ffmpeg` runs as a child process owned by a `StreamSession` or `DownloadJob` object, whose `async with` block guarantees the process is killed (SIGTERM, then SIGKILL after 3 s) on exit (LIVE-9). Children run in their own process group so that the whole group can be killed.
- Scheduled jobs: calendar refresh (6 h), health checks (adaptive, LIVE-5), replay polling (default every 30 min from session end until 72 h after the race), and watched-state sync for spoiler mode (10 min).
- Pitlane's own state lives in SQLite. Media lives on the filesystem. Pitlane never writes to PMS's database directly.

## Data model

```text
Event        id ("2026-16"), season, round, name, circuit, locality, country,
             is_sprint_weekend, updated_at
Session      id ("2026-16-RACE"), event_id, type (FP1|FP2|FP3|SQ|SPRINT|QUALI|RACE|SUPP),
             title, starts_at (UTC), nominal_minutes, episode_no
Channel      id (slug), guide_number, name, logo_url, series_tags[], transcode_profile?
Source       id (hash of provider+locator), channel_id, provider, priority, locator (opaque),
             kind (hls|ts|file|tuner), state, last_ok_at, last_error, latency_ms
StreamRun    id, channel_id, source_id, client_addr, started_at, ended_at, bytes, failovers
ReplayItem   id, provider, provider_item_id, event_id, session_type, title, locator, status
LibraryItem  path, session_id, sha256, plex_rating_key?, metadata_applied_at, spoiler_state
ReviewItem   path, reason, detected_at, resolved_session_id?
```

`Source.locator` is opaque to everything except the provider that created it. The relay calls the provider's `open(locator)` and gets back an input URL plus headers, or a file path.

## Key flows

### Tune a channel

1. PMS calls `GET /auto/v{guide_number}`.
2. The tuner API reserves a slot, or returns 503 (LIVE-4).
3. The channel map returns sources ordered by priority and filtered to `online`/`degraded`.
4. The relay starts `ffmpeg` on the first source and pipes MPEG-TS into the chunked HTTP response.
5. A stall watchdog switches source (LIVE-7) or ends the response when every source is exhausted.
6. On client disconnect, the relay kills `ffmpeg` and releases the slot (LIVE-9).

### File a replay

1. The watcher sees a file whose size has been stable for 30 s, or a file that Plex DVR finished recording (see `replays.md`).
2. The matcher resolves it to a `Session`, or creates a `ReviewItem`.
3. The filer computes the target path and moves or links the file. If the target exists with the same hash, it does nothing.
4. The Plex client scans the target folder, polls for the new item's ratingKey, then writes metadata.

## Security

- The HDHomeRun API is unauthenticated by design, so it binds to the LAN only (NFR-8).
- The web UI requires a password or reverse-proxy auth when bound beyond localhost.
- The Plex token is read from `PITLANE_PLEX_TOKEN` or a file and is redacted in logs.
- Pitlane never runs code obtained from providers (PROV-5). Provider-supplied strings are escaped in the UI and XML.
