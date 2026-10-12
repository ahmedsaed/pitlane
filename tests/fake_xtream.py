"""A fake Xtream Codes panel and a fake Plex for the VOD tests, served through one
``httpx.MockTransport`` (no sockets). Counts every panel request."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

PANEL = "http://panel.example.invalid:80"
PLEX = "http://plex.example.invalid:32400"
USER, PASS = "user1", "pass1"


def make_media(seconds: int = 2, gop: int = 10) -> bytes:
    """A small real MKV (ffmpeg testsrc), or filler bytes when ffmpeg is missing."""
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("ffmpeg"):
        return b"\x1aE\xdf\xa3" + bytes(range(256)) * 400
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "t.mkv"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}",
             "-c:v", "libx264", "-preset", "ultrafast", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", str(out)],
            check=True,
        )  # fmt: skip
        return out.read_bytes()


class FakePanel:
    def __init__(self, media: bytes) -> None:
        self.media = media
        self.calls: Counter[str] = Counter()  # player_api actions
        self.downloads: list[dict] = []  # media requests: path, range
        self.mode = "ok"  # ok | slow | portal | norange
        self.chunk_delay = 0.0
        self.vod_categories = [{"category_id": "1", "category_name": "English Movies"}, {"category_id": "2", "category_name": "افلام عربيه"}]
        self.movies = [
            {"stream_id": 101, "name": "Ong Bak 3 (2010)", "category_id": "1", "rating": "5.8", "stream_icon": "https://img.example.invalid/ob3.jpg", "added": "1600000000", "container_extension": "mkv", "is_adult": 0},
            {"stream_id": 102, "name": "Ong Bak 3 (2010) CAM", "category_id": "1", "rating": "5", "stream_icon": f"{PANEL}/images/x.jpg", "added": "1500000000", "container_extension": "mkv", "is_adult": 0},
            {"stream_id": 103, "name": "حق ميت", "category_id": "2", "rating": "7", "stream_icon": "", "added": "1700000000", "container_extension": "mp4", "is_adult": 0},
            {"stream_id": 105, "name": "Second Film (2021)", "category_id": "1", "rating": "6", "stream_icon": "", "added": "1650000000", "container_extension": "mkv", "is_adult": 0},
            {"stream_id": 104, "name": "Adult Thing", "category_id": "1", "rating": "1", "stream_icon": "", "added": "1700000001", "container_extension": "mkv", "is_adult": "1"},
        ]
        self.series_categories = [{"category_id": "9", "category_name": "English Series"}]
        self.series = [
            {"series_id": 501, "name": "MobLand", "category_id": "9", "releaseDate": "2025-03-30", "cover": "https://img.example.invalid/mob.jpg", "plot": "Crime.", "last_modified": "1700000000"},
        ]
        self.episodes = {
            "1": [self._ep(9001, 1, 1, 1600000000), self._ep(9002, 1, 2, 1600000000)],
            "2": [self._ep(9101, 2, 1, 1700000000)],
        }

    @staticmethod
    def _ep(eid: int, season: int, num: int, added: int) -> dict:
        return {"id": str(eid), "episode_num": num, "season": season, "title": f"Episode {num}", "container_extension": "mkv",
                "added": str(added), "info": {"duration_secs": 3540, "bitrate": 1632, "video": {"width": 1280, "height": 720, "codec_name": "h264"}}}  # fmt: skip

    def add_episode(self, eid: int, season: int, num: int, added: int, last_modified: int) -> None:
        self.episodes.setdefault(str(season), []).append(self._ep(eid, season, num, added))
        self.series[0]["last_modified"] = str(last_modified)

    @property
    def panel_requests(self) -> int:
        return sum(self.calls.values()) + len(self.downloads)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        if url.path == "/player_api.php":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if q.get("username") != USER or q.get("password") != PASS:
                return httpx.Response(200, json={"user_info": {"auth": 0}})
            action = q.get("action", "")
            self.calls[action] += 1
            if action == "get_live_categories":
                return httpx.Response(200, json=[{"category_id": "23", "category_name": "England"}])
            if action == "get_live_streams":
                return httpx.Response(200, json=[
                    {"stream_id": 5396, "name": "Sky Sports F1 [UK]", "category_id": "23"},
                    {"stream_id": 5397, "name": " Sky Sports Main Event [UK] ", "category_id": "23"},
                ])  # fmt: skip
            if action == "get_vod_categories":
                return httpx.Response(200, json=self.vod_categories)
            if action == "get_vod_streams":
                return httpx.Response(200, json=self.movies)
            if action == "get_series_categories":
                return httpx.Response(200, json=self.series_categories)
            if action == "get_series":
                return httpx.Response(200, json=self.series)
            if action == "get_vod_info":
                vid = int(q["vod_id"])
                m = next(x for x in self.movies if x["stream_id"] == vid)
                return httpx.Response(200, json={
                    "info": {"tmdb_id": "43209" if vid == 101 else "", "name": m["name"].replace(" (2010)", "").replace(" CAM", ""),
                             "releasedate": "2010-05-05" if vid == 101 else "", "duration_secs": 5926, "bitrate": 1087,
                             "video": {"width": 1280, "height": 720, "codec_name": "h264"}, "audio": {"codec_name": "aac"}, "plot": "Fights."},
                    "movie_data": {"stream_id": vid, "name": m["name"], "container_extension": m["container_extension"]},
                })  # fmt: skip
            if action == "get_series_info":
                return httpx.Response(200, json={"info": {"name": "MobLand", "releaseDate": "2025-03-30"}, "episodes": self.episodes})
            return httpx.Response(200, json=[])
        m = re.match(rf"^/(movie|series)/{USER}/{PASS}/(\w+)\.(\w+)$", url.path)
        if m:
            rng = request.headers.get("range")
            self.downloads.append({"path": f"/{m.group(1)}/…/{m.group(2)}.{m.group(3)}", "range": rng})
            if self.mode == "missing":
                return httpx.Response(404, headers={"content-type": "text/plain"}, content=b"not found")
            if self.mode == "portal":
                return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html><head><title>Quota Notice</title></head></html>")
            start = 0
            if rng and self.mode != "norange":
                start = int(re.match(r"bytes=(\d+)-", rng).group(1))
                if start >= len(self.media):
                    return httpx.Response(416, headers={"content-range": f"bytes */{len(self.media)}"})
            body = self.media[start:]
            headers = {"content-type": "video/x-matroska", "content-length": str(len(body))}
            status = 200
            if start:
                status = 206
                headers["content-range"] = f"bytes {start}-{len(self.media) - 1}/{len(self.media)}"
            if self.mode == "slow":
                delay = self.chunk_delay

                async def gen():
                    for i in range(0, len(body), 4096):
                        await asyncio.sleep(delay)
                        yield body[i : i + 4096]

                return httpx.Response(status, headers=headers, content=gen())
            return httpx.Response(status, headers=headers, content=body)
        return httpx.Response(404)


class FakePlex:
    """Sections whose content is whatever files exist under their roots (a "scan" is instant)."""

    def __init__(self, roots: dict[int, Path], unmatched: set[str] | None = None) -> None:
        self.roots = roots
        self.scans: list[tuple[int, str]] = []
        self.views: dict[str, int] = {}
        self.unmatched = unmatched or set()
        self.ignore_folder_scans = False  # like a new Plex library before its first full scan
        self.full_scans: set[int] = set()

    def _items(self, section: int, type_: int) -> list[dict]:
        out: list[dict] = []
        if self.ignore_folder_scans and section not in self.full_scans:
            return out
        for f in sorted(self.roots[section].rglob("*")):
            if not f.is_file() or f.name.endswith(".part"):
                continue
            rk = str(abs(hash(str(f))) % 100000)
            guid = "local://1" if any(u in f.name for u in self.unmatched) else ("plex://movie/x" if type_ == 1 else "plex://episode/y")
            out.append({"ratingKey": rk, "title": f.stem, "guid": guid, "viewCount": self.views.get(rk, 0),
                        "Media": [{"Part": [{"file": str(f)}]}]})  # fmt: skip
        return out

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "abc123"}})
        m = re.match(r"^/library/sections/(\d+)/(refresh|all)$", url.path)
        if m:
            section = int(m.group(1))
            if m.group(2) == "refresh":
                self.scans.append((section, q.get("path", "")))
                if "path" not in q:
                    self.full_scans.add(section)
                return httpx.Response(200)
            return httpx.Response(200, json={"MediaContainer": {"Metadata": self._items(section, int(q.get("type", 1)))}})
        m = re.match(r"^/library/metadata/(\d+)$", url.path)
        if m:
            for section in self.roots:
                for it in self._items(section, 1):
                    if it["ratingKey"] == m.group(1):
                        return httpx.Response(200, json={"MediaContainer": {"Metadata": [it]}})
            return httpx.Response(404)
        return httpx.Response(404)


def transport(panel: FakePanel, plex: FakePlex) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "panel.example.invalid":
            return await panel.handle(request)
        if host == "plex.example.invalid":
            return await plex.handle(request)
        if host == "api.jolpi.ca":
            return httpx.Response(503)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def write_secrets(d: Path) -> dict[str, str]:
    (d / "u").write_text(USER)
    (d / "p").write_text(PASS)
    (d / "plex").write_text("plex-token-1")
    return {"username": f"${{file:{d / 'u'}}}", "password": f"${{file:{d / 'p'}}}", "plex": f"${{file:{d / 'plex'}}}"}


def config(tmp: Path, **vod_extra) -> dict:
    s = write_secrets(tmp)
    return {
        "server": {"bind": "127.0.0.1:5998", "advertised_url": "http://127.0.0.1:5998", "data_dir": str(tmp / "data")},
        "channels": [{"id": "f1-main", "guide_number": 101, "name": "F1 Main Feed", "series": ["f1"]}],
        "providers": [{
            "type": "xtream", "name": "iptv", "url": PANEL, "username": s["username"], "password": s["password"],
            "max_connections": 1, "health_checks": False, "vod": True, "http": {"rate_limit_per_sec": 100},
            "map": [{"match": {"name": "^Sky Sports F1 \\[UK\\]$"}, "channel": "f1-main", "priority": 10}],
        }],  # fmt: skip
        "plex": {"url": PLEX, "token": s["plex"]},
        "vod": {
            "provider": "iptv",
            "movies": {"root": str(tmp / "IPTV Movies"), "section_id": 6},
            "shows": {"root": str(tmp / "IPTV Shows"), "section_id": 7},
            "min_free_space": "1MB",
            "quiet_around_sessions": "0s",
            **vod_extra,
        },
    }


def dump(o: object) -> str:
    return json.dumps(o)
