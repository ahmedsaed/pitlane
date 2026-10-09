# Tuner Emulation and Stream Relay

PMS supports network tuners that speak the HDHomeRun HTTP API. Pitlane implements the subset PMS uses. Confirm the field names against SiliconDust's published developer documentation **[verify]**.

## Endpoints

All responses are JSON unless noted. `BASE` is `server.advertised_url` (e.g. `http://192.168.1.10:5004`), which must be reachable from PMS, not just from Pitlane itself.

### `GET /discover.json`

```json
{
  "FriendlyName": "Pitlane",
  "Manufacturer": "Pitlane",
  "ModelNumber": "HDTC-2US",
  "FirmwareName": "hdhomeruntc_atsc",
  "FirmwareVersion": "20200101",
  "DeviceID": "PL0A1B2C",
  "DeviceAuth": "pitlane",
  "TunerCount": 2,
  "BaseURL": "BASE",
  "LineupURL": "BASE/lineup.json"
}
```

- `DeviceID` is 8 hex characters, generated once and saved in state. It MUST stay the same across restarts, or PMS treats Pitlane as a new device.
- `ModelNumber` and `FirmwareName` are set to values PMS accepts for network tuners. They are configurable in case PMS behavior changes. PMS accepting the default values is **[verified 2026-10-09, real PMS]**.

### `GET /lineup_status.json`

```json
{ "ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"] }
```

### `GET /lineup.json` (also `/lineup.xml`)

```json
[
  { "GuideNumber": "101", "GuideName": "F1 Main Feed", "URL": "BASE/auto/v101" },
  { "GuideNumber": "102", "GuideName": "F1 Onboards",  "URL": "BASE/auto/v102" }
]
```

Only channels with at least one enabled source are listed. Channels whose sources are all offline are still listed, so the lineup doesn't change during an outage.

### `POST /lineup.post?scan=start|abort`

Returns 200 and does nothing. The lineup is config-driven.

### `GET /device.xml`

A UPnP device description, used by some clients for discovery. Pitlane serves a minimal static template filled with `FriendlyName`, `DeviceID`, and `BASE`.

### `GET /auto/v{guide_number}`

The stream endpoint. Response: `200`, `Content-Type: video/mp2t`, chunked transfer, no `Content-Length`.

| Condition | Response |
|---|---|
| Unknown guide number | 404 |
| All tuner slots busy | 503 |
| No source could start within `tune_timeout` (default 10 s) | 503 |

### Discovery

PMS can find HDHomeRun devices automatically by broadcast discovery (UDP 65001). Pitlane MAY answer that **[verify]**. Manual entry of `host:port` in PMS is the supported path, so v1 does not require it.

## Relay pipeline

For a source of kind `hls` or `ts`:

```
ffmpeg -hide_banner -loglevel warning -nostdin \
  [-headers "K: V\r\n…"] [-user_agent …] \
  -rw_timeout 10000000 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
  -i <url> \
  -map 0:v:0? -map 0:a? -c copy \
  -f mpegts -mpegts_flags +resend_headers -muxdelay 0 pipe:1
```

- `file` sources add `-re` so they play at real time, and MAY loop (`-stream_loop -1`) if the channel is set to loop.
- `tuner` sources stream the upstream HDHomeRun URL directly without `ffmpeg`, unless a transcode profile is set.
- Transcode profile (LIVE-8), example `720p`: `-c:v libx264 -preset veryfast -b:v 3500k -maxrate 4000k -bufsize 8000k -vf scale=-2:720 -c:a aac -b:a 160k`. Hardware encoders MAY be selectable later.

### Stall detection and failover (LIVE-7)

```
loop over sources in order:
    start ffmpeg(source)
    forward stdout → client in 64 KiB chunks
    watchdog: if no bytes for stall_timeout → kill ffmpeg, mark source degraded, continue loop
    if ffmpeg exits non-zero → mark source per error, continue loop
    if client disconnects → kill ffmpeg, release slot, return
all sources exhausted → end response
```

- Timestamps restart on every switch. `-mpegts_flags +resend_headers`, together with a fresh PAT/PMT at the start of each segment, is what lets clients resync. Clients that can't resync may stop playback. This is acceptable in v1, and the switch is still logged.
- The relay keeps no buffer beyond ffmpeg's pipe and one chunk, so that latency stays within NFR-2.
- A source that failed during a stream is reprobed immediately, outside the normal schedule.

## Tuner slots

- `tuners.count` (default 2) is advertised as `TunerCount`.
- A slot is held from request start until the response ends.
- PMS often opens a second request when changing channels before closing the first. Pitlane MUST free a slot as soon as the TCP connection closes, not when the next chunk write fails.

## Metrics

`pitlane_streams_active`, `pitlane_stream_failovers_total{channel}`, `pitlane_tune_seconds` (histogram), `pitlane_source_state{source,channel}` (gauge: 0 offline, 1 degraded, 2 online), `pitlane_tuner_rejections_total`.
