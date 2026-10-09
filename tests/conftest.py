from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"
UPDATE_GOLDEN = os.environ.get("PITLANE_UPDATE_GOLDEN") == "1"

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed; integration test skipped")


def jolpica_fixture(name: str) -> dict:
    return json.loads((FIXTURES / "jolpica" / name).read_text(encoding="utf-8"))


def assert_golden(name: str, data: bytes) -> None:
    """Byte-for-byte golden comparison. Set PITLANE_UPDATE_GOLDEN=1 to rewrite."""
    path = GOLDEN / name
    if UPDATE_GOLDEN or not path.exists():
        if not UPDATE_GOLDEN:
            pytest.fail(f"golden file {path} missing; run with PITLANE_UPDATE_GOLDEN=1 to create it")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    expected = path.read_bytes()
    if expected != data:
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                expected.decode("utf-8", "replace").splitlines(),
                data.decode("utf-8", "replace").splitlines(),
                "golden",
                "actual",
                lineterm="",
                n=2,
            )
        )
        pytest.fail(f"output differs from golden {name}:\n{diff[:6000]}")


@pytest.fixture
def calendar_2026():
    from pitlane.calendar.jolpica import Calendar, normalize_schedule

    return Calendar(normalize_schedule(jolpica_fixture("2026-races.json")))
