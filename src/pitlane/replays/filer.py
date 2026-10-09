"""Filer: move / hard-link / copy matched files into the library (REP-3, REP-7).

Idempotent: if the target exists with the same SHA-256 the result is
"already filed" (the source is deleted in ``move`` mode); a different hash
sends the incoming file to needs-review with reason ``conflict``. Pitlane
never overwrites.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..models import SUPP_BASE, SessionType
from ..state import State, now_iso
from .matcher import Match
from .naming import episode_path, extras_path

Outcome = Literal["filed", "already_filed", "conflict", "duplicate"]


def sha256_file(path: Path, bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


async def sha256_async(path: Path) -> str:
    return await asyncio.to_thread(sha256_file, path)


@dataclass
class FileResult:
    outcome: Outcome
    target: Path
    episode_no: int | None
    sha256: str


class Filer:
    def __init__(
        self,
        state: State,
        library_root: Path,
        series_title: str = "Formula 1",
        file_mode: str = "move",
        supplementary_mode: str = "episodes",
        remux_to: str | None = None,
        ffmpeg: str | None = None,
    ) -> None:
        self.state = state
        self.library_root = Path(library_root)
        self.series_title = series_title
        self.file_mode = file_mode
        self.supplementary_mode = supplementary_mode
        self.remux_to = remux_to
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or "ffmpeg"

    # ------------------------------------------------------------------ SUPP numbering
    def _used_supp_numbers(self, season: int, rnd: int) -> set[int]:
        lo, hi = rnd * 100 + SUPP_BASE + 1, rnd * 100 + 99
        used = {
            r["episode_no"]
            for r in self.state.query(
                "SELECT episode_no FROM library_items WHERE season=? AND episode_no BETWEEN ? AND ?", (season, lo, hi)
            )
        }
        season_dir = self.library_root / episode_path(self.series_title, season, 1, "x", "x", ".x").parent
        if season_dir.is_dir():
            import re

            for f in season_dir.iterdir():
                m = re.search(rf"S{season}E(\d{{4}})", f.name)
                if m and lo <= int(m.group(1)) <= hi:
                    used.add(int(m.group(1)))
        return used

    def _next_supp(self, season: int, rnd: int) -> int:
        used = self._used_supp_numbers(season, rnd)
        for n in range(rnd * 100 + SUPP_BASE + 1, rnd * 100 + 100):
            if n not in used:
                return n
        raise RuntimeError(f"no free supplementary episode numbers for {season} round {rnd}")

    # ------------------------------------------------------------------ main
    def target_for(self, m: Match, ext: str, episode_no: int | None) -> Path:
        if m.session_type == SessionType.SUPP and self.supplementary_mode == "extras":
            rel = extras_path(self.series_title, m.event.season, m.event.name, m.title, ext)
        else:
            assert episode_no is not None
            rel = episode_path(self.series_title, m.event.season, episode_no, m.event.name, m.title, ext)
        return self.library_root / rel

    async def file(self, src: Path, m: Match, sha256: str | None = None) -> FileResult:
        sha = sha256 or await sha256_async(src)
        ext = f".{self.remux_to}" if self.remux_to else src.suffix

        # The match decides the target; content elsewhere in the library never does (REP-3).
        existing = [
            r for r in self.state.query("SELECT * FROM library_items WHERE sha256=?", (sha,)) if Path(r["path"]).exists()
        ]
        episode_no = m.episode_no
        if m.session_type == SessionType.SUPP and episode_no is None:
            # A supplementary item without an explicit number has no fixed target until a
            # number is allocated. If this content was already filed as a supplementary item
            # of the same event, that earlier path is its own target (keeps re-runs idempotent).
            same = [r for r in existing if r["session_id"] == f"{m.event.id}-SUPP"]
            if same:
                own = Path(same[0]["path"])
                await self._drop_source(src)
                return FileResult("already_filed", own, same[0]["episode_no"], sha)
            if self.supplementary_mode == "episodes":
                episode_no = self._next_supp(m.event.season, m.event.round)
        target = self.target_for(m, ext, episode_no)

        # REP-3 "Duplicates elsewhere in the library": same bytes under a *different*
        # target path → never delete the incoming file; needs-review with reason duplicate.
        others = [r for r in existing if Path(r["path"]) != target]
        if others and not target.exists():
            return FileResult("duplicate", Path(others[0]["path"]), episode_no, sha)

        if target.exists():
            if await sha256_async(target) == sha:
                await self._drop_source(src)
                self._record(target, m, episode_no, sha)
                return FileResult("already_filed", target, episode_no, sha)
            return FileResult("conflict", target, episode_no, sha)

        target.parent.mkdir(parents=True, exist_ok=True)
        if self.remux_to and src.suffix.lower() != ext.lower():
            await self._remux(src, target)
            if self.file_mode == "move":
                await asyncio.to_thread(src.unlink)
        else:
            await asyncio.to_thread(self._place, src, target)
        self._record(target, m, episode_no, sha)
        return FileResult("filed", target, episode_no, sha)

    def _place(self, src: Path, target: Path) -> None:
        tmp = target.with_name(f".{target.name}.pitlane-tmp")
        if self.file_mode == "hardlink":
            os.link(src, tmp)
        elif self.file_mode == "copy":
            shutil.copy2(src, tmp)
        else:  # move
            try:
                os.link(src, tmp)  # same filesystem: atomic and keeps the source until done
            except OSError:
                shutil.copy2(src, tmp)
        # Never overwrite: link the final name, fail if it appeared meanwhile.
        try:
            os.link(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        if self.file_mode == "move":
            src.unlink()

    async def _remux(self, src: Path, target: Path) -> None:
        tmp = target.with_name(f".{target.name}.pitlane-tmp{target.suffix}")
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
            "-map", "0", "-c", "copy", str(tmp),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )  # fmt: skip
        try:
            _, err = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            tmp.unlink(missing_ok=True)
            raise
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"remux failed: {err.decode(errors='replace')[-300:]}")
        try:
            os.link(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    async def _drop_source(self, src: Path) -> None:
        if self.file_mode == "move" and src.exists():
            await asyncio.to_thread(src.unlink)

    def _record(self, target: Path, m: Match, episode_no: int | None, sha: str) -> None:
        self.state.execute(
            "INSERT INTO library_items(path, session_id, season, episode_no, session_type, sha256, title, spoiler_state, filed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256",
            (
                str(target),
                m.session.id if m.session else f"{m.event.id}-SUPP",
                m.event.season,
                episode_no,
                m.session_type.value,
                sha,
                m.title,
                "hidden",
                now_iso(),
            ),
        )
