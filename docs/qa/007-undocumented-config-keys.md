# 007 – Configuration keys needed by the spec but missing from `configuration.md`
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): PROV-4, OPS-3, LIVE-1, REP-1, REP-5

## Question
Unknown keys are errors (OPS-3), but several behaviors the spec calls configurable have no documented key. Please confirm or rename:

| Key (interim) | Why | Default |
|---|---|---|
| `providers[].http.{timeout,retries,backoff,rate_limit_per_sec,user_agent}` | PROV-4 "configurable timeouts, retry with exponential backoff, and per-host rate limits" | `10s`, `3`, `1s`, `2.0`, none |
| `tuners.model_number`, `tuners.firmware_name`, `tuners.firmware_version` | `tuner.md`: "They are configurable in case PMS behavior changes" | `HDTC-2US`, `hdhomeruntc_atsc`, `20200101` |
| `channels[].loop` | `tuner.md`: file sources loop "if the channel is set to loop" | `false` |
| `replays.settle_time` | `replays.md`: 30 s stability rule; also used by tests | `30s` |
| `plex.season_poster` | `replays.md`: "season poster from config" | none |
| `epg.timezone` | see QA-002 | `UTC` |
| `providers[].refresh` for `channel-json` and `hdhomerun` | only `m3u` documents `refresh` | `6h` |

Also: the spec does not name a default for `server.bind`. The interim default is `0.0.0.0:5004` (as in the example), with the NFR-8 warning when any interface has a public address.

Interim behavior (`# QA-007` in `src/pitlane/config.py`): the keys above, with those defaults.

## Answer
- Answered by:
- Approved by:
