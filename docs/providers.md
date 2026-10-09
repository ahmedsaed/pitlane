# Providers

A provider turns operator configuration into **sources** (live) and **replay items** (replays). Every built-in provider is generic and knows nothing about any particular website.

## Interface

The signatures are illustrative Python. Other languages should keep the same shape.

```python
class Provider(Protocol):
    name: str                      # instance name from config, e.g. "home-playlist"
    capabilities: set[Literal["live", "replay"]]

    async def start(self, cfg: dict, ctx: ProviderContext) -> None: ...
    async def stop(self) -> None: ...

    # live
    async def list_sources(self) -> list[SourceSpec]: ...
    async def open(self, locator: str) -> OpenedInput: ...
    async def probe(self, locator: str) -> ProbeResult: ...   # optional; default = generic probe

    # replay
    async def find_replays(self, event: Event) -> list[ReplayItemSpec]: ...
    async def open_replay(self, locator: str) -> OpenedInput: ...
```

```python
@dataclass
class SourceSpec:
    channel: str          # logical channel id (from config mapping or provider data)
    locator: str          # opaque, provider-defined
    kind: Literal["hls", "ts", "file", "tuner"]
    label: str            # human readable, shown in UI
    priority: int = 100   # lower = preferred; config may override

@dataclass
class OpenedInput:
    url_or_path: str
    headers: dict[str, str] = field(default_factory=dict)   # passed to ffmpeg -headers
    ffmpeg_input_args: list[str] = field(default_factory=list)  # allow-listed, see below

@dataclass
class ProbeResult:
    state: Literal["online", "degraded", "offline"]
    latency_ms: int | None
    error: str | None
    info: dict            # e.g. {"video": "h264 1920x1080 50fps", "audio": ["aac 2ch"]}

@dataclass
class ReplayItemSpec:
    provider_item_id: str                 # stable id for de-duplication
    session_type: SessionType | None      # None => matcher decides from title
    title: str
    locator: str
    kind: Literal["hls", "file"]
```

`ProviderContext` gives a provider a shared HTTP client (with timeouts, retries, and per-host rate limits per PROV-4), a logger, a cache directory, and a function for resolving secrets.

### Rules

- A provider MUST return only direct media (`hls`, `ts`, `file`, `tuner`). Web pages and iframes are not sources (SPEC §1.1).
- A provider MUST NOT evaluate remote code (PROV-5).
- `ffmpeg_input_args` is checked against an allow-list (`-re`, `-rw_timeout`, `-reconnect*`, `-live_start_index`, `-user_agent`, `-headers`). Anything else is rejected, so providers can't inject output options or `-f lavfi` sources.
- Default `probe`: for HLS, fetch the playlist (following a master playlist to its first variant) and then one media segment, requiring HTTP 200 within `probe_timeout`. For TS, read 188 × 100 bytes and check sync bytes. For files, check the file exists and that `ffprobe` succeeds. A successful probe slower than `degraded_latency_ms` reports `degraded`.

## Built-in providers

### `m3u` (live)

Reads an extended M3U playlist from a file path or URL.

```yaml
- type: m3u
  name: my-iptv
  url: https://example.invalid/playlist.m3u   # or: path: /config/playlist.m3u
  refresh: 6h
  map:                                        # attach playlist entries to logical channels
    - match: { tvg-id: "f1.main" }            # keys: tvg-id, tvg-name, group-title, name; values are regex
      channel: f1-main
      priority: 10
  map_unmatched: false
```

It parses `#EXTINF` attributes (`tvg-id`, `tvg-name`, `tvg-logo`, `group-title`) and the `#EXTVLCOPT:http-user-agent` and `#EXTVLCOPT:http-referrer` lines, which become `OpenedInput.headers`. Entries that match no rule are ignored unless `map_unmatched: true`, in which case each gets an auto-created channel.

### `channel-json` (live)

A small, documented JSON format for operators who keep their own list.

```json
{
  "version": 1,
  "channels": [
    {
      "channel": "f1-main",
      "label": "Main feed – primary",
      "kind": "hls",
      "url": "https://example.invalid/main/index.m3u8",
      "headers": { "User-Agent": "Pitlane/1.0" },
      "priority": 10,
      "enabled": true
    }
  ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `version` | int | yes | Must be `1` |
| `channels[].channel` | string | yes | Logical channel id (`[a-z0-9-]+`) |
| `channels[].label` | string | yes | |
| `channels[].kind` | `hls`\|`ts` | yes | |
| `channels[].url` | URL | yes | `http`/`https` only |
| `channels[].headers` | object | no | String values only |
| `channels[].priority` | int | no | Default 100 |
| `channels[].enabled` | bool | no | Default `true` |

Unknown fields are ignored. The implementation ships the JSON Schema at `schemas/channel-json.v1.json`.

### `directory` (replay)

Treats a folder as a replay provider. `find_replays(event)` returns the files whose parsed name (see `replays.md`) resolves to that event. Use it as a manual drop folder for anything you already own.

```yaml
- type: directory
  name: drop
  path: /media/f1-drop
  recursive: true
```

### `hdhomerun` (live)

Passes through a real HDHomeRun on the LAN (for example free-to-air broadcasts). It reads the device's `lineup.json` and exposes selected channels as sources, so they can join a logical channel and get guide generation and failover.

```yaml
- type: hdhomerun
  name: antenna
  device: http://192.168.1.50
  map:
    - match: { GuideName: "^Channel 4" }
      channel: f1-main
      priority: 20
```

## Plugin providers (PROV-6)

Plugins are Python modules in `<config_dir>/plugins/*.py` that expose `PROVIDER_TYPE: str` and a class implementing the interface. They load at start and on config reload, run in-process with the same `ProviderContext`, and are subject to the same rules. Pitlane does not distribute plugins and never downloads them.
