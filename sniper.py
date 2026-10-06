"""Username availability checks for Discord, guns.lol and Instagram.

Every check goes out through a **validated** proxy from the store, because all
three targets rate-limit or block by IP -- which is the whole reason the proxy
list exists. Three things make a batch of names fast:

* **A warm client per proxy.** The proxy CONNECT tunnel and the TLS handshake to
  the target are paid once and then reused across names (httpx keeps a
  per-host keep-alive pool). Creating a client per name -- and so re-tunnelling
  and re-handshaking every time -- is the single biggest cost in a batch.
* **A pool that remembers what just refused.** A proxy that blocks or errors is
  rested for ``SNIPE_PROXY_COOLDOWN``, so a retry lands on a different, healthy
  host instead of the one that just failed. Proxies already proven against a
  platform are preferred for that platform.
* **Only the bytes that answer the question.** A profile page is tens of KB of
  Next.js payload; the ``<title>`` the check turns on is inside the first few
  KB, so the body is streamed and abandoned there.

The three endpoints, and what each answer means, were read off the live sites
rather than assumed:

* **Discord** -- the unauthenticated signup form's own endpoint,
  ``POST /api/v9/unique-username/username-attempt-unauthed``. It answers
  ``{\"taken\": true|false}``. A 400 carries a *validation* error (a reserved
  word, bad length) and must never be read as "available".
* **guns.lol** -- ``GET /{name}`` always returns 200 (it is a Next.js app), so
  the status is useless. A live profile embeds a Schema.org block
  (``profile-page-json-ld``, with ``"identifier": "<name>"``) at 8-9 KB, and a
  free name instead renders the site's default page. The ``<title>`` works too
  (``@name | guns.lol`` vs. the default title) but the edge cache streams it at
  4 KB or at 38 KB depending on the render, so the marker is the primary
  signal and the title is the confirmation.
* **Instagram** -- ``GET /api/v1/users/web_profile_info/?username=`` with the
  web app's own ``x-ig-app-id``. 200 with a user object = taken, 404 =
  available, and 429/401/302 = blocked, which is reported as such rather than
  guessed at. A datacenter IP is refused outright, which is exactly what the
  proxy pool is for.
"""

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

import httpx

from config import SNIPE_CONNECT_TIMEOUT, SNIPE_PROXY_COOLDOWN, SNIPE_READ_TIMEOUT

log = logging.getLogger("proxy-scraper.sniper")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
IG_APP_ID = "936619743392459"
DISCORD_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) discord/1.0"

DISCORD_ATTEMPT = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"
GUNS_DEFAULT_TITLE = "guns.lol: Everything you want, right here."
GUNS_TITLE_RE = re.compile(r"^\s*@[^|]{1,64}\|\s*guns\.lol\s*$", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# A live profile embeds its own Schema.org block; a free name renders the site's
# default page without it. Measured over repeated requests this marker lands at
# 8-9 KB on every taken profile, while the <title> of the same page lands at
# 4 KB or 38 KB depending on which way the edge cache streamed it -- which is
# why the marker, not the title, is the primary signal here.
GUNS_PROFILE_MARKER = b"profile-page-json-ld"
GUNS_IDENTIFIER_RE = re.compile(r'"identifier"\s*:\s*"([^"]{1,64})"')

# How much of a profile page to read. Both signals above land well inside this
# while the page itself runs 22-40 KB, so the head is most of the transfer.
MAX_HEAD_BYTES = 16 * 1024

# 1-32 of the characters the strictest of the three accepts. Anything else is
# rejected before a request is spent on it.
NAME_RE = re.compile(r"^[A-Za-z0-9._]{1,32}$")

PLATFORMS = ("discord", "guns.lol", "instagram")

# The statuses that are an answer. Everything else is worth another proxy.
FINAL_STATUSES = ("available", "taken", "invalid")


@dataclass
class SnipeResult:
    username: str
    platform: str
    status: str  # available | taken | invalid | blocked | error
    detail: str = ""
    proxy: str | None = None
    latency_ms: float | None = None

    @property
    def is_available(self) -> bool:
        return self.status == "available"


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=SNIPE_CONNECT_TIMEOUT,
        read=SNIPE_READ_TIMEOUT,
        write=SNIPE_READ_TIMEOUT,
        pool=SNIPE_READ_TIMEOUT,
    )


class ProxyPool:
    """The validated proxies a batch rotates over, plus their kept-alive clients.

    ``len(pool) == 0`` means the store has nothing alive; callers report that as
    "no validated proxies yet" rather than silently going direct. There is no
    lock on ``next``/``client``: they never await, so on the single event loop
    they cannot interleave.
    """

    def __init__(
        self,
        proxies: Iterable[str],
        per_platform: dict[str, Iterable[str]] | None = None,
        cooldown: float = SNIPE_PROXY_COOLDOWN,
        per_proxy: int = 4,
    ) -> None:
        self._all = list(dict.fromkeys(proxies))
        known = set(self._all)
        self._by_platform = {
            name: [p for p in dict.fromkeys((per_platform or {}).get(name, ())) if p in known]
            for name in PLATFORMS
        }
        self._cooldown = max(0.0, cooldown)
        self._per_proxy = max(1, per_proxy)
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._resting: dict[str, float] = {}
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._all)

    def platform_size(self, platform: str) -> int:
        return len(self._by_platform.get(platform, ()))

    def next(self, platform: str | None = None) -> str | None:
        """The next proxy to use, preferring one already proven for ``platform``."""
        now = time.time()
        for group in (self._by_platform.get(platform or "", ()), self._all):
            ready = [p for p in group if self._resting.get(p, 0.0) <= now]
            if ready:
                picked = ready[self._cursor % len(ready)]
                self._cursor += 1
                return picked
        if not self._all:
            return None
        # Every proxy is resting: use whichever comes back first.
        return min(self._all, key=lambda p: self._resting.get(p, 0.0))

    def client(self, proxy: str) -> httpx.AsyncClient:
        """The kept-alive client for one proxy, created on first use.

        One client per proxy carries every platform, so the tunnel and the TLS
        session to each target are established once per batch, not once per name.
        """
        client = self._clients.get(proxy)
        if client is None:
            client = httpx.AsyncClient(
                proxy=proxy,
                timeout=_timeout(),
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=self._per_proxy,
                    max_keepalive_connections=self._per_proxy,
                ),
            )
            self._clients[proxy] = client
        return client

    def report(self, proxy: str, ok: bool) -> None:
        """Remember whether a proxy just behaved, so retries avoid a bad one."""
        if ok:
            self._resting.pop(proxy, None)
        elif self._cooldown > 0:
            self._resting[proxy] = time.time() + self._cooldown

    def resting(self) -> int:
        now = time.time()
        return sum(1 for until in self._resting.values() if until > now)

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - closing is best effort
                log.debug("closing a sniper client failed", exc_info=True)


# --- Discord --------------------------------------------------------------
async def check_discord(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.post(
            DISCORD_ATTEMPT,
            json={"username": username},
            headers={"Content-Type": "application/json", "User-Agent": DISCORD_UA},
        )
    except httpx.HTTPError as exc:
        return SnipeResult(username, "discord", "error", type(exc).__name__)

    if resp.status_code == 200:
        try:
            taken = bool(resp.json().get("taken"))
        except ValueError:
            return SnipeResult(username, "discord", "error", "unreadable body")
        return SnipeResult(username, "discord", "taken" if taken else "available")
    if resp.status_code == 429:
        return SnipeResult(username, "discord", "blocked", "rate limited")
    if resp.status_code == 400:
        # A validation refusal, not an availability answer.
        return SnipeResult(username, "discord", "invalid", _discord_reason(resp))
    return SnipeResult(username, "discord", "error", f"status {resp.status_code}")


def _discord_reason(resp: httpx.Response) -> str:
    try:
        errors = resp.json().get("errors", {}).get("username", {}).get("_errors") or []
        if errors:
            return str(errors[0].get("code") or "invalid")
    except ValueError:
        pass
    return "invalid"


# --- guns.lol -------------------------------------------------------------
def _guns_verdict(username: str, head: bytes) -> SnipeResult | None:
    """Read a verdict off the part of the page read so far, or None for "more"."""
    if GUNS_PROFILE_MARKER in head:
        found = GUNS_IDENTIFIER_RE.search(head.decode("utf-8", errors="ignore"))
        return SnipeResult(
            username, "guns.lol", "taken", f"@{found.group(1)}" if found else "live profile"
        )
    if b"</title>" not in head:
        return None
    match = TITLE_RE.search(head.decode("utf-8", errors="ignore"))
    if not match:
        return None
    title = match.group(1).strip()
    if title == GUNS_DEFAULT_TITLE:
        return SnipeResult(username, "guns.lol", "available", "default page")
    if GUNS_TITLE_RE.match(title):
        return SnipeResult(username, "guns.lol", "taken", title)
    # A title we do not recognise: keep reading, another marker may still land.
    return None


async def _drain(response: httpx.Response) -> None:
    """Finish an abandoned response so its connection returns to the keep-alive
    pool instead of being torn down. The verdict is already in hand, so every
    failure here is irrelevant."""
    try:
        async for _ in response.aiter_bytes():
            pass
    except Exception:  # pragma: no cover - verdict already decided
        pass
    finally:
        try:
            await response.aclose()
        except Exception:  # pragma: no cover - closing is best effort
            pass


async def check_guns(client: httpx.AsyncClient, username: str) -> SnipeResult:
    head = bytearray()
    request = client.build_request(
        "GET", f"https://guns.lol/{username}", headers={"User-Agent": BROWSER_UA}
    )
    try:
        resp = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        return SnipeResult(username, "guns.lol", "error", type(exc).__name__)

    verdict: SnipeResult | None = None
    try:
        if resp.status_code == 429:
            verdict = SnipeResult(username, "guns.lol", "blocked", "rate limited")
        elif resp.status_code == 404:
            verdict = SnipeResult(username, "guns.lol", "available", "404")
        elif resp.status_code != 200:
            verdict = SnipeResult(username, "guns.lol", "error", f"status {resp.status_code}")
        else:
            # Stop the moment the page has said which it is; the remaining tens
            # of KB are Next.js payload this check has no use for.
            async for chunk in resp.aiter_bytes():
                head.extend(chunk)
                verdict = _guns_verdict(username, head)
                if verdict is not None:
                    break
                if len(head) >= MAX_HEAD_BYTES:
                    break
    except httpx.HTTPError as exc:
        await resp.aclose()
        return SnipeResult(username, "guns.lol", "error", type(exc).__name__)

    # Reading only the head is the whole point, but an abandoned response closes
    # its connection and the next name would pay the handshake again. Draining
    # the rest in the background gets both: the verdict now, the tunnel warm.
    asyncio.create_task(_drain(resp))

    if verdict is not None:
        return verdict
    # Neither a profile block nor a title we know: say so instead of guessing.
    text = head.decode("utf-8", errors="ignore")
    match = TITLE_RE.search(text)
    if match:
        return SnipeResult(username, "guns.lol", "error", f"unexpected title: {match.group(1).strip()[:60]}")
    return SnipeResult(username, "guns.lol", "error", f"no title in the first {len(head) // 1024} KB")


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
    except httpx.HTTPError as exc:
        return SnipeResult(username, "instagram", "error", type(exc).__name__)

    if resp.status_code == 404:
        return SnipeResult(username, "instagram", "available", "404")
    if resp.status_code in (401, 429, 302):
        return SnipeResult(username, "instagram", "blocked", f"status {resp.status_code}")
    if resp.status_code != 200:
        return SnipeResult(username, "instagram", "error", f"status {resp.status_code}")

    try:
        user = (resp.json().get("data") or {}).get("user")
    except ValueError:
        return SnipeResult(username, "instagram", "error", "unreadable body")
    if user is None:
        return SnipeResult(username, "instagram", "available", "no user object")
    return SnipeResult(username, "instagram", "taken", str(user.get("username") or ""))


CHECKERS = {
    "discord": check_discord,
    "guns.lol": check_guns,
    "instagram": check_instagram,
}


def normalize_platforms(platforms: list[str] | None) -> list[str]:
    if not platforms:
        return list(PLATFORMS)
    wanted = {p.strip().lower() for p in platforms if p and p.strip()}
    return [p for p in PLATFORMS if p.lower() in wanted] or list(PLATFORMS)


# --- the batch ------------------------------------------------------------
async def check_once(platform: str, username: str, pool: ProxyPool) -> SnipeResult:
    """One name on one platform, through the next proxy the pool hands out."""
    proxy = pool.next(platform)
    if proxy is None:
        return SnipeResult(username, platform, "error", "no validated proxies")
    started = time.perf_counter()
    try:
        result = await CHECKERS[platform](pool.client(proxy), username)
    except Exception as exc:  # pragma: no cover - guard against library surprises
        result = SnipeResult(username, platform, "error", f"unexpected:{type(exc).__name__}")
    result.latency_ms = (time.perf_counter() - started) * 1000.0
    result.proxy = proxy
    # A block or a transport failure says nothing about the name, only about the
    # proxy -- rest it so the retry gets a different host.
    pool.report(proxy, result.status in FINAL_STATUSES)
    return result


async def iter_snipes(
    usernames: list[str],
    platforms: list[str],
    pool: ProxyPool,
    concurrency: int = 32,
    retries: int = 2,
    window_factor: int = 4,
) -> AsyncIterator[SnipeResult]:
    """Yield results as they land, retrying a blocked name on another proxy.

    A name the platform refused (blocked) or the proxy could not carry (error)
    is retried on a different proxy, up to ``retries`` -- that is the whole
    point of having a pool. A definitive answer (available/taken/invalid) is
    never retried. Names that cannot be valid on any of the three platforms are
    rejected before a request is spent on them.
    """
    queue: list[tuple[str, str]] = []
    for username in usernames:
        if not NAME_RE.match(username):
            for platform in platforms:
                yield SnipeResult(username, platform, "invalid", "bad characters or length")
            continue
        queue.extend((platform, username) for platform in platforms)
    if not queue:
        return

    sem = asyncio.Semaphore(max(1, concurrency))
    window = max(1, concurrency * window_factor)
    offset = 0

    async def one(platform: str, username: str) -> SnipeResult:
        async with sem:
            attempt = 0
            while True:
                result = await check_once(platform, username, pool)
                if result.status in ("blocked", "error") and attempt < retries:
                    attempt += 1
                    continue
                return result

    # Outstanding tasks are bounded to `window` so a thousand names cannot spawn
    # a task each, while one semaphore keeps in-flight requests at the ceiling.
    while offset < len(queue):
        chunk = queue[offset : offset + window]
        offset += len(chunk)
        tasks = [asyncio.create_task(one(p, u)) for p, u in chunk]
        try:
            for finished in asyncio.as_completed(tasks):
                yield await finished
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
