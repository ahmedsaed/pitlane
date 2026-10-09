"""Shared HTTP fetch client: timeouts, exponential-backoff retries, per-host rate limits (PROV-4, NFR-5)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .logging import get_logger

log = get_logger("http")

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class FetchPolicy:
    timeout: float = 10.0
    retries: int = 3
    backoff: float = 1.0
    rate_limit_per_sec: float = 2.0
    user_agent: str | None = None

    @classmethod
    def from_cfg(cls, http_cfg) -> "FetchPolicy":  # config.HttpCfg
        return cls(
            timeout=http_cfg.timeout.total_seconds(),
            retries=http_cfg.retries,
            backoff=http_cfg.backoff.total_seconds(),
            rate_limit_per_sec=http_cfg.rate_limit_per_sec,
            user_agent=http_cfg.user_agent,
        )


class _HostLimiter:
    """Minimum spacing between request starts per host."""

    def __init__(self) -> None:
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str, rate: float) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            nxt = self._next.get(host, now)
            if nxt > now:
                await asyncio.sleep(nxt - now)
            self._next[host] = max(now, nxt) + 1.0 / rate


class FetchClient:
    """Wraps one :class:`httpx.AsyncClient` shared by providers and the calendar."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, user_agent: str = "Pitlane/0.1") -> None:
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=True, headers={"User-Agent": user_agent})
        self._limiter = _HostLimiter()

    @property
    def raw(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        policy: FetchPolicy | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET with retries on network errors and retryable statuses. Raises on final failure."""
        return await self.request("GET", url, policy=policy, headers=headers, params=params)

    async def request(
        self,
        method: str,
        url: str,
        policy: FetchPolicy | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        policy = policy or FetchPolicy()
        host = urlparse(url).netloc
        hdrs = dict(headers or {})
        if policy.user_agent and "User-Agent" not in hdrs:
            hdrs["User-Agent"] = policy.user_agent
        attempt = 0
        while True:
            await self._limiter.wait(host, policy.rate_limit_per_sec)
            try:
                resp = await self._client.request(method, url, headers=hdrs, params=params, timeout=policy.timeout)
                if resp.status_code in RETRY_STATUSES and attempt < policy.retries:
                    raise _Retryable(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp
            except (httpx.TransportError, _Retryable) as e:
                if attempt >= policy.retries:
                    if isinstance(e, _Retryable):
                        raise httpx.HTTPError(f"{e} after {attempt + 1} attempts") from None
                    raise
                delay = policy.backoff * (2**attempt)
                log.debug("retrying request", extra={"host": host, "attempt": attempt + 1, "delay_s": delay})
                attempt += 1
                await asyncio.sleep(delay)


class _Retryable(Exception):
    pass
