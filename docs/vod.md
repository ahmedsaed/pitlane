# Movies and shows from an IPTV account (VOD) – plan

Status: **implemented** (2026-10-12). The requirements (`VOD-1` … `VOD-10`) are in `SPEC.md` §2.5; this document keeps the design.

Pitlane already relays an IPTV account's live channels into Plex. Most Xtream Codes accounts also carry a catalogue of movies and series. Plex cannot list a remote URL as a library item, so Pitlane downloads the titles the operator asks for into dedicated Plex libraries, named so Plex matches them exactly.

## 1. What the panel offers [verified 2026-10-12, the operator's Xtream Codes panel]

| | Movies | Series |
|---|---|---|
| Bulk list | `get_vod_streams`: 19,854 titles, 100 categories, 1.4 MB | `get_series`: 6,454 series, 1.6 MB |
| In the bulk list | name (year in only ~10%), category, rating, poster, `added`, `container_extension` (99% mkv), `is_adult` | name, plot, cover, genre, `releaseDate`, `last_modified` |
| Not in the bulk list | TMDB ID, year, size, resolution | TMDB ID, seasons, episodes |
| Per-title details | `get_vod_info`: `tmdb_id`, release date, duration, bitrate, video/audio codec and size, plot, cast | `get_series_info`: seasons and episodes, each with duration, bitrate, codec; no TMDB ID |
| File URL | `/movie/{user}/{pass}/{id}.{ext}` | `/series/{user}/{pass}/{episode_id}.{ext}` |

Samples: a movie was 720p h264/AAC at ~1.1 Mbps (1 h 39 m ≈ 0.8 GB). An episode was 720p h264 at ~1.6 Mbps (59 m ≈ 0.7 GB). Some recent movies are camera recordings, tagged `CAM` in the name. 539 movie names occur more than once (the same film in several categories or qualities).

File URLs redirect to an edge server and support HTTP `Range` (`206` with `Content-Range`), so resume works **[verified 2026-10-12]**; the real size of the sample movie (805,403,509 bytes) matched the bitrate × duration estimate (805.2 MB). Not verified: whether a VOD download counts against the account's single connection the same way a live stream does. Pitlane assumes it does (VOD-4).

## 2. Requirements

See `SPEC.md` §2.5 (`VOD-1` … `VOD-10`).

## 3. Provider: `xtream`

A new provider type that talks to `player_api.php` directly. It replaces the hand-built playlist file the live channels use today.

```yaml
providers:
  - type: xtream
    name: iptv
    url: http://panel.example.invalid:80
    username: ${file:/secrets/iptv_username}
    password: ${file:/secrets/iptv_password}
    max_connections: 1
    health_checks: false
    refresh: 24h                    # live list + VOD catalogue, one round of bulk calls a day
    map:                            # same rules as m3u (name, group-title = category)
      - { match: { name: "^Sky Sports F1 \\[UK\\]$" }, channel: f1-main, priority: 10 }
    vod: true                       # VOD-1
```

- One provider holds one account, so live and VOD share one connection limiter (VOD-4). Two separate providers for the same account would each believe they had the one connection.
- **Live** comes from `get_live_categories` + `get_live_streams`, replacing `secrets/iptv.m3u`. Map rules work as for `m3u`: `name`, `group-title` (the category), and `tvg-id` (`xc.{stream_id}`).
- **Daily request budget:** about 6 bulk calls (live, VOD and series lists plus their categories), plus 1 details call per series followed whose `last_modified` changed (§5.4). Each requested title costs 1 details call plus its download.

## 4. Data

New SQLite tables (`state.py`):

| Table | Holds |
|---|---|
| `vod_titles` | Movies from the bulk list: id, name, normalized name, year parsed from the name, category, rating, poster URL, `added`, extension, flags (`cam`, `adult`). Replaced on each refresh. |
| `vod_series` | Series from the bulk list: id, name, year from `releaseDate`, category, plot, cover, `last_modified`. |
| `vod_search` | FTS5 index over names (Arabic and Latin) for search. |
| `vod_details` | Cached `get_vod_info` / `get_series_info` JSON per id, with fetch time. |
| `vod_follows` | Followed series: series id, from which season or episode, created at. |
| `vod_items` | One row per requested movie or episode: kind, panel id, target path, expected size (bitrate × duration), bytes done, status (`queued`, `running`, `paused`, `done`, `failed`, `cancelled`), attempts, last error, Plex rating key, match state, watched at. |
| `vod_usage` | Bytes downloaded per day, for the data caps (VOD-6). |

Duplicates: titles with the same normalized name and year form one group in the UI. The default pick is the newest non-`CAM` copy. Its resolution is known once details are fetched.

## 5. Components

### 5.1 Catalogue (`vod/catalog.py`)

- Refreshes on the provider's `refresh` schedule and on demand from the UI. Applies in one transaction, so the UI never sees a half-written catalogue.
- Parses `(YYYY)` from names, flags `CAM` / `HDCAM` / `TS`, and keeps `is_adult`.
- Reports what changed (new movies, new or updated series) for the follow logic and the UI's "recently added".

### 5.2 Requests

- **Movie:** fetch details (VOD-3) to get `tmdb_id`, year, extension and expected size. Show the size and resolution before confirming. Then queue it.
- **Episodes, seasons, series:** fetch `get_series_info` and queue the chosen episodes. "Follow" also stores a `vod_follows` row.
- The target path is computed when the item is queued (§5.5), so the UI can show where it will land.

### 5.3 Downloader (`vod/downloader.py`)

- One worker per provider. It takes the oldest queued item and asks the limiter for a **download lease**: a new kind, preemptible like probes (`probe_slot(..., preemptible=True)` already has this behaviour). A stream that needs the slot cancels the download task. The `.part` file is kept and the item goes back to `paused`.
- Plain HTTP GET via `httpx` streaming to `{target}.part`, using `Range: bytes={done}-` on resume. Without `Range` support (verified on the first download), a preempted download restarts from 0, and the UI says so.
- Checks before a download starts:
  - inside `download_window`;
  - under `max_gb_per_day` / `max_gb_per_month`;
  - free space ≥ expected size + `min_free_space`;
  - not within `quiet_around_sessions` (default 30 min) of an F1 session programme, so a race never starts with the line busy.
- Throttling: optional `max_rate` (token bucket), so a download does not starve other devices in the house.
- Completion:
  1. Size matches `Content-Length`.
  2. `ffprobe` reads the container.
  3. `os.replace` into the library.
  4. Targeted Plex scan (VOD-7).
- Failures: retry with backoff (3 attempts, as REP-4). A response that is not media (`describe_non_media`, e.g. the ISP quota page) pauses the whole queue and sends one ntfy warning instead of burning retries.
- Uses Pitlane's existing tools: the shared HTTP client, the redaction rules for logs and ntfy, correlation IDs (`vod-…`), and metrics (`pitlane_vod_bytes_total`, `pitlane_vod_queue{status}`).

### 5.4 Follows

After each catalogue refresh, for each followed series whose `last_modified` changed, fetch `get_series_info` once and queue the episodes that are not in `vod_items` yet. A series that has not changed costs nothing.

### 5.5 Libraries and naming

- Two Plex libraries that the operator creates once (documented in `docs/plex-setup.md`):

  | Library | Type | Agent | Config key |
  |---|---|---|---|
  | IPTV Movies | Movies | Plex Movie | `vod.movies.section_id` |
  | IPTV Shows | TV Shows | Plex TV Series | `vod.shows.section_id` |

- Paths: `vod.movies.root` and `vod.shows.root`. On the reference server they sit next to the existing libraries in `~/Media` (a bind mount of `/mnt/disk3/Media`, 3.5 TB free), like the F1 folders. `.part` files are written next to their target, so they land on the same disk and the final move is a rename.
- **Movies:** `IPTV Movies/Ong Bak 3 (2010) {tmdb-43209}/Ong Bak 3 (2010) {tmdb-43209}.mkv`. The `{tmdb-…}` hint makes Plex's match exact.
- **Episodes:** `IPTV Shows/MobLand (2025)/Season 01/MobLand (2025) - S01E01.mkv`. The panel has no TMDB ID for series, so the match relies on name and year. The UI lets the operator set a TMDB or TVDB ID per series; it is then added as `MobLand (2025) {tmdb-…}` to the folder name.
- Names are sanitized with the existing `replays/naming.sanitize`. Arabic titles are kept as they are.

### 5.6 Match check and retention

- **New libraries:** Plex ignores folder scans in a library that has never had a full scan **[verified 2026-10-12, PMS 1.43.4]**. If the file has not appeared a minute after the folder scan, Pitlane asks for one full scan of that library. A restart between a finished download and its Plex step is caught up at startup.
- **Match check:** after the scan, Pitlane finds the item (scan, then poll, as `wait_for_episode`) and reads its `guid`. A `local://` or `none://` guid means "not matched" (VOD-8).
- **Retention:** per library, `delete_after_watched: 14d` (default off, dry-run supported). This reuses the existing viewCount polling and `_relink_missing` handling. A whole series is never deleted while it is followed; only its watched episodes are.

### 5.7 Web UI

New pages, server-rendered like the current UI:

| Page | Shows |
|---|---|
| Movies & Shows | Search box, category filter, "recently added", posters. Adult titles are hidden, and `CAM` copies are marked. |
| Title | Details (fetched on open, VOD-3), the versions in its duplicate group, expected size, a Request button. For series: seasons and episodes with checkboxes, plus a Follow toggle. |
| Downloads | Queue with progress, speed and ETA; pause/resume/cancel/retry; today's and this month's data use against the caps; free disk space; items Plex did not match, with "Fix Match" links. |

The header gets a short status: "VOD: 2 queued, downloading 41%" or "paused – live stream".

### 5.8 Play now (VOD-11)

The panel caps each stream at about 1.1× the title's bitrate [verified 2026-10-12: a 5 s burst of about 7 MB, then a constant 213 KB/s for a 1.55 Mbit/s movie], so a download takes about as long as the title runs. It is built for watching, so Pitlane also offers watching: a **play channel** ("Pitlane Cinema", 900 by default) on the virtual tuner.

- **Pick:** "▶ Play on channel 900" on a movie or episode page stores the choice (`vod.play` in the state database). Tuning 900 in Plex Live TV starts it within seconds.
- **One connection:** the channel's source belongs to the xtream provider, so it follows LIVE-10 like any live stream: it pauses a running download, and a second live tune waits for it.
- **Pipe, not a second request:** Pitlane reads the panel's response once and feeds the bytes to ffmpeg (remux to MPEG-TS for Plex) and, with `save`, to `{target}.watch`. MP4 files may keep their index at the end, which a pipe cannot reach; they are played by ffmpeg from the URL and not saved.
- **End:** when the title ends the stream ends; a stall ends it too (a title has no backup, and restarting from the top would be worse).
- **Switch:** picking another title while 900 is playing closes the input and opens the new title on the same client stream.
- **Keep:** watched to the end → moved into the library, item `done`, Plex scan and match check as for a download. Stopped early → the `.watch` file replaces a shorter `.part` and the item is `paused` ("partly saved"); Resume downloads the rest with a `Range` request. Nothing is downloaded that was not asked for.
- **Already downloaded:** played from the library file (`-re`), no panel request.
- **Limits of live playback:** Plex's Live TV buffer allows pause and rewind within what was played; skipping ahead and resume-from-position are not available. For those, download the title.

## 6. Configuration

```yaml
vod:
  provider: iptv                     # an xtream provider with vod: true
  movies: { root: /vod/movies, section_id: 6 }
  shows:  { root: /vod/shows,  section_id: 7 }
  catalog_refresh: 24h
  download_window: null              # e.g. "01:00-08:00" local time (epg.timezone)
  max_rate: null                     # e.g. 4MB/s
  max_gb_per_day: null
  max_gb_per_month: null
  min_free_space: 50GB
  quiet_around_sessions: 30m
  delete_after_watched: null         # e.g. 14d
  dry_run_retention: true
  show_adult: false
  play_channel: { guide_number: 900, name: Pitlane Cinema, save: true }   # null turns "Play now" off
```

`docker-compose.yml` mounts `~/Media/IPTV Movies` and `~/Media/IPTV Shows` into both the Pitlane and the Plex containers at the same paths, like the F1 folders.

## 7. Phases

Each phase ships separately with tests. Tests use a fake Xtream panel (like `tests/fake_pms.py`): JSON endpoints, a media file with and without `Range` support, and a quota-page mode.

1. **`xtream` provider for live.** API-built channel list, map rules, one shared limiter. It replaces the playlist file on the server, and the live behaviour stays identical. Test: same sources as the m3u, same request count per day.
2. **Catalogue and browsing.** Tables, daily refresh, FTS search, the Movies & Shows page, and the title page with details fetched on open. No downloads yet.
3. **Movie downloads.**
   - Download lease and preemption by a live tune.
   - Resume, checks, caps, window, throttle.
   - Naming, Plex scan, match check, Downloads page.
   - Verify `Range` on the real panel with the first real download.
   - Tests: preempt mid-download then resume, caps, quota page pauses the queue, no connection overlap.
4. **Series.** Episode, season and series requests; follows driven by `last_modified`; per-series TMDB override.
5. **Retention and polish.** Delete-after-watched with dry-run, ntfy "ready to watch" (optional), metrics, docs (`SPEC.md` VOD-n, `docs/configuration.md`, `docs/plex-setup.md`).

## 8. Decisions for the operator

| Question | Proposed default |
|---|---|
| Where the libraries live | `~/Media/IPTV Movies` and `~/Media/IPTV Shows` (on disk3 via the existing bind mount) |
| Download window | None (any time the connection is free) |
| Data caps | None until usage is known; the Downloads page shows it |
| Retention | Off; or e.g. 14 days after watched |
| "Ready to watch" ntfy message | On, low priority |
| Replace the playlist file with the `xtream` provider for live (phase 1) | Yes; it is required for one shared connection limit |
