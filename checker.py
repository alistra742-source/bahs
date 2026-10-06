"""Proxy validation: liveness, latency, exit IP and anonymity classification.

One client per checked proxy carries both the judge request and the platform
probes, so the proxy handshake is paid once. The judge is a single request
that returns *both* the exit IP as the target saw it and the request headers
the target received -- the two facts anonymity turns on.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from config import (
    CONNECT_TIMEOUT,
    JUDGE_FALLBACKS,
    JUDGE_TIMEOUT,
    JUDGE_URL,
    READ_TIMEOUT,
)
from platforms import PlatformResult, probe_all

log = logging.getLogger("proxy-scraper.checker")

# Forwarding headers whose presence (or whose value containing our real IP)
# decides transparency. Lower-cased; httpbin and postman-echo both lower-case.
FORWARD_HEADERS = (
    "x-forwarded-for",
    "x-forwarded",
    "forwarded",
    "x-real-ip",
    "client-ip",
    "x-client-ip",
    "x-originating-ip",
    "via",
    "x-proxy-id",
    "proxy-connection",
)

HTTPX_SCHEME = {"http": "http", "https": "http", "socks4": "socks4", "socks5": "socks5"}


@dataclass
class CheckResult:
    proxy: str
    protocol: str
    host: str
    port: int
    ok: bool
    latency_ms: float | None = None
    anonymity: str = "unknown"  # elite | anonymous | transparent | unknown
    exit_ip: str | None = None
    platforms: dict[str, PlatformResult] = field(default_factory=dict)
    error: str = ""

    @property
    def platforms_passed(self) -> int:
        return sum(1 for r in self.platforms.values() if r.ok)


def httpx_proxy_url(protocol: str, host: str, port: int) -> str:
    scheme = HTTPX_SCHEME.get(protocol, "http")
    return f"{scheme}://{host}:{port}"


def parse_proxy(proxy: str) -> tuple[str, str, int]:
    """Split ``proto://host:port`` into ``(protocol, host, port)``."""
    proto, rest = proxy.split("://", 1)
    host, port_s = rest.rsplit(":", 1)
    return proto, host, int(port_s)


def _headers_of(payload: object) -> dict[str, str]:
    if isinstance(payload, dict):
        headers = payload.get("headers")
        if isinstance(headers, dict):
            return {str(k).lower(): str(v) for k, v in headers.items()}
    return {}


def _origin_of(payload: object) -> str | None:
    if isinstance(payload, dict):
        origin = payload.get("origin")
        if isinstance(origin, str) and origin and origin != "null":
            # httpbin may report "ip, ip" when chained; take the first hop.
            return origin.split(",")[0].strip()
    return None


def classify_anonymity(headers: dict[str, str], exit_ip: str | None, direct_ip: str | None) -> str:
    if direct_ip is None:
        direct_ip = ""
    # If our real IP leaks in any forwarding header, the proxy is transparent.
    for key in FORWARD_HEADERS:
        value = headers.get(key, "")
        if direct_ip and direct_ip in value:
            return "transparent"
    if exit_ip and direct_ip and exit_ip == direct_ip:
        return "transparent"
    if exit_ip and (not direct_ip or exit_ip != direct_ip):
        # Hiding the IP. Forwarding headers present but no real IP = anonymous;
        # a clean header surface = elite.
        if any(headers.get(k) for k in FORWARD_HEADERS):
            return "anonymous"
        return "elite"
    # No exit IP and forwarding headers present -> at best anonymous.
    if any(headers.get(k) for k in FORWARD_HEADERS):
        return "anonymous"
    return "unknown"


async def get_direct_ip() -> str | None:
    """The validator's own public IP, for transparency comparison."""
    urls = [JUDGE_URL, *JUDGE_FALLBACKS]
    timeout = httpx.Timeout(JUDGE_TIMEOUT, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            ip = _origin_of(resp.json() if _is_json(resp) else {}) or _origin_from_text(resp.text)
            if ip:
                return ip
    return None


def _is_json(resp: httpx.Response) -> bool:
    ctype = resp.headers.get("content-type", "")
    return "json" in ctype


def _origin_from_text(text: str) -> str | None:
    # azenv-style plain text: "REMOTE_ADDR = 1.2.3.4"
    for line in text.splitlines():
        if "remote_addr" in line.lower() and "=" in line:
            return line.split("=", 1)[1].strip()
    return None


async def check_proxy(proxy: str, direct_ip: str | None) -> CheckResult:
    protocol, host, port = parse_proxy(proxy)
    proxy_url = httpx_proxy_url(protocol, host, port)
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=JUDGE_TIMEOUT)

    result = CheckResult(proxy=proxy, protocol=protocol, host=host, port=port, ok=False)

    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            # Judge and platform probes are independent, so both start at once:
            # an alive proxy pays one round trip instead of two. The judge is
            # still the gate -- a proxy it rejects has its probes cancelled
            # rather than awaited, so a dead host costs only the connect timeout.
            started = time.perf_counter()
            probes = asyncio.create_task(probe_all(client, timeout))
            try:
                payload: object = {}
                try:
                    resp = await client.get(JUDGE_URL, timeout=timeout)
                    resp.raise_for_status()
                    payload = resp.json() if _is_json(resp) else {}
                    if not payload:
                        payload = {"origin": _origin_from_text(resp.text) or None}
                except httpx.HTTPError as exc:
                    result.error = f"judge:{type(exc).__name__}"
                    return result

                latency_ms = (time.perf_counter() - started) * 1000.0
                headers = _headers_of(payload)
                exit_ip = _origin_of(payload)
                result.latency_ms = latency_ms
                result.exit_ip = exit_ip
                result.anonymity = classify_anonymity(headers, exit_ip, direct_ip)
                result.ok = True
                result.platforms = await probes
                return result
            except Exception as exc:  # pragma: no cover - guard against library surprises
                result.error = f"unexpected:{type(exc).__name__}"
                return result
            finally:
                if not probes.done():
                    probes.cancel()
    except httpx.HTTPError as exc:
        result.error = f"transport:{type(exc).__name__}"
        return result
    except Exception as exc:  # pragma: no cover - guard against library surprises
        result.error = f"unexpected:{type(exc).__name__}"
        return result


async def iter_checks(
    proxies: list[str], direct_ip: str | None, concurrency: int, window_factor: int = 4
) -> AsyncIterator[CheckResult]:
    """Yield each verdict the moment it lands, in completion order.

    A cycle can span tens of thousands of candidates and many minutes. Yielding
    per proxy lets the caller store and serve results as they arrive instead of
    holding an empty list until the last proxy is checked. Outstanding tasks are
    bounded to ``concurrency * window_factor`` so a huge input cannot spawn a
    task per proxy, and one wide semaphore keeps the in-flight count at the
    configured ceiling across window boundaries.
    """
    total = len(proxies)
    if total == 0:
        return
    sem = asyncio.Semaphore(max(1, concurrency))
    window = max(1, concurrency * window_factor)

    async def one(proxy: str) -> CheckResult:
        async with sem:
            return await check_proxy(proxy, direct_ip)

    for start in range(0, total, window):
        chunk = proxies[start : start + window]
        tasks = [asyncio.create_task(one(p)) for p in chunk]
        try:
            for finished in asyncio.as_completed(tasks):
                yield await finished
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()


async def check_many(proxies: list[str], direct_ip: str | None, concurrency: int) -> list[CheckResult]:
    return [result async for result in iter_checks(proxies, direct_ip, concurrency)]
