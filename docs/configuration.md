# Configuration Reference

Pitlane reads one YAML file, `PITLANE_CONFIG` (default `/config/pitlane.yaml`). Durations take `s`, `m`, `h`, or `d`. Any string value may be `${ENV_VAR}` or `${file:/path}`, and secrets MUST use one of these forms (OPS-4).

## Full example

```yaml
server:
  bind: 0.0.0.0:5004                 # NFR-8: warns if this resolves to a public interface
  advertised_url: http://192.168.1.10:5004
  ui_password: ${file:/config/ui_password}
  data_dir: /data
  log_level: info

tuners:
  count: 2
  tune_timeout: 10s
  stall_timeout: 8s
  device_name: Pitlane

health:
  interval: 5m
  interval_near_session: 1m          # within 30 min of a session
  probe_timeout: 6s
  degraded_latency_ms: 2500

series:
  f1:
    title: Formula 1
    calendar: jolpica
    seasons: auto                    # current season (+ next one once it is published)

channels:
  - id: f1-main
    guide_number: 101
    name: F1 Main Feed
    logo: /config/logos/f1-main.png
    series: [f1]
    transcode: null                  # or "720p"
  - id: f1-alt
    guide_number: 102
    name: F1 Alternate
    series: [f1]

providers:
  - type: m3u
    name: my-iptv
    url: ${file:/config/iptv_url}
    refresh: 6h
    max_connections: 1                # any provider; see docs/tuner.md "Connection limits"
    health_checks: false              # any provider; no probes, tune tries sources directly
    map:
      - match: { tvg-id: "f1\\.main" }
        channel: f1-main
        priority: 10
  - type: channel-json
    name: mine
    path: /config/channels.json
  - type: hdhomerun
    name: antenna
    device: http://192.168.1.50
    map:
      - match: { GuideName: "^Channel 4" }
        channel: f1-alt
        priority: 10
  - type: directory
    name: drop
    path: /inbox/drop

epg:
  days_back: 1
  days_ahead: 14
  pre_roll: 10m
  post_roll: { default: 10m, RACE: 20m }
  durations: { FP1: 60m, FP2: 60m, FP3: 60m, SQ: 45m, SPRINT: 60m, QUALI: 60m, RACE: 120m }
  filler_max: 6h
  manual: []

replays:
  inboxes:
    - /dvr
    - /inbox
  library_root: /library
  file_mode: move                   # move | hardlink | copy
  remux_to: null                    # null | mkv | mp4
  supplementary_mode: episodes      # episodes | extras
  needs_review_dir: /inbox/needs-review
  poll:
    every: 30m
    until_after_race: 72h
  downloads:
    concurrency: 1
    window: null                    # e.g. "01:00-07:00"
    prefer_provider_over_dvr: false

spoiler:
  enabled: true
  reveal_after_watch: true

plex:
  url: http://192.168.1.10:32400
  token: ${PITLANE_PLEX_TOKEN}
  library_section_id: 7
  spoiler_user: null
  spoiler_token: ${file:/config/spoiler_token}

retention:
  enabled: false
  dry_run: true
  keep: { RACE: all, QUALI: all, SPRINT: all, SQ: 2, FP1: 1, FP2: 1, FP3: 1, SUPP: 1 }

plugins:
  dir: /config/plugins
  enabled: []                        # plugin PROVIDER_TYPE names to allow

notify:                              # OPS-7: optional push notifications
  ntfy:
    url: https://ntfy.example.invalid
    topic: pitlane
    username: me                       # or token: ${file:/config/ntfy_token}
    password: ${file:/config/ntfy_password}
    level: warning                     # warning | error
    throttle: 30m                      # same message for the same channel/source/file at most once per window
```

### Notifications (OPS-7)

With `notify.ntfy` set, every `pitlane.*` log record at or above `level` is also published to `<url>` as JSON (`topic`, `title`, `message`, `priority`, `tags`): warnings at priority 3, errors at 4. The title is the log message and the body lists its fields (channel, source, error, …). Sending runs in the background with one retry for network and 5xx errors; a failed send is logged and never re-forwarded. Secrets and URL paths are removed from every message (`http://panel.example:80/…`), so IPTV credentials embedded in stream URLs are never sent. A message held back by `throttle` is counted, and the next one that goes out says how many were held.

## Validation rules

| Rule | Error example |
|---|---|
| `channels[].id` unique, matches `[a-z0-9-]+` | `channels[1].id: duplicate "f1-main"` |
| `guide_number` unique, numeric string or int | `channels[2].guide_number: 101 already used by f1-main` |
| Every `map[].channel` refers to a defined channel | `providers[0].map[0].channel: unknown channel "f1-man"` |
| `advertised_url` is not loopback when `bind` isn't loopback | warning, not an error |
| `plex.*` required only if `replays` is configured | `plex.token: required when replays.inboxes is set` |
| Plugin types must be listed in `plugins.enabled` | `providers[3].type: "foo" not enabled` |
| Unknown keys | error (catches typos) |

## Environment variables

| Variable | Purpose |
|---|---|
| `PITLANE_CONFIG` | Config file path |
| `PITLANE_PLEX_TOKEN` / `PITLANE_PLEX_TOKEN_FILE` | Plex token |
| `PITLANE_SPOILER_TOKEN_FILE` | Token of the spoiler user |
| `PITLANE_LOG_FORMAT` | `json` (default) or `text` |

## Reload

`SIGHUP` or the UI button. Pitlane validates the new file first. If it's valid, it applies it and keeps active streams on their current source. If it's invalid, it logs the errors, keeps the old config, and shows a banner in the UI (OPS-3).
