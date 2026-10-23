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
all sources exhausted → reconnect to the last playing source (reconnect_attempts, 1 s apart)
                        → still nothing → end response
```

- **Bursty panels.** Some IPTV panels deliver a live channel in bursts: a block of video at several times the channel's bitrate, then nothing until the next block exists, pauses of ~10 s **[observed 2026-10-23, Sky Sports F1 on the operator's panel]**. IPTV apps buffer through that; with `stall_timeout: 8s` every pause looked like a dead source and streams and DVR recordings were cut every 10–40 s. Set `tuners.stall_timeout` above the longest pause (30 s on that server). ffmpeg's own read timeout follows it (`stall_timeout + 5 s`, at least 10 s).
- **Reconnect before giving up (LIVE-7).** A channel with backups still fails over immediately. Only when every source has failed does the relay retry the source that was last playing, up to `tuners.reconnect_attempts` times (default 2), 1 s apart, on the same client response. The budget is restored once a source has played for 30 s. A 401/403 is not retried (the provider refuses the account), nor are local files or the play channel. Observed case: a single-source IPTV channel whose panel cut the connection every 10–20 s; each cut ended the stream after one 8 s stall.

- Timestamps restart on every switch. `-mpegts_flags +resend_headers`, together with a fresh PAT/PMT at the start of each segment, is what lets clients resync. Clients that can't resync may stop playback. This is acceptable in v1, and the switch is still logged.
- The relay keeps no buffer beyond ffmpeg's pipe and one chunk, so that latency stays within NFR-2.
- A source that failed during a stream is reprobed immediately, outside the normal schedule.
- When ffmpeg rejects an HTTP source with "Invalid data found when processing input", the server answered with something that is not media. The relay makes one GET of the source (first 4 KiB, within the tune deadline, holding the source's connection slot) and records what it was as the source's error, e.g. `not a media stream: HTTP 200 text/html from megaplusredirection.tedata.net (redirected), page title "Redirection Page"; an ISP quota/captive-portal page or a provider error page`. Only hosts are reported, never URL paths, which may carry credentials. Probes describe web pages and redirects the same way.

## Tuner slots

- `tuners.count` (default 2) is advertised as `TunerCount`.
- A slot is held from request start until the response ends.
- PMS often opens a second request when changing channels before closing the first. Pitlane MUST free a slot as soon as the TCP connection closes, not when the next chunk write fails.

## Connection limits (LIVE-10)

A provider with `max_connections: N` holds a lease for every upstream connection Pitlane opens to it: one per stream input, one per health probe.

- **Streams before probes.** A tune that needs a slot cancels running health probes on that provider. A probe started at tune time (LIVE-6 last resort) is not cancelled; the tune waits for it.
- **No probes while streaming.** When every slot of the provider is streaming, or a tune is waiting for one, health probes of its sources are skipped and their last state is kept. The live stream is the health signal; the relay marks failures as they happen. Probes of the same provider run one at a time.
- **No overlap on failover.** A failed input keeps its slot until its ffmpeg has exited (SIGTERM, SIGKILL after 3 s). The next source of the same provider waits for it.
- **Busy provider at tune time.** A source whose provider has every slot streaming is put back at the end of the channel's source list, so sources of other providers are tried first. On the second pass the tune waits for a slot within `tune_timeout` (`stall_timeout` mid-stream). This covers PMS opening the new channel before closing the old one. If nothing frees up, the tune fails with 503.

Set `tuners.count` above the provider limit when the account is the only source: with `count: 1`, PMS's overlapping channel-change request would be rejected at the tuner slot before it could wait for the connection.

**Freeing the connection (LIVE-12).** The Overview's "Account connections" card shows what uses each limited provider (a live channel, a download) and has **Free the connection** (for 1 h, 3 h, or until resumed). Pitlane then ends that provider's streams, sends its running download back to the queue (it resumes later), and holds the provider: `acquire_stream`, probes and downloads get no slot, and a tune answers 503 without a warning. **Resume** ends the hold early. Holds live in memory: a restart ends them.

The web UI header shows `<provider> connections N/limit` next to the tuner count for every limited provider (`connections` in `/api/status`). N counts streams, probes and inputs still closing.

## Health checks off (LIVE-11)

Each probe of a `ts`/`hls` source is a real upstream connection. For an IPTV account that is one connection per source every 5 min (every minute near a session), which is the kind of traffic a panel may flag. With `health_checks: false` on the provider:

- no scheduled probes, no reprobe after a mid-stream failure, no probe before a tune;
- sources start as not yet probed and are tried in priority order (LIVE-6). A source that failed during a stream is marked `offline` by the relay and is tried last, directly, without a probe;
- failover (LIVE-7) works as before, because it is driven by the stream itself.

The UI therefore shows only what streams have observed for these sources.

## Metrics

`pitlane_streams_active`, `pitlane_stream_failovers_total{channel}`, `pitlane_tune_seconds` (histogram), `pitlane_source_state{source,channel}` (gauge: 0 offline, 1 degraded, 2 online), `pitlane_tuner_rejections_total`.
