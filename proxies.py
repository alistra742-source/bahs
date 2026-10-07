"""The proxy list: parse whatever the user pastes, normalise it, persist it.

There is no scraper and no validator here. A pasted list is exactly what a run
rotates over -- a proxy that does not work shows up as an error on the name it
was used for, and the pool retires it for the rest of the run. That is the
whole model: the list is the user's, and this module only has to read it
faithfully.

Accepted on one line, with ``#`` comments and blanks ignored::

    1.2.3.4:8080                       -> http://1.2.3.4:8080
    https://1.2.3.4:8080               -> https://1.2.3.4:8080
    socks5://1.2.3.4:1080              -> socks5://1.2.3.4:1080
    user:pass@1.2.3.4:8080             -> http://user:pass@1.2.3.4:8080
    socks5://user:pass@1.2.3.4:1080    -> socks5://user:pass@1.2.3.4:1080
    1.2.3.4:8080:user:pass              -> http://user:pass@1.2.3.4:8080
    [2001:db8::1]:8080                 -> http://[2001:db8::1]:8080
    proxy.example.com:3128             -> http://proxy.example.com:3128

A bare ``host:port`` is assumed to be ``http``, which is what the overwhelming
majority of public lists are. Credentials and IPv6 brackets are preserved in
the stored form, because that is the string httpx is handed.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from config import MAX_PROXIES, STORE_PATH

log = logging.getLogger("bahs.proxies")

SCHEMES = ("http", "https", "socks4", "socks5")

# hostnames: letters, digits, dots, hyphens; must not start or end with a dot
# or a hyphen. 253 characters is the DNS ceiling.
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?$")
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_IPV6_RE = re.compile(r"^[0-9A-Fa-f:]+$")

# Separators a list may use between entries when it is not one-per-line.
_SPLIT_RE = re.compile(r"[\s,;|]+")


def _strip_comment(line: str) -> str:
    """Drop a `#` and everything after it.

    This has to happen per line, before the split: splitting first turns
    ``# a comment`` into the tokens ``a`` and ``comment``, which then each read
    as a rejected proxy and make the report look like the user's list was full
    of junk. A proxy URL never contains ``#``, so cutting at the first one is
    safe.
    """
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


def _valid_host(host: str) -> bool:
    if not host:
        return False
    if _IPV4_RE.match(host):
        return all(0 <= int(part) <= 255 for part in host.split("."))
    if ":" in host:  # IPv6, brackets already stripped
        return bool(_IPV6_RE.match(host))
    return bool(_HOSTNAME_RE.match(host))


def _split_hostport(text: str) -> tuple[str, int] | None:
    """``host:port`` -> ``(host, port)``, handling bracketed IPv6."""
    text = text.strip().strip("/")
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or end + 1 >= len(text) or text[end + 1] != ":":
            return None
        host, port = text[1:end], text[end + 2 :]
    else:
        host, sep, port = text.rpartition(":")
        if not sep:
            return None
    host = host.strip()
    port = port.strip()
    if not port.isdigit():
        return None
    number = int(port)
    if not (1 <= number <= 65535) or not _valid_host(host):
        return None
    return host, number


def normalize(line: str) -> str | None:
    """One raw line to a canonical proxy URL, or None if it is not one.

    Never raises and never guesses past the formats above: a line this cannot
    read is counted as rejected and shown to the user, not silently stored as
    something httpx will refuse at run time.
    """
    text = (line or "").strip().lstrip("\ufeff")
    if not text or text.startswith("#") or text.startswith("//"):
        return None
    text = text.strip("\"'` \t,;")

    scheme = "http"
    explicit_scheme = False
    if "://" in text:
        head, text = text.split("://", 1)
        scheme = head.strip().lower()
        explicit_scheme = True
        if scheme not in SCHEMES:
            return None
        text = text.strip()

    userinfo = ""
    if "@" in text:
        userinfo, text = text.rsplit("@", 1)
        userinfo = userinfo.strip()
    elif not explicit_scheme and text.count(":") == 3:
        # `host:port:user:pass`, the shape a lot of pasted lists use.
        host_part, port_part, user, password = (p.strip() for p in text.split(":"))
        text = f"{host_part}:{port_part}"
        userinfo = f"{user}:{password}" if user else ""

    parsed = _split_hostport(text)
    if parsed is None:
        return None
    host, port = parsed
    if ":" in host:  # re-bracket IPv6 for the URL
        host = f"[{host}]"
    if userinfo:
        if ":" not in userinfo:
            userinfo = f"{userinfo}:"  # httpx wants user:password
        return f"{scheme}://{userinfo}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


@dataclass
class ParseReport:
    """What a paste turned into, so the UI can say what it did with it."""

    accepted: list[str] = field(default_factory=list)
    duplicates: int = 0
    # A sample of the unreadable tokens, and how many there were in total: a
    # space-separated paste can reject more tokens than the 25 shown.
    rejected: list[str] = field(default_factory=list)
    rejected_total: int = 0

    @property
    def count(self) -> int:
        return len(self.accepted)


def parse_many(text: str) -> ParseReport:
    """Parse a pasted blob into unique, canonical proxies."""
    report = ParseReport()
    seen: set[str] = set()
    rejected = 0
    for line in (text or "").splitlines():
        for raw in _SPLIT_RE.split(_strip_comment(line)):
            if not raw:
                continue
            proxy = normalize(raw)
            if proxy is None:
                rejected += 1
                if len(report.rejected) < 25:
                    report.rejected.append(raw[:80])
                continue
            if proxy in seen:
                report.duplicates += 1
                continue
            seen.add(proxy)
            report.accepted.append(proxy)
    report.rejected_total = rejected
    return report


@dataclass
class _State:
    proxies: list[str] = field(default_factory=list)
    updated: float = 0.0


class ProxyList:
    """The stored list, and the file it lives in.

    Mutations bump ``version``, which is what lets the sniper notice that the
    list changed and rebuild its pool instead of running the previous set.
    """

    def __init__(self, path: str = STORE_PATH) -> None:
        self.path = path
        self._state = _State()
        self.version = 0

    # --- persistence ------------------------------------------------------
    def load(self) -> int:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            log.info("no proxy list at %s yet", self.path)
            return 0
        except (ValueError, OSError) as exc:
            log.warning("could not read %s (%s); starting empty", self.path, exc)
            return 0
        raw = data.get("proxies") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            log.warning("%s does not hold a proxy list; starting empty", self.path)
            return 0
        kept = [p for p in dict.fromkeys(str(x) for x in raw) if normalize(p) == p]
        self._state = _State(
            proxies=kept[:MAX_PROXIES],
            updated=float(data.get("updated") or 0.0) if isinstance(data, dict) else 0.0,
        )
        self.version += 1
        log.info("loaded %d stored proxies from %s", len(kept), self.path)
        return len(kept)

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._state.updated = time.time()
        payload = {"updated": self._state.updated, "proxies": self._state.proxies}
        # Write beside the target and rename, so a crash mid-write cannot leave
        # a half-written list where a run would read it.
        temp = f"{self.path}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temp, self.path)

    # --- contents ---------------------------------------------------------
    def all(self) -> list[str]:
        return list(self._state.proxies)

    def __len__(self) -> int:
        return len(self._state.proxies)

    @property
    def updated(self) -> float:
        return self._state.updated

    def stats(self) -> dict:
        by_scheme: dict[str, int] = {}
        with_auth = 0
        for proxy in self._state.proxies:
            scheme = proxy.split("://", 1)[0]
            by_scheme[scheme] = by_scheme.get(scheme, 0) + 1
            if "@" in proxy:
                with_auth += 1
        return {
            "count": len(self._state.proxies),
            "updated": self._state.updated or None,
            "by_scheme": by_scheme,
            "with_auth": with_auth,
            "version": self.version,
        }

    # --- mutation ---------------------------------------------------------
    def replace(self, proxies: list[str]) -> int:
        """Swap the whole list. Returns how many entries were stored."""
        deduped = list(dict.fromkeys(proxies))[:MAX_PROXIES]
        self._state.proxies = deduped
        self.version += 1
        return len(deduped)

    def add(self, proxies: list[str]) -> tuple[int, int]:
        """Append to the list. Returns ``(added, skipped_as_duplicate)``."""
        known = set(self._state.proxies)
        added: list[str] = []
        duplicates = 0
        for proxy in proxies:
            if proxy in known:
                duplicates += 1
                continue
            known.add(proxy)
            added.append(proxy)
        room = MAX_PROXIES - len(self._state.proxies)
        if room <= 0:
            return 0, duplicates
        added = added[:room]
        self._state.proxies.extend(added)
        if added:
            self.version += 1
        return len(added), duplicates

    def remove(self, proxy: str) -> bool:
        try:
            self._state.proxies.remove(proxy)
        except ValueError:
            return False
        self.version += 1
        return True

    def clear(self) -> int:
        removed = len(self._state.proxies)
        self._state.proxies = []
        self.version += 1
        return removed
