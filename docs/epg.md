# Calendar and XMLTV Guide

## Calendar source

Pitlane uses the Jolpica F1 API (an Ergast-compatible successor), which is free and public.

| Purpose | Request |
|---|---|
| Season schedule | `GET https://api.jolpi.ca/ergast/f1/{season}/races/?format=json` |
| Race results (spoiler-gated) | `GET https://api.jolpi.ca/ergast/f1/{season}/{round}/results/?format=json` |
| Sprint results | `GET https://api.jolpi.ca/ergast/f1/{season}/{round}/sprint/?format=json` |
| Qualifying results | `GET https://api.jolpi.ca/ergast/f1/{season}/{round}/qualifying/?format=json` |

Races are found at `MRData.RaceTable.Races[]`. The relevant fields for each race are:

```
season, round, raceName, date, time,
Circuit { circuitName, Location { locality, country } },
FirstPractice { date, time }, SecondPractice?, ThirdPractice?,
SprintQualifying? (older seasons: SprintShootout?), Sprint?, Qualifying { date, time }
```

- Times are UTC with a trailing `Z`. If `time` is missing, the session is scheduled as date-only, shown as an all-day placeholder programme and flagged in the UI.
- A weekend is a sprint weekend if `Sprint` is present.
- Rate limits: respect Jolpica's published limits (at the time of writing, a few requests per second and a few hundred per hour, unauthenticated) **[verify]**. Pitlane makes about one schedule request every 6 h, plus results requests only when spoiler mode allows. Responses are cached in `<data_dir>/jolpica/`, and the cache is used as-is when the API is unreachable.

## Normalization

| Jolpica field | Session type | Title | Nominal minutes | Order |
|---|---|---|---|---|
| FirstPractice | FP1 | Practice 1 | 60 | 1 |
| SecondPractice | FP2 | Practice 2 | 60 | 2 |
| ThirdPractice | FP3 | Practice 3 | 60 | 3 |
| SprintQualifying / SprintShootout | SQ | Sprint Qualifying | 45 | 2 |
| Sprint | SPRINT | Sprint | 60 | 3 |
| Qualifying | QUALI | Qualifying | 60 | 4 (normal) / 4 (sprint) |
| race date/time | RACE | Race | 120 | 5 |

"Order" is the session's position within the weekend. On a sprint weekend it is FP1=1, SQ=2, SPRINT=3, QUALI=4, RACE=5. On a normal weekend it is FP1=1, FP2=2, FP3=3, QUALI=4, RACE=5. Durations are configurable per type.

### Episode numbering

`episode_no = round × 100 + order` (Australian GP 2026 race → `105`; Round 16 qualifying → `1604`).

- Season = the championship year.
- Supplementary items (REP-7) use `round × 100 + 10 + n`, starting at 11.
- The same numbering is used in XMLTV, recordings, and the replay library, so DVR recordings land in the same episode slot as replays.

## XMLTV output (`GET /xmltv.xml`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE tv SYSTEM "xmltv.dtd">
<tv generator-info-name="Pitlane">
  <channel id="f1-main.pitlane">
    <display-name>F1 Main Feed</display-name>
    <display-name>101</display-name>
    <icon src="BASE/static/logos/f1-main.png"/>
  </channel>

  <programme start="20261011120000 +0000" stop="20261011142000 +0000" channel="f1-main.pitlane">
    <title lang="en">Formula 1</title>
    <sub-title lang="en">Singapore Grand Prix – Race</sub-title>
    <desc lang="en">Round 17 of the 2026 season at Marina Bay Street Circuit, Singapore.</desc>
    <category lang="en">Sports</category>
    <category lang="en">Motorsport</category>
    <episode-num system="xmltv_ns">2025.1704.</episode-num>
    <episode-num system="onscreen">S2026E1705</episode-num>
    <date>20261011</date>
    <new/>
  </programme>
</tv>
```

Rules:

- `xmltv_ns` is zero-based: `season-1 . episode-1 . part`. Example: S2026E1705 → `2025.1704.`.
- `start` = official start − `pre_roll` (default 10 min). `stop` = start + nominal + `post_roll` (default 20 min for RACE, 10 min otherwise).
- When two padded windows on the same channel overlap, the earlier programme's `stop` is cut to the later programme's `start`. Programmes never overlap.
- `<new/>` is set for every session programme, so that "record new episodes only" rules in Plex fire. A single series rule scheduling every session across weekends is **[verified 2026-10-09, real PMS]**. Filler is never marked new.
- Filler: gaps longer than 0 are filled with programmes titled `{channel name}` with sub-title `Next: {next session title} – {local time}`, split at midnight UTC, each at most 6 h long. This fills the guide without flooding Plex with entries.
- Channel IDs are `{channel_id}.pitlane` and stay stable (LIVE-2).
- Window: `epg.days_back` (default 1) through `epg.days_ahead` (default 14).
- Output is deterministic (sorted by channel, then start time; fixed attribute order), so golden-file tests work.
- `Cache-Control: max-age=900` and an `ETag` header are set. PMS refreshes the guide on its own schedule.

Only channels tagged for a series (config `series: [f1]`) get that series' programmes. Other channels get filler only.

## Manual programmes (EPG-7)

```yaml
epg:
  manual:
    - channel: f1-main
      title: Pre-Race Build-Up       # becomes sub-title; series title stays "Formula 1"
      relative_to: RACE              # anchor session
      offset: -90m
      duration: 75m
      supplementary_index: 1         # → episode round*100 + 11
```

Manual items repeat every weekend because they're anchored to a session type, not a date.
