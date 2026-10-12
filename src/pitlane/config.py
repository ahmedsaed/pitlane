"""Configuration loading and validation (OPS-3) and secret resolution (OPS-4).

See docs/configuration.md. The file is a single YAML document. Every string
value may be ``${ENV_VAR}`` or ``${file:/path}``; secrets MUST use one of those
forms. Unknown keys are errors. Errors carry the exact path, e.g.
``channels[1].id: duplicate "f1-main"``.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator

from .durations import parse_duration
from .logging import get_logger, secrets
from .models import SessionType

log = get_logger("config")

Duration = Annotated[timedelta, BeforeValidator(parse_duration)]

_SECRET_RE = re.compile(r"^\$\{(?:(file):(.+)|([A-Za-z_][A-Za-z0-9_]*))\}$")
_CHANNEL_ID_RE = re.compile(r"^[a-z0-9-]+$")

# Paths (dotted, without list indexes) whose values are secrets and MUST come
# from ${ENV} / ${file:} (OPS-4).
SECRET_PATHS = {"plex.token", "plex.spoiler_token", "server.ui_password"}

BUILTIN_PROVIDER_TYPES = {"m3u", "channel-json", "directory", "hdhomerun"}


class ConfigError(Exception):
    """Raised when the configuration is invalid. ``errors`` lists ``path: reason`` strings."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid configuration:\n  " + "\n  ".join(errors))


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------- sections


class ServerCfg(Strict):
    # QA-007: default bind not specified; the example value is used.
    bind: str = "0.0.0.0:5004"
    advertised_url: str | None = None
    ui_password: str | None = None
    data_dir: str = "/data"
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @field_validator("bind")
    @classmethod
    def _bind(cls, v: str) -> str:
        host, port = split_bind(v)
        if not (0 < port < 65536):
            raise ValueError(f"port out of range in {v!r}")
        return v

    @field_validator("advertised_url")
    @classmethod
    def _adv(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^https?://", v):
            raise ValueError("must start with http:// or https://")
        return v.rstrip("/") if v else v


class TunersCfg(Strict):
    count: int = Field(2, ge=1, le=64)
    tune_timeout: Duration = timedelta(seconds=10)
    stall_timeout: Duration = timedelta(seconds=8)
    device_name: str = "Pitlane"
    # docs/tuner.md: ModelNumber/FirmwareName "are configurable in case PMS behavior changes". # QA-007
    model_number: str = "HDTC-2US"
    firmware_name: str = "hdhomeruntc_atsc"
    firmware_version: str = "20200101"


class HealthCfg(Strict):
    interval: Duration = timedelta(minutes=5)
    interval_near_session: Duration = timedelta(minutes=1)
    probe_timeout: Duration = timedelta(seconds=6)
    degraded_latency_ms: int = Field(2500, ge=1)


class SeriesCfg(Strict):
    title: str = "Formula 1"
    calendar: Literal["jolpica"] = "jolpica"
    seasons: Literal["auto"] | list[int] = "auto"


class ChannelCfg(Strict):
    id: str
    guide_number: str
    name: str
    logo: str | None = None
    series: list[str] = Field(default_factory=list)
    transcode: Literal["720p"] | None = None  # LIVE-8
    filler_desc: str | None = None  # guide text between programmes (instead of the generic one)
    loop: bool = False  # QA-007; docs/tuner.md: file sources MAY loop "if the channel is set to loop"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _CHANNEL_ID_RE.match(v):
            raise ValueError(f'"{v}" must match [a-z0-9-]+')
        return v

    @field_validator("guide_number", mode="before")
    @classmethod
    def _gn(cls, v: Any) -> str:
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise ValueError("must be a number or numeric string")
        s = str(v).strip()
        if not re.match(r"^\d+(\.\d+)?$", s):
            raise ValueError(f'"{s}" is not numeric')
        return s


class HttpCfg(Strict):
    """PROV-4: per-provider fetch policy."""

    # QA-007: key names/defaults not in configuration.md; interim choice.

    timeout: Duration = timedelta(seconds=10)
    retries: int = Field(3, ge=0, le=10)
    backoff: Duration = timedelta(seconds=1)
    rate_limit_per_sec: float = Field(2.0, gt=0)
    user_agent: str | None = None


class MapRule(Strict):
    match: dict[str, str]
    channel: str
    priority: int | None = None

    @field_validator("match")
    @classmethod
    def _regexes(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("must contain at least one key")
        for k, pattern in v.items():
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"{k}: invalid regex {pattern!r}: {e}") from None
        return v


class ProviderBase(Strict):
    type: str
    name: str
    http: HttpCfg = Field(default_factory=HttpCfg)
    max_connections: int | None = Field(None, ge=1)  # LIVE-10
    health_checks: bool = True  # LIVE-11


class M3uProviderCfg(ProviderBase):
    type: Literal["m3u"]
    url: str | None = None
    path: str | None = None
    refresh: Duration = timedelta(hours=6)
    map: list[MapRule] = Field(default_factory=list)
    map_unmatched: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> "M3uProviderCfg":
        if bool(self.url) == bool(self.path):
            raise ValueError("exactly one of url or path is required")
        if self.map:
            allowed = {"tvg-id", "tvg-name", "group-title", "name"}
            for i, rule in enumerate(self.map):
                bad = set(rule.match) - allowed
                if bad:
                    raise ValueError(f"map[{i}].match: unknown key(s) {sorted(bad)}; allowed: {sorted(allowed)}")
        return self


class ChannelJsonProviderCfg(ProviderBase):
    type: Literal["channel-json"]
    url: str | None = None
    path: str | None = None
    refresh: Duration = timedelta(hours=6)

    @model_validator(mode="after")
    def _one_of(self) -> "ChannelJsonProviderCfg":
        if bool(self.url) == bool(self.path):
            raise ValueError("exactly one of url or path is required")
        return self


class DirectoryProviderCfg(ProviderBase):
    type: Literal["directory"]
    path: str
    recursive: bool = True


class HdhomerunProviderCfg(ProviderBase):
    type: Literal["hdhomerun"]
    device: str
    refresh: Duration = timedelta(hours=6)
    map: list[MapRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _keys(self) -> "HdhomerunProviderCfg":
        allowed = {"GuideNumber", "GuideName"}
        for i, rule in enumerate(self.map):
            bad = set(rule.match) - allowed
            if bad:
                raise ValueError(f"map[{i}].match: unknown key(s) {sorted(bad)}; allowed: {sorted(allowed)}")
        return self


class XtreamProviderCfg(ProviderBase):
    """Xtream Codes panel via ``player_api.php`` (docs/providers.md "xtream", docs/vod.md)."""

    type: Literal["xtream"]
    url: str
    username: str
    password: str
    refresh: Duration = timedelta(hours=24)
    map: list[MapRule] = Field(default_factory=list)
    vod: bool = False  # VOD-1: keep the movie and series catalogue
    user_agent: str = "VLC/3.0.20"

    @model_validator(mode="after")
    def _check(self) -> "XtreamProviderCfg":
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("url: must start with http:// or https://")
        allowed = {"tvg-id", "name", "group-title"}
        for i, rule in enumerate(self.map):
            bad = set(rule.match) - allowed
            if bad:
                raise ValueError(f"map[{i}].match: unknown key(s) {sorted(bad)}; allowed: {sorted(allowed)}")
        if self.refresh < timedelta(hours=1):
            raise ValueError("refresh: at least 1h (every refresh is a round of panel requests)")
        return self


class PluginProviderCfg(BaseModel):
    """Plugin providers get their raw config; the plugin validates the rest (PROV-6)."""

    model_config = ConfigDict(extra="allow")
    type: str
    name: str
    http: HttpCfg = Field(default_factory=HttpCfg)
    max_connections: int | None = Field(None, ge=1)  # LIVE-10
    health_checks: bool = True  # LIVE-11


PROVIDER_MODELS: dict[str, type[BaseModel]] = {
    "m3u": M3uProviderCfg,
    "channel-json": ChannelJsonProviderCfg,
    "directory": DirectoryProviderCfg,
    "hdhomerun": HdhomerunProviderCfg,
    "xtream": XtreamProviderCfg,
}


class ManualProgrammeCfg(Strict):
    """EPG-7 manual programmes, anchored to a session type."""

    channel: str
    title: str
    relative_to: SessionType
    offset: Duration
    duration: Duration
    supplementary_index: int = Field(ge=1, le=89)


def _post_roll(v: Any) -> Any:
    if isinstance(v, str):
        return {"default": v}
    return v


class EpgCfg(Strict):
    days_back: int = Field(1, ge=0, le=30)
    days_ahead: int = Field(14, ge=0, le=60)
    pre_roll: Duration = timedelta(minutes=10)
    post_roll: Annotated[dict[str, Duration], BeforeValidator(_post_roll)] = Field(
        default_factory=lambda: {"default": timedelta(minutes=10), "RACE": timedelta(minutes=20)}
    )
    durations: dict[SessionType, Duration] = Field(default_factory=dict)
    filler_max: Duration = timedelta(hours=6)
    manual: list[ManualProgrammeCfg] = Field(default_factory=list)
    # QA-002 / QA-007: zone used for the "{local time}" in filler sub-titles. Not in
    # configuration.md; default UTC keeps output identical to having no option.
    timezone: str = "UTC"

    @field_validator("post_roll")
    @classmethod
    def _pr_keys(cls, v: dict[str, timedelta]) -> dict[str, timedelta]:
        allowed = {"default"} | {t.value for t in SessionType}
        bad = set(v) - allowed
        if bad:
            raise ValueError(f"unknown key(s) {sorted(bad)}; allowed: default or a session type")
        if "default" not in v:
            v = {"default": timedelta(minutes=10), **v}
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f'unknown time zone "{v}"') from None
        return v

    @field_validator("filler_max")
    @classmethod
    def _fm(cls, v: timedelta) -> timedelta:
        if v <= timedelta(0):
            raise ValueError("must be positive")
        return v

    def post_roll_for(self, t: SessionType) -> timedelta:
        # docs/epg.md: default is 20 min for RACE, 10 min otherwise (see the field
        # default). An operator-supplied mapping replaces the default mapping.
        return self.post_roll.get(t.value, self.post_roll["default"])

    def nominal_minutes(self, t: SessionType, fallback: int) -> int:
        d = self.durations.get(t)
        return int(d.total_seconds() // 60) if d is not None else fallback


class PollCfg(Strict):
    every: Duration = timedelta(minutes=30)
    until_after_race: Duration = timedelta(hours=72)


class DownloadsCfg(Strict):
    concurrency: int = Field(1, ge=1, le=16)
    window: str | None = None
    prefer_provider_over_dvr: bool = False

    @field_validator("window")
    @classmethod
    def _win(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError('must look like "01:00-07:00"')
        return v


class ReplaysCfg(Strict):
    inboxes: list[str] = Field(default_factory=list)
    library_root: str = "/library"
    file_mode: Literal["move", "hardlink", "copy"] = "move"
    remux_to: Literal["mkv", "mp4"] | None = None
    supplementary_mode: Literal["episodes", "extras"] = "episodes"
    needs_review_dir: str = "/inbox/needs-review"
    poll: PollCfg = Field(default_factory=PollCfg)
    downloads: DownloadsCfg = Field(default_factory=DownloadsCfg)
    # docs/replays.md: "size and mtime haven't changed for 30 s". Exposed so tests
    # and slow filesystems can change it; default per spec.
    settle_time: Duration = timedelta(seconds=30)  # QA-007


class SpoilerCfg(Strict):
    enabled: bool = True
    reveal_after_watch: bool = True


class PlexCfg(Strict):
    url: str | None = None
    token: str | None = None
    library_section_id: int | None = None
    spoiler_user: str | None = None
    spoiler_token: str | None = None
    season_poster: str | None = None  # docs/replays.md "season poster from config" # QA-007


RetentionKeep = Literal["all"] | int


class RetentionCfg(Strict):
    enabled: bool = False
    dry_run: bool = True
    keep: dict[SessionType, RetentionKeep] = Field(
        default_factory=lambda: {
            SessionType.RACE: "all",
            SessionType.QUALI: "all",
            SessionType.SPRINT: "all",
            SessionType.SQ: 2,
            SessionType.FP1: 1,
            SessionType.FP2: 1,
            SessionType.FP3: 1,
            SessionType.SUPP: 1,
        }
    )


class PluginsCfg(Strict):
    dir: str | None = None
    enabled: list[str] = Field(default_factory=list)


class NtfyCfg(Strict):
    """OPS-7: warnings and errors as ntfy push notifications."""

    url: str
    topic: str = "pitlane"
    username: str | None = None
    password: str | None = None
    token: str | None = None
    level: Literal["warning", "error"] = "warning"
    throttle: Duration = timedelta(minutes=30)

    @model_validator(mode="after")
    def _check(self) -> "NtfyCfg":
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("url: must start with http:// or https://")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.topic):
            raise ValueError("topic: letters, digits, '-' and '_' only")
        if self.token and (self.username or self.password):
            raise ValueError("use either token or username/password, not both")
        if bool(self.username) != bool(self.password):
            raise ValueError("username and password go together")
        return self


class NotifyCfg(Strict):
    ntfy: NtfyCfg | None = None


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def parse_size(v: Any) -> int:
    """``50GB``, ``4MB``, ``1.5 TB`` or a plain number of bytes."""
    if isinstance(v, bool):
        raise ValueError("expected a size like 50GB")
    if isinstance(v, (int, float)):
        return int(v)
    m = _SIZE_RE.match(str(v))
    if not m:
        raise ValueError(f'"{v}" is not a size (e.g. 50GB, 4MB)')
    return int(float(m.group(1)) * _SIZE_UNITS[(m.group(2) or "B").upper()])


Size = Annotated[int, BeforeValidator(parse_size)]


class VodLibraryCfg(Strict):
    root: str
    section_id: int


PLAY_CHANNEL_ID = "pitlane-cinema"


class PlayChannelCfg(Strict):
    """docs/vod.md "Play now": a channel that plays the title picked in Pitlane."""

    guide_number: str = "900"
    name: str = "Pitlane Cinema"
    save: bool = False  # keep a copy of what is watched (same bytes, no extra requests); off: copies only via Download
    reencode: bool = False  # true: re-encode video (keyframe every 2 s) for long-GOP films in Plex desktop apps; ~1 CPU core

    @field_validator("guide_number", mode="before")
    @classmethod
    def _gn(cls, v: Any) -> str:
        s = str(v).strip()
        if isinstance(v, bool) or not re.match(r"^\d+(\.\d+)?$", s):
            raise ValueError("must be a number or numeric string")
        return s


class VodCfg(Strict):
    """Movies and shows from an Xtream account (docs/vod.md)."""

    provider: str
    movies: VodLibraryCfg
    shows: VodLibraryCfg
    catalog_refresh: Duration = timedelta(hours=24)
    download_window: str | None = None  # VOD-6, local time (epg.timezone)
    max_rate: Size | None = None  # bytes per second
    max_gb_per_day: float | None = Field(None, gt=0)
    max_gb_per_month: float | None = Field(None, gt=0)
    min_free_space: Size = 50 * 1000**3
    quiet_around_sessions: Duration = timedelta(minutes=30)
    delete_after_watched: Duration | None = None  # VOD-9
    dry_run_retention: bool = True
    show_adult: bool = False
    notify_ready: bool = True
    play_channel: PlayChannelCfg | None = Field(default_factory=PlayChannelCfg)

    @field_validator("download_window")
    @classmethod
    def _win(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError('must look like "01:00-07:00"')
        return v

    @field_validator("catalog_refresh")
    @classmethod
    def _refresh(cls, v: timedelta) -> timedelta:
        if v < timedelta(hours=1):
            raise ValueError("at least 1h (every refresh is a round of panel requests)")
        return v


class Config(Strict):
    server: ServerCfg = Field(default_factory=ServerCfg)
    tuners: TunersCfg = Field(default_factory=TunersCfg)
    health: HealthCfg = Field(default_factory=HealthCfg)
    series: dict[str, SeriesCfg] = Field(default_factory=lambda: {"f1": SeriesCfg()})
    channels: list[ChannelCfg] = Field(default_factory=list)
    providers: list[dict[str, Any]] = Field(default_factory=list)
    epg: EpgCfg = Field(default_factory=EpgCfg)
    replays: ReplaysCfg | None = None
    spoiler: SpoilerCfg = Field(default_factory=SpoilerCfg)
    plex: PlexCfg = Field(default_factory=PlexCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    plugins: PluginsCfg = Field(default_factory=PluginsCfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)
    vod: VodCfg | None = None

    # Filled by load_config, not by YAML.
    provider_cfgs: list[Any] = Field(default_factory=list, exclude=True)
    source_path: str | None = Field(default=None, exclude=True)

    def channel(self, channel_id: str) -> ChannelCfg | None:
        for c in self.channels:
            if c.id == channel_id:
                return c
        return None

    def connection_limits(self) -> dict[str, int]:
        """LIVE-10: ``max_connections`` per provider name, for providers that set it."""
        return {p.name: p.max_connections for p in self.provider_cfgs if getattr(p, "max_connections", None)}

    @property
    def replays_enabled(self) -> bool:
        return self.replays is not None and bool(self.replays.inboxes)

    @property
    def config_dir(self) -> Path:
        return Path(self.source_path).parent if self.source_path else Path.cwd()

    @property
    def plugins_dir(self) -> Path:
        return Path(self.plugins.dir) if self.plugins.dir else self.config_dir / "plugins"


# --------------------------------------------------------------------------- helpers


def split_bind(bind: str) -> tuple[str, int]:
    if bind.startswith("["):
        host, _, rest = bind[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, sep, port = bind.rpartition(":")
        if not sep:
            raise ValueError(f"expected host:port, got {bind!r}")
    try:
        return host, int(port)
    except ValueError:
        raise ValueError(f"invalid port in {bind!r}") from None


def _fmt_loc(loc: tuple[Any, ...], prefix: str = "") -> str:
    out = prefix
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def _fmt_errors(e: ValidationError, prefix: str = "") -> list[str]:
    errs = []
    for err in e.errors():
        loc = tuple(p for p in err["loc"] if not (isinstance(p, str) and p.startswith("function-")))
        # Drop union discriminator noise like "literal['auto']".
        loc = tuple(p for p in loc if not (isinstance(p, str) and ("[" in p and "]" in p)))
        path = _fmt_loc(loc, prefix)
        msg = err["msg"]
        if err["type"] == "extra_forbidden":
            msg = "unknown key"
        msg = msg.removeprefix("Value error, ")
        errs.append(f"{path}: {msg}" if path else msg)
    return errs


@dataclass
class _Resolver:
    environ: dict[str, str]
    errors: list[str] = field(default_factory=list)
    secret_form_paths: set[str] = field(default_factory=set)

    def resolve(self, node: Any, path: str = "") -> Any:
        if isinstance(node, dict):
            return {k: self.resolve(v, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [self.resolve(v, f"{path}[{i}]") for i, v in enumerate(node)]
        if isinstance(node, str):
            m = _SECRET_RE.match(node.strip())
            if not m:
                return node
            self.secret_form_paths.add(path)
            if m.group(1):
                fpath = m.group(2)
                try:
                    value = Path(fpath).read_text(encoding="utf-8").strip()
                except OSError as e:
                    self.errors.append(f"{path}: cannot read secret file {fpath}: {e.strerror or e}")
                    return None
            else:
                var = m.group(3)
                if var not in self.environ:
                    self.errors.append(f"{path}: environment variable {var} is not set")
                    return None
                value = self.environ[var]
            secrets.add(value)  # OPS-4: never log resolved values
            return value
        return node


def _read_env_secret(environ: dict[str, str], var: str, file_var: str | None) -> str | None:
    if environ.get(var):
        return environ[var]
    if file_var and environ.get(file_var):
        try:
            return Path(environ[file_var]).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ConfigError([f"{file_var}: cannot read {environ[file_var]}: {e.strerror or e}"]) from None
    return None


def is_loopback_host(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_config(raw: Any, *, environ: dict[str, str] | None = None, source_path: str | None = None) -> Config:
    """Validate an already-parsed YAML document. Raises :class:`ConfigError`."""
    environ = dict(os.environ if environ is None else environ)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(["<root>: expected a mapping"])

    resolver = _Resolver(environ)
    resolved = resolver.resolve(raw)
    errors = list(resolver.errors)

    # OPS-4: secrets MUST use ${ENV} or ${file:}.
    for sp in SECRET_PATHS:
        section, key = sp.split(".")
        sec = raw.get(section)
        if isinstance(sec, dict) and sec.get(key) not in (None, "") and sp not in resolver.secret_form_paths:
            errors.append(f"{sp}: secrets must be given as ${{ENV_VAR}} or ${{file:/path}}, not inline")

    # Env-var fallbacks (configuration.md "Environment variables").
    plex = resolved.setdefault("plex", {}) if isinstance(resolved.get("plex", {}), dict) else None
    if plex is not None:
        try:
            if not plex.get("token"):
                tok = _read_env_secret(environ, "PITLANE_PLEX_TOKEN", "PITLANE_PLEX_TOKEN_FILE")
                if tok:
                    secrets.add(tok)
                    plex["token"] = tok
            if not plex.get("spoiler_token"):
                tok = _read_env_secret(environ, "", "PITLANE_SPOILER_TOKEN_FILE")
                if tok:
                    secrets.add(tok)
                    plex["spoiler_token"] = tok
        except ConfigError as ce:
            errors.extend(ce.errors)

    providers_raw = resolved.get("providers") or []
    try:
        cfg = Config.model_validate(resolved)
    except ValidationError as e:
        errors.extend(_fmt_errors(e))
        raise ConfigError(errors) from None

    cfg.source_path = source_path

    # docs/vod.md "Play now": the play channel is an ordinary channel for the tuner and guide.
    if cfg.vod is not None and cfg.vod.play_channel is not None:
        pc = cfg.vod.play_channel
        cfg.channels.append(ChannelCfg(
            id=PLAY_CHANNEL_ID, guide_number=pc.guide_number, name=pc.name,
            filler_desc="Plays the movie or episode you pick with “Play now” in Pitlane. Pick one, then tune here.",
        ))  # fmt: skip

    # Cross-field rules (configuration.md "Validation rules").
    seen_ids: dict[str, int] = {}
    seen_gn: dict[str, str] = {}
    for i, ch in enumerate(cfg.channels):
        if ch.id in seen_ids:
            errors.append(f'channels[{i}].id: duplicate "{ch.id}"')
        else:
            seen_ids[ch.id] = i
        if ch.guide_number in seen_gn:
            errors.append(f"channels[{i}].guide_number: {ch.guide_number} already used by {seen_gn[ch.guide_number]}")
        else:
            seen_gn[ch.guide_number] = ch.id
        for j, s in enumerate(ch.series):
            if s not in cfg.series:
                errors.append(f'channels[{i}].series[{j}]: unknown series "{s}"')

    provider_cfgs: list[Any] = []
    seen_names: set[str] = set()
    for i, p in enumerate(providers_raw):
        prefix = f"providers[{i}]"
        if not isinstance(p, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        ptype = p.get("type")
        if not ptype:
            errors.append(f"{prefix}.type: required")
            continue
        model = PROVIDER_MODELS.get(ptype)
        if model is None:
            if ptype not in cfg.plugins.enabled:
                errors.append(f'{prefix}.type: "{ptype}" not enabled')
                continue
            model = PluginProviderCfg
        try:
            pc = model.model_validate(p)
        except ValidationError as e:
            errors.extend(_fmt_errors(e, prefix))
            continue
        if pc.name in seen_names:
            errors.append(f'{prefix}.name: duplicate "{pc.name}"')
        if ptype == "xtream":
            # OPS-4: panel credentials end up in stream URLs; keep them out of the file.
            for key in ("username", "password"):
                if f"{prefix}.{key}" not in resolver.secret_form_paths:
                    errors.append(f"{prefix}.{key}: secrets must be given as ${{ENV_VAR}} or ${{file:/path}}, not inline")
        seen_names.add(pc.name)
        for j, rule in enumerate(getattr(pc, "map", []) or []):
            if rule.channel not in seen_ids:
                errors.append(f'{prefix}.map[{j}].channel: unknown channel "{rule.channel}"')
        provider_cfgs.append(pc)
    cfg.provider_cfgs = provider_cfgs

    if cfg.vod is not None:
        vp = next((p for p in provider_cfgs if p.name == cfg.vod.provider), None)
        if vp is None:
            errors.append(f'vod.provider: unknown provider "{cfg.vod.provider}"')
        elif getattr(vp, "type", None) != "xtream" or not getattr(vp, "vod", False):
            errors.append(f'vod.provider: "{cfg.vod.provider}" must be an xtream provider with vod: true')
        if not cfg.plex.url or not cfg.plex.token:
            errors.append("plex.url and plex.token: required when vod is set")
        if cfg.vod.movies.root == cfg.vod.shows.root:
            errors.append("vod.shows.root: must differ from vod.movies.root")

    for i, m in enumerate(cfg.epg.manual):
        if m.channel not in seen_ids:
            errors.append(f'epg.manual[{i}].channel: unknown channel "{m.channel}"')

    if cfg.replays_enabled:
        if not cfg.plex.url:
            errors.append("plex.url: required when replays.inboxes is set")
        if not cfg.plex.token:
            errors.append("plex.token: required when replays.inboxes is set")
        if cfg.plex.library_section_id is None:
            errors.append("plex.library_section_id: required when replays.inboxes is set")

    if errors:
        raise ConfigError(errors)
    return cfg


def config_warnings(cfg: Config) -> list[str]:
    """Non-fatal problems (configuration.md rule on advertised_url; NFR-8)."""
    warnings = []
    host, _ = split_bind(cfg.server.bind)
    if cfg.server.advertised_url and not is_loopback_host(host):
        from urllib.parse import urlparse

        adv_host = urlparse(cfg.server.advertised_url).hostname or ""
        if is_loopback_host(adv_host):
            warnings.append(
                "server.advertised_url: points at loopback while server.bind is not loopback; "
                "PMS on another host will not reach it"
            )
    if not cfg.server.advertised_url:
        warnings.append("server.advertised_url: not set; URLs will be built from the request Host header")
    return warnings


def load_config(path: str | os.PathLike[str] | None = None, *, environ: dict[str, str] | None = None) -> Config:
    """Load and validate the YAML file at ``path`` (default ``$PITLANE_CONFIG`` or ``/config/pitlane.yaml``)."""
    env = dict(os.environ if environ is None else environ)
    p = Path(path or env.get("PITLANE_CONFIG") or "/config/pitlane.yaml")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError([f"{p}: cannot read config file: {e.strerror or e}"]) from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ConfigError([f"{p}: YAML syntax error{where}: {getattr(e, 'problem', e)}"]) from None
    return parse_config(raw, environ=env, source_path=str(p.resolve()))
