"""Proxy-list sources and the parser that turns their raw text into candidates.

Sources are public raw lists. Each carries the protocol it publishes, because
the same endpoint address behaves differently as HTTP vs SOCKS5 and the two
must never be merged.
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from config import SOURCE_TIMEOUT

log = logging.getLogger("proxy-scraper.sources")


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    protocol: str  # "http", "https", "socks4" or "socks5"


# Ordered by historical yield. Add or drop by editing this tuple; nothing else
# reads a hard-coded URL.
SOURCES: tuple[Source, ...] = (
    Source("speedx-http", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "http"),
    Source("speedx-socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
    Source("speedx-socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "socks4"),
    Source("monosans-http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "http"),
    Source("monosans-socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    Source("monosans-socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt", "socks4"),
    Source("clarketm-http", "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt", "http"),
    Source("shifty-http", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt", "http"),
    Source("shifty-socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt", "socks5"),
    Source("hookzof-socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt", "socks5"),
    Source("roosterkid-http", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt", "https"),
    Source("roosterkid-socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt", "socks5"),
    Source("mmpx12-http", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt", "http"),
    Source("mmpx12-socks5", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt", "socks5"),
    Source("proxyscrape-http", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all", "http"),
    Source("proxyscrape-socks5", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all", "socks5"),
    Source("proxylist-download-http", "https://www.proxy-list.download/api/v1/get?type=http", "http"),
    Source("proxylist-download-socks5", "https://www.proxy-list.download/api/v1/get?type=socks5", "socks5"),
    Source("prxchk-http", "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt", "http"),
    Source("prxchk-socks5", "https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt", "socks5"),
)

# host:port, optionally prefixed with a scheme the source labels per-line.
_ADDR_RE = re.compile(
    r"(?:(?P<scheme>https?|socks4|socks5)://)?"
    r"(?P<host>(?:\d{1,3}\.){3}\d{1,3}):(?P<port>\d{2,5})",
    re.IGNORECASE,
)

# A response longer than this from a source is treated as garbage, not a list.
_MAX_SOURCE_BYTES = 8 * 1024 * 1024


def _valid_octets(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4:
        return False
    return all(0 <= int(p) <= 255 for p in parts)


def parse_source_text(text: str, default_protocol: str) -> set[str]:
    """Return a set of ``protocol://host:port`` strings from raw source text.

    Handles plain ``ip:port`` lines, scheme-prefixed lines, and lines with
    trailing metadata (``ip:port:country``) because sources disagree on format.
    """
    found: set[str] = set()
    for match in _ADDR_RE.finditer(text):
        scheme = (match.group("scheme") or default_protocol).lower()
        host = match.group("host")
        port = int(match.group("port"))
        if not (1 <= port <= 65535):
            continue
        if not _valid_octets(host):
            continue
        found.add(f"{scheme}://{host}:{port}")
    return found


async def _fetch_one(client: httpx.AsyncClient, source: Source) -> set[str]:
    try:
        resp = await client.get(source.url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("source %s failed: %s", source.name, exc)
        return set()
    if len(resp.content) > _MAX_SOURCE_BYTES:
        log.warning("source %s exceeded size cap, ignored", source.name)
        return set()
    parsed = parse_source_text(resp.text, source.protocol)
    log.info("source %s -> %d proxies", source.name, len(parsed))
    return parsed


async def fetch_all(sources: tuple[Source, ...] = SOURCES) -> set[str]:
    """Fetch every source concurrently and return the de-duplicated union."""
    timeout = httpx.Timeout(SOURCE_TIMEOUT, connect=10.0)
    headers = {"User-Agent": "Mozilla/5.0 (proxy-scraper)"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        results = await asyncio.gather(*(_fetch_one(client, s) for s in sources))
    merged: set[str] = set()
    for chunk in results:
        merged |= chunk
    log.info("scrape complete: %d unique candidates from %d sources", len(merged), len(sources))
    return merged
