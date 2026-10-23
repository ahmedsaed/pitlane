#!/usr/bin/env python3
"""A "flaky" HTTP proxy that stalls or drops connections on command (SPEC §6).

    python scripts/flaky_proxy.py --upstream http://127.0.0.1:8899 --port 8898

Control endpoints (on the same port):

    GET /_control/mode?set=pass     forward normally (default)
    GET /_control/mode?set=stall    accept requests and active responses but send nothing
    GET /_control/mode?set=drop     close every connection immediately (including active ones)
    GET /_control/mode?set=error    answer 503
    GET /_control/mode?set=unauthorized  answer 401 (an IPTV panel refusing the account)
    GET /_control/mode?set=portal   redirect to an HTML page on "localhost" (like an ISP quota/captive portal)
    GET /_control/mode              show the current mode

Importable as ``FlakyProxy`` for tests. Uses only the standard library and binds to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from urllib.parse import parse_qs, urlsplit

MODES = {"pass", "stall", "drop", "error", "unauthorized", "portal"}

PORTAL_PAGE = b"<!DOCTYPE html><html><head><title>Quota Notice</title></head><body>You have used 100% of your quota.</body></html>"


class FlakyProxy:
    def __init__(self, upstream: str, host: str = "127.0.0.1", port: int = 0) -> None:
        u = urlsplit(upstream)
        if u.scheme != "http":
            raise ValueError("only http upstreams are supported")
        self.up_host = u.hostname or "127.0.0.1"
        self.up_port = u.port or 80
        self.up_prefix = u.path.rstrip("/")
        self.host = host
        self.port = port
        self.mode = "pass"
        self.requests = 0  # proxied (non-control) requests, for tests
        self.server: asyncio.base_events.Server | None = None
        self._conns: set[asyncio.StreamWriter] = set()
        self._mode_changed = asyncio.Event()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode
        self._mode_changed.set()
        self._mode_changed = asyncio.Event()
        if mode == "drop":
            for w in list(self._conns):
                with contextlib.suppress(Exception):
                    w.transport.abort()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            for w in list(self._conns):
                with contextlib.suppress(Exception):
                    w.transport.abort()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conns.add(writer)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line, *header_lines = head.decode("latin-1").split("\r\n")
            method, target, _ = request_line.split(" ", 2)
            if target.startswith("/_control/"):
                await self._control(target, writer)
                return
            if target.startswith("/_portal"):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(PORTAL_PAGE) + PORTAL_PAGE)
                await writer.drain()
                return
            self.requests += 1
            if self.mode == "portal":
                location = f"http://localhost:{self.port}/_portal.html".encode()
                writer.write(b"HTTP/1.1 307 Temporary Redirect\r\nLocation: " + location + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            if self.mode == "drop":
                writer.transport.abort()
                return
            if self.mode == "unauthorized":  # what an IPTV panel answers when it refuses the account
                writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            if self.mode == "error":
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            if self.mode == "stall":
                await self._stall_until_changed()
                return
            await self._forward(method, target, header_lines, writer)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            self._conns.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()

    async def _stall_until_changed(self) -> None:
        while self.mode == "stall":
            await self._mode_changed.wait()

    async def _control(self, target: str, writer: asyncio.StreamWriter) -> None:
        q = parse_qs(urlsplit(target).query)
        if "set" in q:
            try:
                self.set_mode(q["set"][0])
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
        body = f"{self.mode}\n".encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()

    async def _forward(self, method: str, target: str, header_lines: list[str], writer: asyncio.StreamWriter) -> None:
        up_r, up_w = await asyncio.open_connection(self.up_host, self.up_port)
        try:
            headers = [h for h in header_lines if h and not h.lower().startswith(("host:", "connection:"))]
            req = f"{method} {self.up_prefix}{target} HTTP/1.1\r\nHost: {self.up_host}:{self.up_port}\r\nConnection: close\r\n"
            req += "".join(h + "\r\n" for h in headers) + "\r\n"
            up_w.write(req.encode("latin-1"))
            await up_w.drain()
            while True:
                if self.mode == "drop":
                    writer.transport.abort()
                    return
                if self.mode == "stall":
                    await self._stall_until_changed()
                    if self.mode == "drop":
                        writer.transport.abort()
                    return
                data = await up_r.read(16384)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            up_w.close()


async def _amain(args: argparse.Namespace) -> None:
    p = FlakyProxy(args.upstream, port=args.port)
    await p.start()
    print(f"flaky proxy on {p.url} → {args.upstream} (mode={p.mode})", flush=True)
    await asyncio.Event().wait()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, default=8898)
    try:
        asyncio.run(_amain(ap.parse_args()))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
