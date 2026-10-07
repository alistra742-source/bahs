"""Username availability checks for Discord, guns.lol, Instagram and TikTok.

Every check goes out through a proxy from the stored list, because all four
targets rate-limit or block by IP. Four things make a batch of names fast:

* **A warm client per proxy.** The proxy CONNECT tunnel and the TLS handshake to
  the target are paid once and then reused across names (httpx keeps a per-host
  keep-alive pool). Creating a client per name -- and so re-tunnelling and
  re-handshaking every time -- is the single biggest cost in a batch.
* **A bounded number of warm clients.** One client per proxy is only cheap up to
  a point; past ``SNIPE_MAX_CLIENTS`` the least recently used one is closed, so
  a 100k-line list cannot turn into an fd and buffer leak.
* **A pool that remembers what just refused.** A proxy that blocks or errors is
  rested for ``SNIPE_PROXY_COOLDOWN``, so a retry lands on a different host, and
  one that keeps failing is retired for the rest of the run.
* **Only the bytes that answer the question.** A profile page is tens of KB of
  framework payload; the marker the check turns on is inside the first few KB,
  so the body is streamed and abandoned there.

The four endpoints, and what each answer means, were read off the live sites
rather than assumed:

* **Discord** -- the unauthenticated signup form's own endpoint,
  ``POST /api/v9/unique-username/username-attempt-unauthed``. It answers
  ``{"taken": true|false}``. A 400 carries a *validation* error (a reserved
  word, bad length) and must never be read as "available".
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
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

import httpx

from config import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    SNIPE_MAX_CLIENTS,
    SNIPE_PER_PROXY,
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

# How much of a profile page to read. Both guns.lol signals land well inside
# this while the page itself runs 21-40 KB, so the head is most of the transfer.
MAX_HEAD_BYTES = 16 * 1024

# 1-32 of the characters the strictest of the four accepts. Anything else is
# rejected before a request is spent on it.
NAME_RE = re.compile(r"^[A-Za-z0-9._]{1,32}$")

PLATFORMS = ("discord", "guns.lol", "instagram", "tiktok")

# The statuses that are an answer. Everything else is worth another proxy.
FINAL_STATUSES = ("available", "taken", "invalid")


def default_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT,
        write=READ_TIMEOUT,
        pool=READ_TIMEOUT,
    )


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
        per_proxy: int = SNIPE_PER_PROXY,
        timeout: httpx.Timeout | None = None,
        fail_limit: int = SNIPE_PROXY_FAIL_LIMIT,
        max_clients: int = SNIPE_MAX_CLIENTS,
    ) -> None:
        self._all = list(dict.fromkeys(proxies))
        self._cooldown = max(0.0, cooldown)
        self._per_proxy = max(1, per_proxy)
        self._timeout = timeout or default_timeout()
        self._max_clients = max(1, max_clients)
        self._clients: OrderedDict[str, httpx.AsyncClient] = OrderedDict()
        self._resting: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        # A proxy retired mid-run is never handed out again: one that has failed
        # `fail_limit` times is dead for this batch's purposes, and continuing
        # to rotate it in is where a wall of ConnectErrors comes from.
        self._retired: set[str] = set()
        self._fail_limit = max(1, fail_limit)
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._all)

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

    def report(self, proxy: str, ok: bool) -> None:
        """Remember whether a proxy just behaved, so retries avoid a bad one.

        A proxy that keeps failing is retired for the rest of the run; a single
        failure only rests it.
        """
        if ok:
            self._resting.pop(proxy, None)
            self._fails.pop(proxy, None)
            return
        fails = self._fails.get(proxy, 0) + 1
        self._fails[proxy] = fails
        if fails >= self._fail_limit:
            self._retired.add(proxy)
            client = self._clients.pop(proxy, None)
            if client is not None:
                # Free the sockets immediately; a retired proxy's warm pool is
                # dead weight against the process fd budget.
                self._close_later(client)
        elif self._cooldown > 0:
            self._resting[proxy] = time.time() + self._cooldown

    def alive(self) -> int:
        """Proxies still eligible to be handed out."""
        return sum(1 for p in self._all if p not in self._retired)

    def resting(self) -> int:
        now = time.time()
        return sum(1 for until in self._resting.values() if until > now)

    def retired(self) -> int:
        return len(self._retired)

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
    except Exception as exc:
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
            # of KB are framework payload this check has no use for.
            async for chunk in resp.aiter_bytes():
                head.extend(chunk)
                verdict = _guns_verdict(username, head)
                if verdict is not None:
                    break
                if len(head) >= MAX_HEAD_BYTES:
                    break
    except Exception as exc:
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
        return SnipeResult(
            username, "guns.lol", "error", f"unexpected title: {match.group(1).strip()[:60]}"
        )
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
    except Exception as exc:
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

    try:
        body = resp.json()
    except ValueError:
        body = None

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
        return SnipeResult(username, "tiktok", "blocked", f"status {resp.status_code}")
    return SnipeResult(username, "tiktok", "error", f"status {resp.status_code}")


CHECKERS = {
    "discord": check_discord,
    "guns.lol": check_guns,
    "instagram": check_instagram,
    "tiktok": check_tiktok,
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
    everything before it.

    Both rates divide by an elapsed time, and a batch of checks that lands in
    the same instant -- or a run that finishes before its first second -- makes
    that divisor ~0 and reports thousands of checks a second. The divisor is
    therefore floored at ``_MIN_RATE_SPAN``: a rate is never a number the clock
    cannot support.
    """

    # Shortest span a reported rate may be computed over. Below this the count
    # is divided by this instead, capping the figure at a plausible value.
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
        detail = "proxies exhausted this run" if len(pool) else "no proxies saved"
        return SnipeResult(username, platform, "error", detail)
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
    concurrency: int = 64,
    retries: int = 2,
    window_factor: int = 4,
    stop: asyncio.Event | None = None,
) -> AsyncIterator[SnipeResult]:
    """Yield results as they land, retrying a blocked name on another proxy.

    A name the platform refused (blocked) or the proxy could not carry (error)
    is retried on a different proxy, up to ``retries`` -- that is the whole
    point of having a list. A definitive answer (available/taken/invalid) is
    never retried. Names that cannot be valid are rejected before a request is
    spent on them.

    The scheduler is a sliding window, not fixed chunks: the moment one check
    finishes another starts, so in-flight work stays at ``concurrency`` from the
    first result to the last. Waiting for a whole chunk to drain instead leaves
    the tail of every chunk idle, which is exactly the throughput a run cannot
    spare.

    ``stop`` is the run's cancel token. When it is set the dispatch loop stops
    handing out new work, every in-flight check is cancelled, and the generator
    returns -- so /snipe/stop actually stops the run instead of merely orphaning
    the HTTP response. Cancellation is awaited in the ``finally`` rather than
    fired and forgotten, so by the time this generator closes no check is still
    holding a proxy or a socket.
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

    # Outstanding tasks are bounded to `window` so a hundred thousand checks
    # cannot spawn a task each, while one semaphore keeps in-flight requests at
    # the ceiling.
    pending: dict[asyncio.Task, None] = {}
    # A waiter on the run's stop event, so a stop interrupts the dispatch wait
    # immediately. Waiting on `pending` alone means the stop is only noticed
    # after the next check happens to finish -- and a check stuck on a
    # slow-but-connected proxy does not finish until its read timeout, so a
    # "stop" could take the full timeout to take effect.
    stop_task: asyncio.Task | None = None
    if stop is not None:
        stop_task = asyncio.create_task(stop.wait())
    offset = 0
    try:
        while offset < len(queue) or pending:
            if stop is not None and stop.is_set():
                break
            while offset < len(queue) and len(pending) < window:
                if stop is not None and stop.is_set():
                    break
                platform, username = queue[offset]
                offset += 1
                pending[asyncio.create_task(one(platform, username))] = None
            if not pending:
                break
            waiters: set[asyncio.Task] = set(pending)
            if stop_task is not None and not stop_task.done():
                waiters.add(stop_task)
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is stop_task:
                    continue
                del pending[task]
                result = task.result()
                if result is not None:
                    yield result
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
