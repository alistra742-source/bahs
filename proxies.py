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


class StoreMirror:
    """The durable copy of the proxy list, in the accounts database.

    A container filesystem is rebuilt from the image on every deploy, so
    ``data/proxies.json`` does not survive one and the next run answers "no
    proxies saved yet" -- which reads as a broken run rather than a lost file.
    The database does survive a deploy, so it is the real store and the file
    stays a local cache for a single-process start.

    Every method here is best-effort: if the database is unreachable the list
    keeps working from the file, because a missing mirror must never stop a run.
    """

    ROW = "default"

    @staticmethod
    def _database():
        import accounts  # late: the server imports both, neither imports the other
        return accounts.store

    def load(self) -> tuple[list[str], float]:
        try:
            with self._database().transaction() as db:
                row = db.one("SELECT payload,updated FROM proxy_store WHERE id=?", (self.ROW,))
        except Exception as exc:  # pragma: no cover - a database that has not been migrated
            log.warning("could not read the stored proxy list (%s)", exc)
            return [], 0.0
        if not row:
            return [], 0.0
        try:
            data = json.loads(row["payload"])
        except ValueError:
            log.warning("the stored proxy list is not valid JSON; ignoring it")
            return [], 0.0
        raw = data.get("proxies") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return [], 0.0
        updated = float(row["updated"] or 0.0)
        return [str(entry) for entry in raw], updated

    def save(self, proxies: list[str], updated: float) -> None:
        payload = json.dumps({"updated": updated, "proxies": proxies})
        try:
            with self._database().transaction() as db:
                db.lock("proxy-store")
                db.execute(
                    "INSERT INTO proxy_store(id,payload,updated) VALUES(?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated",
                    (self.ROW, payload, updated),
                )
        except Exception as exc:  # pragma: no cover
            log.warning("could not mirror the proxy list to the database (%s)", exc)


@dataclass
class _State:
    proxies: list[str] = field(default_factory=list)
    updated: float = 0.0


class ProxyList:
    """The stored list, its file, and the database copy that outlives a deploy.

    Mutations bump ``version``, which is what lets the sniper notice that the
    list changed and rebuild its pool instead of running the previous set.
    """

    def __init__(self, path: str = STORE_PATH, mirror: "StoreMirror | None" = None) -> None:
        self.path = path
        self.mirror = mirror
        self._state = _State()
        self.version = 0

    # --- persistence ------------------------------------------------------
    def _from_file(self) -> tuple[list[str], float]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            log.info("no proxy list at %s yet", self.path)
            return [], 0.0
        except (ValueError, OSError) as exc:
            log.warning("could not read %s (%s); starting empty", self.path, exc)
            return [], 0.0
        raw = data.get("proxies") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            log.warning("%s does not hold a proxy list; starting empty", self.path)
            return [], 0.0
        updated = float(data.get("updated") or 0.0) if isinstance(data, dict) else 0.0
        return [str(x) for x in raw], updated

    def load(self) -> int:
        """The newest list this process can see: the file, or the database.

        Whichever was written last wins. After a deploy the file is gone and the
        database still holds the list; on a fresh database the file is what
        there is. Neither is assumed to exist.
        """
        from_file, file_updated = self._from_file()
        stored, stored_updated = self.mirror.load() if self.mirror else ([], 0.0)
        if stored_updated > file_updated:
            raw, source = stored, "the database"
        else:
            raw, source = from_file, self.path
        kept = [p for p in dict.fromkeys(raw) if normalize(p) == p]
        self._state = _State(proxies=kept[:MAX_PROXIES], updated=max(file_updated, stored_updated))
        self.version += 1
        log.info("loaded %d stored proxies from %s", len(kept), source)
        return len(kept)

    def save(self) -> None:
        self._state.updated = time.time()
        payload = {"updated": self._state.updated, "proxies": self._state.proxies}
        # The database first: it is the copy that survives a redeploy, so it must
        # not be lost to a full or read-only volume. Guarded anyway -- the file is
        # still a usable store, and a mirror that fails must not cost the user the
        # list they just pasted.
        if self.mirror:
            try:
                self.mirror.save(self._state.proxies, self._state.updated)
            except Exception as exc:
                log.warning("proxy list mirror failed (%s); the file copy is current", exc)
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            # Write beside the target and rename, so a crash mid-write cannot
            # leave a half-written list where a run would read it.
            temp = f"{self.path}.tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(temp, self.path)
        except OSError as exc:  # a read-only volume is survivable, the database is the store
            log.warning("could not write %s (%s); the database copy is current", self.path, exc)

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
