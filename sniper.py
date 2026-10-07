"""Username availability checks for Discord, guns.lol, Instagram and TikTok.

Every check goes out through a proxy from the stored list, because all four
targets rate-limit or block by IP. Four things make a batch of names fast:

* **A warm client per proxy.** The proxy CONNECT tunnel and the TLS handshake to
  the target are paid once and then reused across names (httpx keeps a per-host
  keep-alive pool). Creating a client per name -- and so re-tunnelling and
  re-handshaking every time -- is the single biggest cost in a batch.
* **Enough connections per proxy to use the concurrency that was asked for.**
  Measured with one proxy endpoint: 32 concurrent guns.lol checks ran at 3.9/s
  with one connection, 13.0/s with six, and 53.1/s with thirty-two. The
  connection cap, not the check, was the whole ceiling, so the pool sizes it from
  ``concurrency / len(pool)`` rather than from a fixed number.
* **A pool that tells "the platform refused" apart from "this host is dead".**
  A transport failure is the proxy's fault and counts against it; a 429 is the
  platform's and only rests it. Conflating them is what let a blanket rate limit
  retire a healthy list and report ``proxies exhausted``.
* **Only the bytes that answer the question.** A profile page is tens of KB of
  framework payload; the marker the check turns on is inside the first few KB.
  Note that this saves decompression and parsing, not transfer -- the server
  sends the whole body either way -- so it is a cheap saving, not a large one.

The four endpoints, and what each answer means, were read off the live sites
rather than assumed:

* **Discord** -- the unauthenticated signup form's own endpoint,
  ``POST /api/v9/unique-username/username-attempt-unauthed``. It answers
  ``{"taken": true|false}``. A 400 carries a *validation* error (a reserved
  word, bad length) and must never be read as "available"; a 429 carries a
  ``retry_after`` in seconds, which is surfaced rather than swallowed.
* **guns.lol** -- ``GET /{name}`` always returns 200 (it is a Next.js app), so
  the status is useless. A live profile embeds a Schema.org block
  (``profile-page-json-ld``) at 8-9 KB; a free name renders the site's default
  page without it. Measured: taken 34 KB with the marker, free 21 KB without.
* **Instagram** -- ``GET /api/v1/users/web_profile_info/?username=`` with the
  web app's own ``x-ig-app-id``. 200 with a user object = taken, 404 =
  available, and 429/401/302 = blocked, which is reported as such rather than
  guessed at. A datacenter IP is refused outright, which is exactly what the
  proxy list is for.
* **TikTok** -- ``GET /oembed?url=.../@name``. The full profile page is useless
  for this: from a datacenter IP TikTok answers 200 with a generic shell whose
  body is the same whether or not the account exists. oEmbed does distinguish
  (measured): a live handle returns 200 with an ``author_name``, a free one
  returns 400 with ``{"message":"Something went wrong","code":400}`` -- 45
  bytes, so it is also by far the cheapest of the four.
"""

import asyncio
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass

import httpx

from config import (
    CONNECT_TIMEOUT,
    POOL_EMPTY_STREAK,
    POOL_WAIT_MAX,
    READ_TIMEOUT,
    SNIPE_BLOCK_COOLDOWN,
    SNIPE_MAX_CLIENTS,
    GITHUB_TOKEN,
    SNIPE_PER_PROXY,
    SNIPE_PLATFORM_PAUSE,
    SNIPE_PLATFORM_PAUSE_MAX,
    SNIPE_PROXY_BLOCK_LIMIT,
    SNIPE_PROXY_COOLDOWN,
    SNIPE_PROXY_FAIL_LIMIT,
    SSL_CONTEXT,
)

log = logging.getLogger("bahs.sniper")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
IG_APP_ID = "936619743392459"
DISCORD_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) discord/1.0"

DISCORD_ATTEMPT = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"
TIKTOK_OEMBED = "https://www.tiktok.com/oembed"
GUNS_DEFAULT_TITLE = "guns.lol: Everything you want, right here."
GUNS_TITLE_RE = re.compile(r"^\s*@[^|]{1,64}\|\s*guns\.lol\s*$", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# A live profile embeds its own Schema.org block; a free name renders the site's
# default page without it.
GUNS_PROFILE_MARKER = b"profile-page-json-ld"
GUNS_IDENTIFIER_RE = re.compile(r'"identifier"\s*:\s*"([^"]{1,64})"')

# How much of a profile page to look at. Both guns.lol signals land well inside
# this while the page itself runs 21-40 KB.
MAX_HEAD_BYTES = 16 * 1024

# The union of every platform's charset and length, used only as a cheap "is
# this even a name" gate before the per-platform rule below. It is deliberately
# wider than any single platform: GitHub allows a hyphen and Roblox does not, so
# a gate at the intersection would silently drop names that are valid somewhere.
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,39}$")

# Per-platform name rules, applied *before* a request is spent.
#
# This is not a nicety. For most of these hosts an unregisterable name and a free
# name are indistinguishable over the wire: GitHub answers 404 for "bad!name"
# exactly as it does for an unclaimed handle, and Mojang does the same for
# "bad-name". Without the local rule the run reports names that nobody can ever
# register as "available" -- and it spends a proxy request per name to do it.
NAMESPACES: dict[str, tuple[re.Pattern[str], str]] = {
    "discord": (re.compile(r"^[A-Za-z0-9._]{2,32}$"), "2-32 of a-z A-Z 0-9 . _"),
    "roblox": (re.compile(r"^[A-Za-z0-9_]{3,20}$"), "3-20 of a-z A-Z 0-9 _"),
    "minecraft": (re.compile(r"^[A-Za-z0-9_]{3,16}$"), "3-16 of a-z A-Z 0-9 _"),
    "telegram": (re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$"), "5-32, must start with a letter"),
    "instagram": (re.compile(r"^[A-Za-z0-9._]{1,30}$"), "1-30 of a-z A-Z 0-9 . _"),
    "tiktok": (re.compile(r"^[A-Za-z0-9._]{2,24}$"), "2-24 of a-z A-Z 0-9 . _"),
    "x": (re.compile(r"^[A-Za-z0-9_]{1,15}$"), "1-15 of a-z A-Z 0-9 _"),
    "github": (
        re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$"),
        "1-39, no leading or trailing -",
    ),
    "youtube": (re.compile(r"^[A-Za-z0-9._-]{3,30}$"), "3-30 of a-z A-Z 0-9 . _ -"),
    "guns.lol": (re.compile(r"^[A-Za-z0-9._-]{3,32}$"), "3-32 of a-z A-Z 0-9 . _ -"),
}


def name_fits(platform: str, username: str) -> bool:
    """Can this name exist on this platform at all?"""
    rule = NAMESPACES.get(platform)
    return True if rule is None else rule[0].match(username) is not None


def namespace_note(platform: str) -> str:
    rule = NAMESPACES.get(platform)
    return "bad characters or length" if rule is None else f"not valid on {platform}: {rule[1]}"

PLATFORMS = (
    "discord",
    "roblox",
    "minecraft",
    "telegram",
    "instagram",
    "tiktok",
    "x",
    "github",
    "youtube",
    "guns.lol",
)

# The statuses that are an answer: a verdict about the name.
FINAL_STATUSES = ("available", "taken", "invalid")

# How a result reflects on the proxy that carried it.
OUTCOME_OK = "ok"            # the check produced a verdict
OUTCOME_BLOCKED = "blocked"  # the platform refused: not the proxy's fault
OUTCOME_FAILED = "failed"    # transport, timeout: the proxy's fault

# The details check_once reports when the pool had nothing to hand out. Kept as a
# constant so a run can count them without matching on prose, and so the
# dashboard can show "proxy misses" as its own number instead of letting them
# disappear into the error bucket.
POOL_MISS_DETAILS = ("proxies exhausted this run", "no proxies saved")


def outcome_for(status: str) -> str:
    if status in FINAL_STATUSES:
        return OUTCOME_OK
    return OUTCOME_BLOCKED if status == "blocked" else OUTCOME_FAILED


def default_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT,
        write=READ_TIMEOUT,
        pool=READ_TIMEOUT,
    )


def per_proxy_connections(pool_size: int, concurrency: int, floor: int, ceiling: int) -> int:
    """Connections one proxy needs to carry its share of ``concurrency``.

    A fixed per-proxy cap is a throughput ceiling that is invisible until you
    have very few proxies: with one rotating endpoint and a cap of six, asking
    for 256 concurrent checks still only gets six at a time. Dividing the
    requested concurrency across the pool removes that ceiling for small lists
    while keeping a large list from opening a socket storm.
    """
    if pool_size <= 0:
        return max(1, floor)
    share = -(-max(1, concurrency) // pool_size)  # ceil division
    return max(1, min(ceiling, max(floor, share)))


def retry_after_seconds(body: object) -> float | None:
    """Pull ``retry_after`` out of a rate-limit body, when the platform sends it."""
    if not isinstance(body, dict):
        return None
    for key in ("retry_after", "retry_after_ms", "retryAfter"):
        value = body.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = float(value)
            if key == "retry_after_ms":
                seconds /= 1000.0
            if 0 < seconds < 86400:
                return seconds
    return None


@dataclass
class SnipeResult:
    username: str
    platform: str
    status: str  # available | taken | invalid | blocked | error
    detail: str = ""
    proxy: str | None = None
    latency_ms: float | None = None
    retry_after: float | None = None

    @property
    def is_available(self) -> bool:
        return self.status == "available"

    @property
    def answered(self) -> bool:
        """Did the check actually answer the question about this name?"""
        return self.status in FINAL_STATUSES


class ProxyPool:
    """The proxies a run rotates over, plus their kept-alive clients.

    ``len(pool) == 0`` means the stored list is empty; callers report that as
    "no proxies saved yet" rather than silently going direct. There is no lock
    on ``next``/``client``: they never await, so on the single event loop they
    cannot interleave.
    """

    def __init__(
        self,
        proxies: Iterable[str],
        cooldown: float = SNIPE_PROXY_COOLDOWN,
        block_cooldown: float = SNIPE_BLOCK_COOLDOWN,
        per_proxy: int = SNIPE_PER_PROXY,
        timeout: httpx.Timeout | None = None,
        fail_limit: int = SNIPE_PROXY_FAIL_LIMIT,
        block_limit: int = SNIPE_PROXY_BLOCK_LIMIT,
        max_clients: int = SNIPE_MAX_CLIENTS,
    ) -> None:
        self._all = list(dict.fromkeys(proxies))
        self._cooldown = max(0.0, cooldown)
        self._block_cooldown = max(0.0, block_cooldown)
        self._per_proxy = max(1, per_proxy)
        self._timeout = timeout or default_timeout()
        self._max_clients = max(1, max_clients)
        self._clients: OrderedDict[str, httpx.AsyncClient] = OrderedDict()
        self._resting: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._blocks: dict[str, int] = {}
        # A proxy retired mid-run is never handed out again.
        self._retired: set[str] = set()
        self._fail_limit = max(1, fail_limit)
        self._block_limit = max(1, block_limit)
        self._cursor = 0
        # platform -> the proxies it has refused, so "the platform blocked
        # everything" can be asked about a specific platform. A proxy blocked by
        # discord is still a perfectly good proxy for tiktok, and treating those
        # as the same thing stops a healthy run.
        self._blocked_by: dict[str, set[str]] = {}
        # platform -> monotonic deadline, for a platform that refused the whole
        # list. Sitting one out is what keeps a discord 429 from costing the rest
        # of a multi-platform run.
        self._paused: dict[str, float] = {}
        self.paused_reason: dict[str, str] = {}
        # Jobs dropped because their platform was sitting out. Counted so the
        # summary can say how much of the run a throttle actually cost.
        self.skipped = 0
        # Why a run ended before its last name, when it did, and the longest
        # retry_after any platform handed us.
        self.stop_reason = ""
        self.retry_after: float | None = None

    def __len__(self) -> int:
        return len(self._all)

    @property
    def per_proxy(self) -> int:
        return self._per_proxy

    def next(self) -> str | None:
        """The next proxy to use, walking the rotation from a cursor.

        The cursor skips resting and retired entries in place. Rebuilding a
        filtered copy of the pool on every call is O(n) per check -- tens of
        millions of operations over a wide run -- so this walks instead, which
        is O(resting) in the common case and only reaches O(n) when nearly
        everything is resting, which is itself the signal that the pool is dead.
        """
        now = time.time()
        n = len(self._all)
        if not n:
            return None
        start = self._cursor % n
        for offset in range(n):
            idx = start + offset
            if idx >= n:
                idx -= n
            proxy = self._all[idx]
            if proxy in self._retired:
                continue
            if self._resting.get(proxy, 0.0) > now:
                continue
            self._cursor = idx + 1
            return proxy
        usable = [p for p in self._all if p not in self._retired]
        if not usable:
            return None
        # Every live proxy is resting: use whichever comes back first.
        return min(usable, key=lambda p: self._resting.get(p, 0.0))

    def client(self, proxy: str) -> httpx.AsyncClient:
        """The kept-alive client for one proxy, created on first use.

        One client per proxy carries every platform, so the tunnel and the TLS
        session to each target are established once per run, not once per name.
        Past ``max_clients`` the least recently used client is dropped: a list
        with 100k entries must not become 100k open pools.
        """
        client = self._clients.get(proxy)
        if client is not None:
            self._clients.move_to_end(proxy)
            return client
        if len(self._clients) >= self._max_clients:
            _evicted, stale = self._clients.popitem(last=False)
            self._close_later(stale)
        client = httpx.AsyncClient(
            proxy=proxy,
            timeout=self._timeout,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=self._per_proxy,
                max_keepalive_connections=self._per_proxy,
            ),
            # Shared context: a pool is one client per proxy, and a per-client
            # SSLContext would cost ~1.25 MB x pool size.
            verify=SSL_CONTEXT,
        )
        self._clients[proxy] = client
        return client

    @staticmethod
    def _close_later(client: httpx.AsyncClient) -> None:
        """Close a client without awaiting -- callers of ``client()`` are sync."""
        try:
            asyncio.get_running_loop().create_task(client.aclose())
        except RuntimeError:  # pragma: no cover - no loop, nothing to close onto
            pass

    def _retire(self, proxy: str) -> None:
        self._retired.add(proxy)
        client = self._clients.pop(proxy, None)
        if client is not None:
            # Free the sockets immediately; a retired proxy's warm pool is dead
            # weight against the process fd budget.
            self._close_later(client)

    def report(
        self,
        proxy: str,
        outcome: str,
        platform: str = "",
        retry_after: float | None = None,
    ) -> None:
        """Remember how a proxy behaved, so retries avoid a bad one.

        The three outcomes are deliberately not the same:

        * ``ok``      -- clear the proxy's record entirely.
        * ``blocked`` -- the platform refused a request from this exit IP. It is
          rested briefly and counted separately, and only retired after many
          repeats. Retiring it after a handful, which is what this used to do,
          means a blanket rate limit walks the whole list into the bin and the
          run then reports "proxies exhausted" -- blaming the list for the
          platform's throttle.
        * ``failed``  -- a transport error is the proxy's own problem and counts
          toward retirement quickly.
        """
        if retry_after and (self.retry_after is None or retry_after > self.retry_after):
            self.retry_after = retry_after

        if outcome == OUTCOME_OK:
            self._resting.pop(proxy, None)
            self._fails.pop(proxy, None)
            self._blocks.pop(proxy, None)
            # Only the platform that just answered stops refusing it. Wiping the
            # record for *every* platform here meant a healthy second platform
            # erased the evidence of a blanket 429: discord blocks both proxies,
            # tiktok answers on both, and the run never notices discord is dead.
            refused = self._blocked_by.get(platform or "")
            if refused is not None:
                refused.discard(proxy)
            return

        if outcome == OUTCOME_BLOCKED:
            refused = self._blocked_by.setdefault(platform or "", set())
            refused.add(proxy)
            blocks = self._blocks.get(proxy, 0) + 1
            self._blocks[proxy] = blocks
            if blocks >= self._block_limit:
                self._retire(proxy)
            elif self._block_cooldown > 0:
                self._resting[proxy] = time.time() + self._block_cooldown
            return

        fails = self._fails.get(proxy, 0) + 1
        self._fails[proxy] = fails
        if fails >= self._fail_limit:
            self._retire(proxy)
        elif self._cooldown > 0:
            self._resting[proxy] = time.time() + self._cooldown

    def alive(self) -> int:
        """Proxies not written off for the rest of the run."""
        return sum(1 for p in self._all if p not in self._retired)

    def resting(self) -> int:
        now = time.time()
        return sum(1 for until in self._resting.values() if until > now)

    def retired(self) -> int:
        return len(self._retired)

    def blocked(self) -> int:
        """Proxies some platform has refused at least once."""
        return len(self._blocks)

    def blocked_by(self) -> dict[str, int]:
        """How many proxies each platform has refused."""
        return {name: len(refused) for name, refused in self._blocked_by.items() if refused}

    def all_blocked(self, platforms: Iterable[str]) -> bool:
        """Has every proxy been refused by *every* platform the run is using?

        This is the crisp signal for a blanket rate limit, and it fires after one
        request per proxy per platform instead of after some number of wasted
        seconds. Waiting for a rest to expire cannot detect it: with one proxy,
        each 5-second rest expires, the next check is refused again, and the run
        grinds one blocked name at a time forever.

        It has to be per platform. Asking "has anything refused these proxies"
        makes a discord 429 look like a dead list, and a three-platform run then
        stops before tiktok -- which was answering fine -- is ever asked.
        """
        if not self._all:
            return False
        wanted = list(platforms) or list(self._blocked_by)
        if not wanted:
            return False
        return all(
            all(proxy in self._blocked_by.get(one, ()) for proxy in self._all)
            for one in wanted
        )

    def pause_seconds(self) -> float:
        """How long to sit a platform out after it refused the whole list."""
        if self.retry_after:
            return min(SNIPE_PLATFORM_PAUSE_MAX, max(SNIPE_PLATFORM_PAUSE, self.retry_after))
        return SNIPE_PLATFORM_PAUSE

    def pause(self, platform: str, reason: str, seconds: float) -> None:
        self._paused[platform] = time.monotonic() + max(1.0, seconds)
        self.paused_reason[platform] = reason

    def is_paused(self, platform: str) -> bool:
        """Is this platform sitting out? Expires the pause on read."""
        until = self._paused.get(platform)
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        # The pause is up: clear it *and* this platform's block history, so it
        # gets a fresh set of attempts instead of being paused again by the
        # strikes it collected before.
        self._paused.pop(platform, None)
        self.paused_reason.pop(platform, None)
        self._blocked_by.pop(platform, None)
        return False

    def pause_blocked(self, platforms: Iterable[str]) -> list[str]:
        """Sit out any platform that has now refused every proxy in the list.

        Returns the platforms newly paused, so the run can report it instead of
        leaving the user to infer it from a column of `blocked` rows.
        """
        newly: list[str] = []
        for platform in platforms:
            if self.is_paused(platform):
                continue
            if self.all_blocked([platform]):
                refused = len(self._blocked_by.get(platform, ()))
                self.pause(
                    platform,
                    f"refused all {refused} proxies"
                    + (f", asked for {self.retry_after:.0f}s" if self.retry_after else ""),
                    self.pause_seconds(),
                )
                newly.append(platform)
                log.info("pausing %s for %.0fs: %s", platform, self.pause_seconds(), self.paused_reason[platform])
        return newly

    def paused(self) -> dict[str, dict[str, object]]:
        """The platforms sitting out, with how long each has left."""
        out: dict[str, dict[str, object]] = {}
        for platform in list(self._paused):
            if self.is_paused(platform):
                out[platform] = {
                    "seconds_left": round(max(0.0, self._paused[platform] - time.monotonic()), 1),
                    "reason": self.paused_reason.get(platform, ""),
                }
        return out

    def usable(self) -> int:
        """Proxies that can be handed out right now."""
        now = time.time()
        return sum(
            1 for p in self._all if p not in self._retired and self._resting.get(p, 0.0) <= now
        )

    async def wait_usable(self, timeout: float, stop: asyncio.Event | None = None) -> bool:
        """Wait for a resting proxy to come back, up to ``timeout``.

        Returns False when the wait ran out or every proxy is retired, which is
        the caller's signal to stop the run and say why rather than emit a
        screenful of errors.
        """
        deadline = time.time() + max(0.0, timeout)
        while True:
            if self.usable():
                return True
            if self.alive() == 0:
                return False
            if stop is not None and stop.is_set():
                return False
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.25, remaining))

    def exhausted_reason(self) -> str:
        """Why a run stopped with nothing left to dispatch through."""
        total = len(self._all)
        if total == 0:
            return "no proxies are saved, so there was nothing to check through"
        if self.retired() >= total:
            if self.blocked():
                return (
                    f"every one of the {total} proxies was rate-limited {self._block_limit} "
                    "times and retired for this run -- the list is being throttled, not "
                    "merely exhausted; check the proxies and try again later"
                )
            return (
                f"every one of the {total} proxies in the list failed with a transport error "
                "and was retired for this run -- the list is dead, replace it"
            )
        return f"no proxy from the list of {total} could be used"

    def blocked_reason(self) -> str:
        """Why a run stopped with proxies alive but none usable."""
        counts = self.blocked_by()
        platform = max(counts, key=lambda k: counts[k]) if counts else "the platform"
        extra = f" (it asked for {self.retry_after:.0f}s)" if self.retry_after else ""
        return (
            f"{platform} refused every proxy in the list, so the run was stopped after "
            f"{POOL_EMPTY_STREAK} waits. That is a platform-wide throttle, not a dead list -- "
            f"proxies were only rested {int(self._block_cooldown)}s each{extra}. A bigger pool, "
            "fewer simultaneous checks, or a different exit range is what fixes it."
        )

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), OrderedDict()
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - closing is best effort
                log.debug("closing a sniper client failed", exc_info=True)


# --- Discord --------------------------------------------------------------
async def check_discord(client: httpx.AsyncClient, username: str) -> SnipeResult:
    # A proxy whose SOCKS handshake fails raises the proxy library's own
    # ProtocolError, which is *not* an httpx.HTTPError; catching only httpx
    # errors lets it escape as "unexpected:ProtocolError" and hides the proxy as
    # the cause. Catch broadly and report the exception's own name.
    try:
        resp = await client.post(
            DISCORD_ATTEMPT,
            json={"username": username},
            headers={"Content-Type": "application/json", "User-Agent": DISCORD_UA},
        )
    except Exception as exc:
        return SnipeResult(username, "discord", "error", type(exc).__name__)

    if resp.status_code == 200:
        try:
            taken = bool(resp.json().get("taken"))
        except ValueError:
            return SnipeResult(username, "discord", "error", "unreadable body")
        return SnipeResult(username, "discord", "taken" if taken else "available")
    if resp.status_code == 429:
        wait = retry_after_seconds(_json_or_none(resp))
        detail = "rate limited" + (f", retry_after {wait:.0f}s" if wait else "")
        return SnipeResult(username, "discord", "blocked", detail, retry_after=wait)
    if resp.status_code == 400:
        # A validation refusal, not an availability answer.
        return SnipeResult(username, "discord", "invalid", _discord_reason(resp))
    return SnipeResult(username, "discord", "error", f"status {resp.status_code}")


def _json_or_none(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return None


def _discord_reason(resp: httpx.Response) -> str:
    body = _json_or_none(resp)
    if isinstance(body, dict):
        errors = (body.get("errors") or {}).get("username", {}).get("_errors") or []
        if errors:
            return str(errors[0].get("code") or "invalid")
    return "invalid"


# --- guns.lol -------------------------------------------------------------
def _guns_verdict(username: str, body: bytes) -> SnipeResult | None:
    """Read a verdict off the part of the page we look at, or None for "unknown"."""
    if GUNS_PROFILE_MARKER in body:
        found = GUNS_IDENTIFIER_RE.search(body.decode("utf-8", errors="ignore"))
        return SnipeResult(
            username, "guns.lol", "taken", f"@{found.group(1)}" if found else "live profile"
        )
    if b"</title>" not in body:
        return None
    match = TITLE_RE.search(body.decode("utf-8", errors="ignore"))
    if not match:
        return None
    title = match.group(1).strip()
    if title == GUNS_DEFAULT_TITLE:
        return SnipeResult(username, "guns.lol", "available", "default page")
    if GUNS_TITLE_RE.match(title):
        return SnipeResult(username, "guns.lol", "taken", title)
    # A title we do not recognise: no verdict from this page.
    return None


async def check_guns(client: httpx.AsyncClient, username: str) -> SnipeResult:
    """One request, read in full, then inspected.

    This used to stream the page and abandon the response the moment the marker
    showed up, with a background task draining the rest to keep the socket warm.
    Measured against an origin serving the same 34 KB page, that was never
    faster than simply reading it: the server sends the whole body either way,
    so aborting saves decompression but not transfer -- and in between, the
    drain task held a connection out of the proxy's pool for the length of the
    page, which is the wrong trade when the pool is only a few connections wide.
    """
    try:
        resp = await client.get(f"https://guns.lol/{username}", headers={"User-Agent": BROWSER_UA})
    except Exception as exc:
        return SnipeResult(username, "guns.lol", "error", type(exc).__name__)

    if resp.status_code == 429:
        wait = retry_after_seconds(_json_or_none(resp))
        detail = "rate limited" + (f", retry_after {wait:.0f}s" if wait else "")
        return SnipeResult(username, "guns.lol", "blocked", detail, retry_after=wait)
    if resp.status_code == 404:
        return SnipeResult(username, "guns.lol", "available", "404")
    if resp.status_code != 200:
        return SnipeResult(username, "guns.lol", "error", f"status {resp.status_code}")

    body = resp.content[:MAX_HEAD_BYTES]
    verdict = _guns_verdict(username, body)
    if verdict is not None:
        return verdict
    # Neither a profile block nor a title we know: say so instead of guessing.
    match = TITLE_RE.search(body.decode("utf-8", errors="ignore"))
    if match:
        return SnipeResult(
            username, "guns.lol", "error", f"unexpected title: {match.group(1).strip()[:60]}"
        )
    return SnipeResult(username, "guns.lol", "error", "no title in the first 16 KB")


# --- Instagram ------------------------------------------------------------
async def check_instagram(client: httpx.AsyncClient, username: str) -> SnipeResult:
    headers = {
        "User-Agent": BROWSER_UA,
        "x-ig-app-id": IG_APP_ID,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    try:
        resp = await client.get(url, headers=headers)
    except Exception as exc:
        return SnipeResult(username, "instagram", "error", type(exc).__name__)

    if resp.status_code == 404:
        return SnipeResult(username, "instagram", "available", "404")
    if resp.status_code == 429:
        wait = retry_after_seconds(_json_or_none(resp))
        detail = "rate limited" + (f", retry_after {wait:.0f}s" if wait else "")
        return SnipeResult(username, "instagram", "blocked", detail, retry_after=wait)
    if resp.status_code in (401, 302):
        return SnipeResult(username, "instagram", "blocked", f"status {resp.status_code}")
    if resp.status_code != 200:
        return SnipeResult(username, "instagram", "error", f"status {resp.status_code}")

    body = _json_or_none(resp)
    if not isinstance(body, dict):
        return SnipeResult(username, "instagram", "error", "unreadable body")
    user = (body.get("data") or {}).get("user")
    if user is None:
        return SnipeResult(username, "instagram", "available", "no user object")
    return SnipeResult(username, "instagram", "taken", str(user.get("username") or ""))


# --- TikTok ---------------------------------------------------------------
async def check_tiktok(client: httpx.AsyncClient, username: str) -> SnipeResult:
    """oEmbed answers 200 for a live handle and 400 for a free one.

    Anything else -- a 403, a 429, an HTML body, an empty one -- is reported as
    blocked or an error rather than as an answer, because the whole point of the
    proxy list is that a datacenter IP gets a shell page whose body does not
    distinguish the two cases.
    """
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*"}
    try:
        resp = await client.get(
            TIKTOK_OEMBED,
            params={"url": f"https://www.tiktok.com/@{username}"},
            headers=headers,
        )
    except Exception as exc:
        return SnipeResult(username, "tiktok", "error", type(exc).__name__)

    body = _json_or_none(resp)

    if resp.status_code == 200:
        if not isinstance(body, dict):
            return SnipeResult(username, "tiktok", "error", "unreadable body")
        author = str(body.get("author_name") or "")
        if author:
            return SnipeResult(username, "tiktok", "taken", author)
        return SnipeResult(username, "tiktok", "error", "no author_name in oembed body")
    if resp.status_code == 400:
        # oEmbed's "no such user" answer. Only trust it as *free* when it is the
        # shape oEmbed actually returns for a missing handle.
        if isinstance(body, dict) and not body.get("author_name"):
            return SnipeResult(username, "tiktok", "available", "oembed 400")
        return SnipeResult(username, "tiktok", "error", "unexpected 400 body")
    if resp.status_code in (403, 429):
        wait = retry_after_seconds(body)
        detail = f"status {resp.status_code}" + (f", retry_after {wait:.0f}s" if wait else "")
        return SnipeResult(username, "tiktok", "blocked", detail, retry_after=wait)
    return SnipeResult(username, "tiktok", "error", f"status {resp.status_code}")


# --- Status-only checks -----------------------------------------------------
# YouTube's /@handle is a 1.5 MB page for a live handle and a 404 for a free
# one; the body is never part of the answer. Reading it was the entire cost of
# that check. Streaming and leaving the context at the headers drops it to zero
# bytes -- measured at 76 ms against 450 ms for a HEAD, which still makes the
# server build the page. Abandoning the body costs the connection (httpx closes
# it rather than draining 1.5 MB to reuse a socket), which is still the cheap
# side of the trade at these sizes.
async def _status_only(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> tuple[int | None, str | None]:
    """``(status, error_name)`` -- send the request, read only the status line."""
    try:
        async with client.stream("GET", url, headers=headers) as resp:
            return resp.status_code, None
    except Exception as exc:
        return None, type(exc).__name__


def _blocked(
    username: str, platform: str, resp: httpx.Response, note: str = "rate limited"
) -> SnipeResult:
    """A platform refusal, which is not the proxy's fault and is not a verdict."""
    wait = retry_after_seconds(_json_or_none(resp))
    detail = note + (f", retry_after {wait:.0f}s" if wait else "")
    return SnipeResult(username, platform, "blocked", detail, retry_after=wait)


# --- Roblox ----------------------------------------------------------------
# Roblox publishes the validator its own signup form uses, so this is the
# cheapest and most exact check in the set: one ~50 byte JSON body whose `code`
# *is* the verdict, including whether the name is filtered as inappropriate.
ROBLOX_VALIDATE = "https://auth.roblox.com/v1/usernames/validate"
ROBLOX_CODES: dict[int, tuple[str, str]] = {
    0: ("available", "usable"),
    1: ("taken", "already in use"),
    2: ("invalid", "not appropriate for Roblox"),
    3: ("invalid", "3 to 20 characters"),
    4: ("invalid", "cannot start or end with _"),
    5: ("invalid", "too many underscores"),
    6: ("invalid", "not allowed"),
    7: ("invalid", "only a-z, A-Z, 0-9 and _"),
}


async def check_roblox(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            ROBLOX_VALIDATE,
            params={"birthday": "2000-01-01", "context": "Signup", "username": username},
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return SnipeResult(username, "roblox", "error", type(exc).__name__)

    if resp.status_code in (401, 403, 429):
        return _blocked(username, "roblox", resp)
    if resp.status_code != 200:
        return SnipeResult(username, "roblox", "error", f"status {resp.status_code}")

    body = _json_or_none(resp)
    if not isinstance(body, dict):
        return SnipeResult(username, "roblox", "error", "unreadable body")
    code = body.get("code")
    verdict = ROBLOX_CODES.get(code) if isinstance(code, int) else None
    if verdict is None:
        return SnipeResult(username, "roblox", "error", f"unexpected code {code!r}")
    status, detail = verdict
    return SnipeResult(username, "roblox", status, detail)


# --- Minecraft -------------------------------------------------------------
# The current profile lookup. 200 carries the account and its canonical
# capitalisation, 404 means no such profile. Note the 404 is also what an
# unregistrable name gets, which is exactly why NAMESPACES rejects "-" and short
# names before a request is spent.
MC_LOOKUP = "https://api.minecraftservices.com/minecraft/profile/lookup/name/{}"


async def check_minecraft(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            MC_LOOKUP.format(username),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return SnipeResult(username, "minecraft", "error", type(exc).__name__)

    if resp.status_code in (401, 403, 429):
        return _blocked(username, "minecraft", resp)
    if resp.status_code == 404:
        return SnipeResult(username, "minecraft", "available", "no profile")
    if resp.status_code == 400:
        return SnipeResult(username, "minecraft", "invalid", "rejected by the lookup")
    if resp.status_code != 200:
        return SnipeResult(username, "minecraft", "error", f"status {resp.status_code}")

    body = _json_or_none(resp)
    if not isinstance(body, dict) or not body.get("id"):
        return SnipeResult(username, "minecraft", "error", "unreadable body")
    return SnipeResult(username, "minecraft", "taken", str(body.get("name") or username))


# --- GitHub ----------------------------------------------------------------
# Workable only with a token: unauthenticated the REST API allows 60 requests an
# hour per IP, which is not a check rate -- it is a limit you hit in a minute and
# then sit out for the rest of it. GITHUB_TOKEN raises that to 5000.
GITHUB_USER = "https://api.github.com/users/{}"


async def check_github(client: httpx.AsyncClient, username: str) -> SnipeResult:
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        resp = await client.get(GITHUB_USER.format(username), headers=headers)
    except Exception as exc:
        return SnipeResult(username, "github", "error", type(exc).__name__)

    if resp.status_code == 404:
        return SnipeResult(username, "github", "available", "no such user")
    if resp.status_code in (401, 403, 429):
        # 403 is also GitHub's rate-limit answer, and it says so in the headers.
        left = resp.headers.get("x-ratelimit-remaining")
        note = "rate limited" if left in (None, "0") else f"status {resp.status_code}"
        return _blocked(username, "github", resp, note)
    if resp.status_code != 200:
        return SnipeResult(username, "github", "error", f"status {resp.status_code}")

    body = _json_or_none(resp)
    if not isinstance(body, dict):
        return SnipeResult(username, "github", "error", "unreadable body")
    login = str(body.get("login") or "")
    if not login:
        return SnipeResult(username, "github", "error", "no login in body")
    return SnipeResult(username, "github", "taken", login)


# --- Telegram --------------------------------------------------------------
# t.me answers 200 either way; the difference is whether the page carries a
# rendered profile. A live handle has ``tgme_page_title``; a free one gets the
# generic messenger page. Both markers sit in the <head>, so the first 16 KB
# decides it.
async def check_telegram(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            f"https://t.me/{username}",
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
        )
    except Exception as exc:
        return SnipeResult(username, "telegram", "error", type(exc).__name__)

    if resp.status_code in (401, 403, 429):
        return _blocked(username, "telegram", resp)
    if resp.status_code == 404:
        return SnipeResult(username, "telegram", "available", "404")
    if resp.status_code != 200:
        return SnipeResult(username, "telegram", "error", f"status {resp.status_code}")

    body = resp.content[:MAX_HEAD_BYTES]
    if b"tgme_page_title" in body:
        match = TITLE_RE.search(body.decode("utf-8", errors="ignore"))
        detail = match.group(1).strip()[:60] if match else "live profile"
        return SnipeResult(username, "telegram", "taken", detail)
    if b"Telegram Messenger" in body:
        return SnipeResult(username, "telegram", "available", "no profile page")
    return SnipeResult(username, "telegram", "error", "unrecognised page")


# --- YouTube ---------------------------------------------------------------
async def check_youtube(client: httpx.AsyncClient, username: str) -> SnipeResult:
    """A live handle answers 200, a free one 404. Only the status is read."""
    code, err = await _status_only(
        client,
        f"https://www.youtube.com/@{username}",
        {"User-Agent": BROWSER_UA, "Accept": "text/html"},
    )
    if err:
        return SnipeResult(username, "youtube", "error", err)
    if code == 200:
        return SnipeResult(username, "youtube", "taken", "channel exists")
    if code == 404:
        return SnipeResult(username, "youtube", "available", "404")
    if code in (401, 403, 429):
        return SnipeResult(username, "youtube", "blocked", f"status {code}")
    return SnipeResult(username, "youtube", "error", f"status {code}")


# --- X / Twitter -----------------------------------------------------------
async def check_x(client: httpx.AsyncClient, username: str) -> SnipeResult:
    """Same shape as YouTube: 200 for a live handle, 404 for a free one.

    X serves these to a datacenter IP far less willingly than the rest of the
    list, so a 403 here is the expected answer on a bad proxy and is reported as
    a block rather than an error.
    """
    code, err = await _status_only(
        client,
        f"https://x.com/{username}",
        {"User-Agent": BROWSER_UA, "Accept": "text/html"},
    )
    if err:
        return SnipeResult(username, "x", "error", err)
    if code == 200:
        return SnipeResult(username, "x", "taken", "profile exists")
    if code == 404:
        return SnipeResult(username, "x", "available", "404")
    if code in (401, 403, 429):
        return SnipeResult(username, "x", "blocked", f"status {code}")
    return SnipeResult(username, "x", "error", f"status {code}")



CHECKERS = {
    "discord": check_discord,
    "roblox": check_roblox,
    "minecraft": check_minecraft,
    "telegram": check_telegram,
    "instagram": check_instagram,
    "tiktok": check_tiktok,
    "x": check_x,
    "github": check_github,
    "youtube": check_youtube,
    "guns.lol": check_guns,
}


def normalize_platforms(platforms: list[str] | None) -> list[str]:
    if not platforms:
        return list(PLATFORMS)
    wanted = {p.strip().lower() for p in platforms if p and p.strip()}
    return [p for p in PLATFORMS if p.lower() in wanted] or list(PLATFORMS)


class RateMeter:
    """Counts completed checks and reports both the trailing and overall rate.

    ``recent`` is what the dashboard shows, because a run that slows down
    halfway should report the slowdown rather than the flattering average of
    everything before it. A rate that divides by a span of ~0 -- a batch landing
    in the same instant -- would report thousands a second, so the divisor is
    floored at ``_MIN_RATE_SPAN``.
    """

    _MIN_RATE_SPAN = 0.2

    def __init__(self, window: float = 5.0) -> None:
        self.started = time.perf_counter()
        self.window = window
        self.count = 0
        self._marks: deque[float] = deque()

    def tick(self, n: int = 1) -> None:
        now = time.perf_counter()
        self.count += n
        self._marks.append(now)
        cutoff = now - self.window
        while self._marks and self._marks[0] < cutoff:
            self._marks.popleft()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def recent(self) -> float:
        if not self._marks:
            return 0.0
        span = time.perf_counter() - self._marks[0]
        return len(self._marks) / max(span, self._MIN_RATE_SPAN)

    @property
    def average(self) -> float:
        return self.count / max(self.elapsed, self._MIN_RATE_SPAN)


# --- the batch ------------------------------------------------------------
async def check_once(platform: str, username: str, pool: ProxyPool) -> SnipeResult:
    """One name on one platform, through the next proxy the pool hands out."""
    proxy = pool.next()
    if proxy is None:
        detail = POOL_MISS_DETAILS[0] if len(pool) else POOL_MISS_DETAILS[1]
        return SnipeResult(username, platform, "error", detail)
    started = time.perf_counter()
    try:
        result = await CHECKERS[platform](pool.client(proxy), username)
    except Exception as exc:  # pragma: no cover - guard against library surprises
        result = SnipeResult(username, platform, "error", f"unexpected:{type(exc).__name__}")
    result.latency_ms = (time.perf_counter() - started) * 1000.0
    result.proxy = proxy
    # How a result reflects on the proxy depends on what happened: a block is the
    # platform's doing, a transport error is the proxy's.
    pool.report(proxy, outcome_for(result.status), platform, result.retry_after)
    return result


def job_stream(usernames: Iterable[str], platforms: list[str]) -> Iterator[tuple[str, str, str]]:
    """``(kind, platform, username)`` jobs, produced lazily.

    Lazily on purpose: enumerating every 4-character name gives 456,976 names,
    and one ``(platform, username)`` pair per name per platform is 1.8M tuples
    -- a couple of hundred MB held at once for a queue that is only ever
    consumed front to back. Streaming the jobs keeps memory proportional to the
    names themselves and nothing else.
    """
    for username in usernames:
        for platform in platforms:
            fits = NAME_RE.match(username) is not None and name_fits(platform, username)
            yield ("job" if fits else "invalid", platform, username)


async def iter_snipes(
    usernames: Iterable[str],
    platforms: list[str],
    pool: ProxyPool,
    concurrency: int = 64,
    retries: int = 2,
    window_factor: int = 4,
    stop: asyncio.Event | None = None,
) -> AsyncIterator[SnipeResult]:
    """Yield results as they land, retrying a blocked name on another proxy.

    A name the platform refused (blocked) or the proxy could not carry (error)
    is retried on a different proxy, up to ``retries``. A definitive answer
    (available/taken/invalid) is never retried. Names that cannot be valid are
    rejected before a request is spent on them.

    The scheduler is a sliding window, not fixed chunks: the moment one check
    finishes another starts, so in-flight work stays at ``concurrency`` from the
    first result to the last.

    ``stop`` is the run's cancel token. When it is set the dispatch loop stops
    handing out new work, every in-flight check is cancelled, and the generator
    returns -- so /snipe/stop actually stops the run instead of merely orphaning
    the HTTP response.

    When the pool has nothing usable the run does not spray errors for the
    remaining names. It waits briefly for a resting proxy, and if that keeps
    failing it stops and records ``pool.stop_reason`` -- which is how a
    platform-wide 429 ends up reported as "the platform is throttling" instead
    of "your list is exhausted".
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    window = max(1, concurrency * window_factor)

    async def one(platform: str, username: str) -> SnipeResult | None:
        async with sem:
            attempt = 0
            while True:
                if stop is not None and stop.is_set():
                    return None
                result = await check_once(platform, username, pool)
                if result.status in ("blocked", "error") and attempt < retries:
                    attempt += 1
                    continue
                return result

    def harvest(task: asyncio.Task) -> SnipeResult | None:
        del pending[task]
        return task.result()

    jobs = job_stream(usernames, platforms)
    jobs_done = False
    held: tuple[str, str, str] | None = None
    pending: dict[asyncio.Task, None] = {}
    empty_streak = 0
    answered = 0
    skipped = 0
    stop_task: asyncio.Task | None = None
    if stop is not None:
        stop_task = asyncio.create_task(stop.wait())

    try:
        while True:
            if stop is not None and stop.is_set():
                break

            # Collect anything already finished before deciding what to do next,
            # so the wait below can never spin on tasks that are done but unread.
            for task in [t for t in pending if t.done()]:
                result = harvest(task)
                if result is not None:
                    empty_streak = 0
                    answered += 1 if result.answered else 0
                    yield result

            # Any platform that has now refused the whole pool is sat out with
            # the reason the platform gave. The run continues on the platforms
            # that are still answering instead of stopping on the worst one.
            pool.pause_blocked(platforms)
            if platforms and all(pool.is_paused(p) for p in platforms):
                pool.stop_reason = pool.blocked_reason()
                break

            while not jobs_done and len(pending) < window:
                if held is not None:
                    item, held = held, None
                else:
                    item = next(jobs, None)
                    if item is None:
                        jobs_done = True
                        break
                kind, platform, username = item
                # A name that cannot be valid needs no proxy and no network, so
                # it is answered even while every proxy is resting. Gating it on
                # pool health silently dropped these results entirely.
                if kind == "invalid":
                    yield SnipeResult(username, platform, "invalid", namespace_note(platform))
                    continue
                if pool.is_paused(platform):
                    # No proxy and no network is spent on a platform that has
                    # refused the whole list; the jobs are dropped, not answered.
                    skipped += 1
                    pool.skipped = skipped
                    continue
                if pool.usable() == 0:
                    held = item
                    break
                pending[asyncio.create_task(one(platform, username))] = None

            if jobs_done and not pending:
                break

            # Nothing left to dispatch and nothing in flight: the pool, not the
            # name list, is what is holding the run up.
            if not jobs_done and not pending and (held is not None or pool.usable() == 0):
                if pool.alive() == 0:
                    pool.stop_reason = pool.exhausted_reason()
                    break
                if not await pool.wait_usable(POOL_WAIT_MAX, stop):
                    if stop is not None and stop.is_set():
                        break
                    empty_streak += 1
                    if empty_streak >= POOL_EMPTY_STREAK:
                        pool.stop_reason = pool.blocked_reason()
                        break
                continue

            if not pending:
                break

            waiters: set[asyncio.Task] = set(pending)
            if stop_task is not None and not stop_task.done():
                waiters.add(stop_task)
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is stop_task:
                    continue
                result = harvest(task)
                if result is not None:
                    empty_streak = 0
                    answered += 1 if result.answered else 0
                    yield result
            pool.pause_blocked(platforms)
            if platforms and all(pool.is_paused(p) for p in platforms):
                pool.stop_reason = pool.blocked_reason()
                break
    finally:
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
        for task in pending:
            task.cancel()
        if pending:
            # Wait for the cancellations to land before returning: the caller
            # telling the user "stopped" should mean no request is still out.
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except asyncio.CancelledError:
                # We are being torn down by the caller; cancel() above already
                # reached every task, so there is nothing left to await.
                pass
