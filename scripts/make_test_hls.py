#!/usr/bin/env python3
"""Produce a local live HLS test stream with ffmpeg (SPEC §6).

    python scripts/make_test_hls.py OUT_DIR [--port 8899] [--label A]

Runs ``ffmpeg -re -f lavfi -i testsrc2 … -f lavfi -i sine … -f hls`` writing
a sliding live playlist to OUT_DIR/index.m3u8 and, with --port, serves OUT_DIR
over HTTP on 127.0.0.1. Stop with Ctrl-C. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path


def hls_command(out_dir: Path, *, size: str = "1280x720", rate: int = 25, segment: int = 1, label: str = "") -> list[str]:
    text = f"drawtext=text='{label}':x=20:y=20:fontsize=48:fontcolor=white," if label and _has_drawtext() else ""
    return [
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-re", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
        "-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-vf", f"{text}format=yuv420p".lstrip(","),
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", str(rate * segment),
        "-keyint_min", str(rate * segment), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "96k",
        "-f", "hls", "-hls_time", str(segment), "-hls_list_size", "6",
        "-hls_flags", "delete_segments+omit_endlist",
        str(out_dir / "index.m3u8"),
    ]  # fmt: skip


@functools.cache
def _has_drawtext() -> bool:
    try:
        out = subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10)
        return " drawtext " in out.stdout
    except Exception:
        return False


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass


def serve(directory: Path, port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--port", type=int, default=0, help="serve OUT_DIR on 127.0.0.1:PORT")
    ap.add_argument("--label", default="", help="text burned into the picture (needs drawtext)")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    def _term(signum, frame):  # make SIGTERM run the cleanup below, like Ctrl-C
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    proc = subprocess.Popen(hls_command(args.out_dir, label=args.label), start_new_session=True)
    srv = serve(args.out_dir, args.port) if args.port else None
    if srv:
        print(f"serving http://127.0.0.1:{args.port}/index.m3u8", flush=True)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(5)
        if srv:
            srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
