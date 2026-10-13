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
  A *banned* account renders that same default page with no block at all, so it
  was reported free like any untouched name -- the ban page carries its own
  heading at ~9.3 KB and is read as its own verdict. The site also answers a
  datacenter exit with a "Please wait a moment" clearance page (401, ~4 KB,
  sometimes 200 under load), which is a refusal and never a verdict.
* **Instagram** -- ``GET /api/v1/users/web_profile_info/?username=`` with the
  web app's own ``x-ig-app-id``. 200 with a user object = taken, 404 =
  available, and 429/401/302 = blocked, which is reported as such rather than
  guessed at. A datacenter IP is refused outright, which is exactly what the
  proxy list is for.
* **TikTok** -- two sources, because neither is right on its own. oEmbed
  (``GET /oembed?url=.../@name``, 45 bytes, asked about the lower-cased handle)
  answers 200 with an ``author_name`` for a public handle, and *only* then is a
  verdict free. Its 400 is not "no such user": measured on a random
  4-character sample, 7 of the 9 names oEmbed called missing were live accounts
  whose profile is not public (``statusCode`` 10222, ``ErrBizUserSecret``, with
  the account's own ``uniqueId``, id and follower count still in the page) --
  and a mixed-case spelling of a taken handle also comes back 400, because
  oEmbed only resolves the canonical spelling. So a 400 is *confirmed* against
  the profile page, which is authoritative in both directions and resolves any
  case: ``webapp.user-detail`` carrying ``userInfo`` means the handle is held,
  and TikTok's own ``10221`` means there is no such user. The page has to be
  asked for with a **mobile Safari** user agent: measured through one datacenter
  endpoint, a desktop UA gets a 1,462-byte "enable JavaScript" shell with no
  verdict in it for a live account and a free name alike, while an iPhone UA
  gets the real 166-202 KB page. That page leads the check for a second
  reason: TikTok throttles the embed endpoint per exit IP far harder than the
  page -- measured through one rotating endpoint at 16 concurrent, 56 of 80
  embed calls came back 429/503 while 80 of 80 page reads were answered -- so
  the page is both the authoritative source and the one that keeps working, and
  one request per name instead of a wasted round trip on every name. A response
  with neither marker is reported as unanswered -- never as available, which is
  the false positive this whole arrangement exists to kill.
"""

import asyncio
import itertools
import logging
import random
import re
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

from config import (
    USER_AGENT,
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    SHUFFLE_NAMES,
    SHUFFLE_WINDOW,
    SNIPE_BLOCK_COOLDOWN,
    SNIPE_BLOCK_MEMORY,
    SNIPE_MAX_CLIENTS,
    GITHUB_TOKEN,
    SNIPE_PER_PROXY,
    SNIPE_PLATFORM_BLOCK_MIN,
    SNIPE_PLATFORM_BLOCK_RATE,
    SNIPE_PLATFORM_MIN_PARALLEL,
    SNIPE_PLATFORM_PACE_RATE,
    SNIPE_PLATFORM_PACE_RECOVER,
    SNIPE_PLATFORM_PACE_WINDOW,
    SNIPE_PLATFORM_PAUSE,
    SNIPE_PLATFORM_PAUSE_MAX,
    SNIPE_PLATFORM_WAIT,
    SNIPE_PROXY_BLOCK_LIMIT,
    SNIPE_CONFIRM_FREE,
    SNIPE_PROXY_BLOCK_RATE,
    SNIPE_PROXY_COOLDOWN,
    SNIPE_PROXY_FAIL_LIMIT,
    SNIPE_PROXY_FAIL_RATE,
    SSL_CONTEXT,
    TIKTOK_PAGE_FIRST,
    TIKTOK_PAGE_TIMEOUT,
)

log = logging.getLogger("bahs.sniper")

# From config, so the version can be moved on without a code change.
BROWSER_UA = USER_AGENT
IG_APP_ID = "936619743392459"
DISCORD_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) discord/1.0"

DISCORD_ATTEMPT = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"
TIKTOK_OEMBED = "https://www.tiktok.com/oembed"
TIKTOK_PROFILE = "https://www.tiktok.com/@{handle}"
# TikTok gates the server-rendered profile on the client it thinks is asking: a
# datacenter request with a desktop user agent is answered with a 1.5 KB "enable
# JavaScript" shell whichever handle it names, and the same request with a mobile
# Safari agent is answered with the real page. Measured through one datacenter
# endpoint: desktop 1,462 bytes for a live handle and a free name alike, iPhone
# 166-202 KB with the verdict intact.
TIKTOK_IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)
# The profile page's own scope. `webapp.user-detail` opens the block that holds
# the account; 10221 is TikTok's "no such user" inside it. The window is wide
# enough to reach the status code that follows the (empty) user block on a free
# page, and narrow enough that a `uniqueId` in a later script cannot be misread
# as this account's handle.
TIKTOK_USER_DETAIL = '"webapp.user-detail"'
TIKTOK_NO_USER = '"statusCode":10221'
TIKTOK_UNIQUE_ID_RE = re.compile(r'"uniqueId":"([^"]{1,40})"')
TIKTOK_SCOPE_CHARS = 8000
GUNS_DEFAULT_TITLE = "guns.lol: Everything you want, right here."
GUNS_TITLE_RE = re.compile(r"^\s*@[^|]{1,64}\|\s*guns\.lol\s*$", re.I)
TIKTOK_HANDLE_RE = re.compile(r"/@([A-Za-z0-9._]{1,40})")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# A live profile embeds its own Schema.org block; a free name renders the site's
# default page without it.
GUNS_PROFILE_MARKER = b"profile-page-json-ld"
GUNS_IDENTIFIER_RE = re.compile(r'"identifier"\s*:\s*"([^"]{1,64})"')
# A *registered* handle ships its account payload in the page even when its
# public profile is gone, which is what a banned or hidden account looks like.
# That matters because such a page renders the site's *default* title -- the
# exact title a free name renders -- so a title-only test calls a banned profile
# free. Worst case: the dashboard says a name is available, and registering it
# fails. `account_created` is in every registered account's payload and in no
# free page's.
GUNS_ACCOUNT_MARKER = b"account_created"
# The page offers to *claim this name* only when it is free, so the offer itself
# is the positive signal. `/register?claim=<name>` is exactly what the free page
# links to, and a taken or banned page never carries it.
GUNS_CLAIM = b"/register?claim="

# A banned account's page carries neither the Schema.org block nor a claim
# offer -- it is byte-for-byte the "available" shape apart from its own heading,
# which sits at ~9.3 KB into a ~25 KB page. Read as its own verdict: the handle
# is held, nobody can register it, and the dashboard should say so rather than
# offering a name whose signup cannot succeed.
GUNS_BANNED_MARKERS = (b"This user has been banned", b"has been banned from")
# guns.lol's own clearance page and Cloudflare's interstitial. Neither carries a
# verdict about the name: they mean the *exit IP* was asked to wait, so they are a
# refusal -- which rests that proxy briefly and gets the name retried on another
# attempt -- and never an answer. Matched on the interstitial's own text only:
# Cloudflare appends a ``challenge-platform`` script reference to *every* page it
# serves, and matching on that turned a normal profile into a refusal and the
# whole platform into a wall.
GUNS_CHALLENGE_MARKERS = (
    b"Please wait a moment",
    b"Just a moment",
    b"Attention Required",
    b"challenge-error-text",
)

# How much of a profile page to look at. The signals live at the head of the
# page, but the account payload of a banned profile sits past 16 KB, and the
# whole body is already in memory by the time this is read -- the slice saves
# only the search, not the transfer -- so the window is comfortably wide.
MAX_HEAD_BYTES = 64 * 1024

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
    "bluesky": (re.compile(r"^[A-Za-z0-9-]{3,63}$"), "3-63 of a-z A-Z 0-9 -"),
    "kick": (re.compile(r"^[A-Za-z0-9_-]{3,25}$"), "3-25 of a-z A-Z 0-9 _ -"),
    "steam": (re.compile(r"^[A-Za-z0-9_-]{3,32}$"), "3-32 of a-z A-Z 0-9 _ -"),
    "soundcloud": (re.compile(r"^[A-Za-z0-9_-]{3,25}$"), "3-25 of a-z A-Z 0-9 _ -"),
    "dribbble": (re.compile(r"^[A-Za-z0-9-]{2,30}$"), "2-30 of a-z A-Z 0-9 -"),
    "chess.com": (re.compile(r"^[A-Za-z0-9_-]{3,25}$"), "3-25 of a-z A-Z 0-9 _ -"),
    "duolingo": (re.compile(r"^[A-Za-z0-9_-]{3,30}$"), "3-30 of a-z A-Z 0-9 _ -"),
    "wattpad": (re.compile(r"^[A-Za-z0-9_-]{3,25}$"), "3-25 of a-z A-Z 0-9 _ -"),
    "disqus": (re.compile(r"^[A-Za-z0-9-]{3,32}$"), "3-32 of a-z A-Z 0-9 -"),
    "vk": (re.compile(r"^[A-Za-z0-9._]{2,32}$"), "2-32 of a-z A-Z 0-9 . _"),
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
    "bluesky",
    "kick",
    "steam",
    "soundcloud",
    "dribbble",
    "chess.com",
    "duolingo",
    "wattpad",
    "disqus",
    "vk",
    "dockerhub",
    "codeberg",
    "mastodon",
    "dev.to",
    "lichess",
    "huggingface",
    "gitlab",
    "itch.io",
    "gumroad",
    "myanimelist",
)

# A name held by an account the platform has banned. Its own verdict rather than
# `taken`, because the two want different things from the user: a taken name is
# someone's live handle, a banned one is a slot nobody can claim.
STATUS_BANNED = "banned"

# The statuses that are an answer: a verdict about the name. `banned` is one of
# them -- a name held by a banned account is occupied, and saying "available"
# about it sends the user to a signup that cannot succeed.
FINAL_STATUSES = ("available", "taken", "invalid", STATUS_BANNED)

# A request that came back and said nothing usable: a challenge page, a status
# we do not read, a 200 whose body is missing the field we look at. Deliberately
# not "error": an error is the transport failing, and the two say opposite things
# about the proxy list. Charging an unreadable *answer* to the proxy is what
# retired healthy lists against a site serving challenges -- a run then reported
# "your list is dead" about a list that was working.
STATUS_UNANSWERED = "unanswered"

# Not a verdict and not a name: a status the scheduler emits while it has nothing
# to dispatch, so a run that is waiting on a resting pool says so instead of
# going quiet. Callers must drop these -- they are not rows.
WAITING_STATUS = "waiting"

# Not a verdict either: a keep-alive the scheduler emits when a run has been
# silent for a while with work still in flight. A long-lived NDJSON response is
# held open through whatever sits between the process and the browser -- a
# platform edge, a carrier NAT, a reverse proxy -- and those drop a connection
# that sends nothing for long enough. A slow platform (a 6s read timeout on
# every in-flight check) could leave the socket quiet for seconds at a time, and
# on a phone that reads as `stream error: Load failed` with the run still going.
# One line every few seconds costs nothing and keeps the pipe warm.
HEARTBEAT_STATUS = "heartbeat"

# How long the scheduler may sit silent before it emits a heartbeat.
HEARTBEAT_AFTER = 5.0

# How a result reflects on the proxy that carried it.
OUTCOME_OK = "ok"            # the check produced a verdict
OUTCOME_BLOCKED = "blocked"  # the platform refused: not the proxy's fault
OUTCOME_FAILED = "failed"    # transport, timeout: the proxy's fault
OUTCOME_UNKNOWN = "unknown"  # an answer we could not read: nobody's fault
OUTCOME_TIMEOUT = "timeout"  # carried through the tunnel, no answer in time: target

# The details check_once reports when the pool had nothing to hand out. Kept as a
# constant so a run can count them without matching on prose, and so the
# dashboard can show "proxy misses" as its own number instead of letting them
# disappear into the error bucket.
POOL_MISS_DETAILS = ("proxies exhausted this run", "no proxies saved")


def outcome_for(result: "SnipeResult") -> str:
    """What one result says about the proxy that carried it."""
    if result.status in FINAL_STATUSES:
        return OUTCOME_OK
    if result.status == "blocked":
        return OUTCOME_BLOCKED
    if result.status in ("error", STATUS_UNANSWERED):
        # The tunnel carried the request but nothing came back in time. That is
        # a slow target, not a dead proxy, and it gets its own outcome so the
        # pool rests the host briefly instead of retiring it.
        if result.fault == FAULT_TARGET:
            return OUTCOME_TIMEOUT
        # Only a transport failure is the tunnel's own doing. Everything else
        # arrived and was unreadable, which says nothing about the proxy.
        return OUTCOME_FAILED if result.fault == FAULT_PROXY else OUTCOME_UNKNOWN
    return OUTCOME_FAILED


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


def retry_after_seconds(body: object, resp: "httpx.Response | None" = None) -> float | None:
    """How long a platform asked us to wait, from the body or the headers.

    Discord puts it in the JSON body (``retry_after``, seconds). GitHub and
    Cloudflare put it in a ``Retry-After`` header -- seconds or an HTTP-date --
    and GitHub additionally sends ``x-ratelimit-reset`` as an epoch second.
    Reading only the body meant a platform that said "back off 30s" in a header
    was sat out for the default instead of for as long as it asked.
    """
    if isinstance(body, dict):
        for key in ("retry_after", "retry_after_ms", "retryAfter"):
            value = body.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                seconds = float(value)
                if key == "retry_after_ms":
                    seconds /= 1000.0
                if 0 < seconds < 86400:
                    return seconds
    if resp is None:
        return None
    raw = (resp.headers.get("retry-after") or "").strip()
    if raw:
        seconds: float | None
        try:
            seconds = float(raw)
        except ValueError:
            try:
                stamp = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                stamp = None
            seconds = (stamp.timestamp() - time.time()) if stamp is not None else None
        if seconds is not None and 0 < seconds < 86400:
            return seconds
    reset = (resp.headers.get("x-ratelimit-reset") or "").strip()
    if reset:
        try:
            delta = float(reset) - time.time()
        except ValueError:
            delta = 0.0
        if 0 < delta < 86400:
            return delta
    return None


FAULT_PROXY = "proxy"    # the tunnel could not carry the request
FAULT_TARGET = "target"  # the tunnel carried it; the far side never answered


def proxy_fault(exc: BaseException) -> bool:
    """Is this exception evidence the *proxy* is bad?

    A proxy that cannot open its tunnel is dead and should be retired quickly.
    A request that opened the tunnel, went out, and then timed out or came back
    mangled is the *target* being slow -- and the proxy's own usage panel still
    counts that request, which is why retiring the proxy for it produced the
    classic false report: "the list is dead" about a list that was working and
    visibly receiving traffic.

    Only the connect phase and the proxy library's own errors are the proxy's.

    * yes -- ``ProxyError`` (the tunnel refused), ``ConnectError`` (nothing
      answered the CONNECT), ``ConnectTimeout`` (the CONNECT never landed),
      ``UnsupportedProtocol``, and the SOCKS handshake errors those are raised
      from.
    * no  -- ``ReadTimeout`` / ``ReadError`` / ``WriteError`` /
      ``RemoteProtocolError``: bytes went out and the answer was late or
      malformed. The connect succeeded, so the exit IP is not what failed.
    """
    if isinstance(
        exc,
        (
            httpx.ProxyError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.UnsupportedProtocol,
        ),
    ):
        return True
    # `httpx[socks]` tunnels through socksio, whose handshake failures are its
    # own exception types rather than httpx ones (the original comment here
    # caught them by name for exactly that reason).
    module = (type(exc).__module__ or "").split(".")[0]
    if module in ("socks", "socksio"):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in ("ProxyError", "GeneralProxyError"):
            return True
    return False


def transport_error(username: str, platform: str, exc: BaseException) -> "SnipeResult":
    """The request did not reach a verdict -- say who that implicates."""
    fault = FAULT_PROXY if proxy_fault(exc) else FAULT_TARGET
    return SnipeResult(username, platform, "error", type(exc).__name__, fault=fault)


def shuffled(names: Iterable[str], window: int = SHUFFLE_WINDOW) -> Iterator[str]:
    """The names in random order, in bounded blocks.

    Every run draws its names in a different order, so two runs over the same
    selection never spend the same proxies on the same prefixes. Enumeration can
    be tens of millions of names and is deliberately lazy, so the shuffle is
    buffered rather than global: order is randomised within a block and memory
    stays flat. Any list that fits ``window`` -- every snipe batch, and any scan
    selection up to it -- is shuffled in full.
    """
    window = max(1, int(window))
    source = iter(names)
    while True:
        block = list(itertools.islice(source, window))
        if not block:
            return
        random.Random().shuffle(block)
        yield from block
        if len(block) < window:
            return


@dataclass
class SnipeResult:
    username: str
    platform: str
    # available | taken | invalid | blocked | error | unanswered
    status: str
    detail: str = ""
    proxy: str | None = None
    latency_ms: float | None = None
    retry_after: float | None = None
    # Who an *error* is evidence against. Left empty it means "came from a
    # response we could not read", which blames nobody; FAULT_PROXY is set by
    # `transport_error` when the request never got there at all.
    fault: str = ""
    # A free verdict that a second, independent read agreed with. False on
    # everything else, and False on a free name the second read could not check
    # -- an unconfirmed free name is still reported, but never as a sure thing.
    confirmed: bool = False
    # The first read said free and the second said taken. This result is the
    # correction; the site counts it so a run can be honest about its own errors.
    corrected: bool = False

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
        block_memory: float = SNIPE_BLOCK_MEMORY,
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
        # Attempts a proxy carried and how many of them failed. A *rate* decides
        # retirement, not a streak: any answer used to wipe the streak, so a host
        # that answered one request in three kept its record clean forever and a
        # run limped through the whole name list one error at a time. The rate
        # also stops a single unlucky run of misses from writing off a host that
        # is 95% healthy.
        self._attempts: dict[str, int] = {}
        self._failed: dict[str, int] = {}
        # Attempts handed to a proxy that have not reported yet. A proxy is only
        # judged on a *settled* sample: at high concurrency the first completions
        # are whichever requests failed fastest, so a host dropping a third of
        # its tunnels looked like a host failing everything -- 64 checks went out
        # at once, the six fastest answers were all failures, and the pool was
        # written off about 0.05s into a sweep it could have carried. With this,
        # "every attempt failed" means every attempt, not every fast one.
        self._inflight: dict[str, int] = {}
        self._blocks: dict[str, int] = {}
        # A proxy retired mid-run is never handed out again.
        self._retired: set[str] = set()
        self._fail_limit = max(1, fail_limit)
        # Attempts before the failure *rate* alone is allowed to write a host
        # off. Six is enough to see that a host is gone; it is not enough to
        # measure a rate, and a six-attempt sample would write off a host that
        # answers 40% of requests on one unlucky opening.
        self._fail_sample = max(2 * self._fail_limit, 16)
        self._block_limit = max(1, block_limit)
        self._block_memory = max(0.0, block_memory)
        self._block_min = max(1, SNIPE_PLATFORM_BLOCK_MIN)
        self._block_rate = min(1.0, max(0.0, SNIPE_PLATFORM_BLOCK_RATE))
        self._proxy_block_rate = min(1.0, max(0.0, SNIPE_PROXY_BLOCK_RATE))
        # Total refusals each platform has recorded this run, and total answers
        # it gave. The ratio is the evidence a pause turns on: a handful of
        # refusals inside a flood of answers is a transient 401, not a blanket
        # throttle, and pausing on the raw count is what stopped a healthy run.
        self._platform_blocks: dict[str, int] = {}
        self._platform_seen: dict[str, int] = {}
        # Total answers each proxy gave (any outcome). The denominator for the
        # refusal *rate* that decides block-retirement.
        self._seen: dict[str, int] = {}
        self._cursor = 0
        # platform -> {proxy: monotonic time of its last refusal}. A proxy
        # blocked by discord is still a perfectly good proxy for tiktok, and
        # treating those as the same thing stops a healthy run. The timestamp is
        # what makes this correct for a *rotating* endpoint: the exit IP changes
        # between requests, so a refusal is only meaningful for a window, not
        # forever -- see `_refused_now`.
        self._blocked_by: dict[str, dict[str, float]] = {}
        # platform -> monotonic deadline, for a platform that refused the whole
        # list. Sitting one out is what keeps a discord 429 from costing the rest
        # of a multi-platform run.
        self._paused: dict[str, float] = {}
        self.paused_reason: dict[str, str] = {}
        # Jobs dropped because their platform was sitting out. Counted so the
        # summary can say how much of the run a throttle actually cost.
        self.skipped = 0
        # Names the run never got to because the pool had nothing left to hand
        # out. Not errors and not results -- a run does not get to file a
        # verdict, or even a failure, for a name it never asked about -- but the
        # summary has to own them, or a sweep that died at name 6 of 500 looks
        # like it simply finished. See `one()` in the dispatch loop.
        self.misses = 0
        # Why a run ended before its last name, when it did, and the longest
        # retry_after any platform handed us.
        self.stop_reason = ""
        self.retry_after: float | None = None

    def __len__(self) -> int:
        return len(self._all)

    @property
    def per_proxy(self) -> int:
        return self._per_proxy

    def begin_run(self) -> None:
        """Reset per-run bookkeeping, keeping the warm tunnels.

        The pool object is cached across requests precisely so its CONNECT
        tunnels and TLS sessions survive -- one client per proxy is where a
        batch's speed comes from. But one run's verdicts must not carry into the
        next: a proxy retired at the tail of a bad run was still retired at the
        head of a fresh one, so a new list of names answered "the list is dead"
        until the 300-second cache expired. Every count that describes *this*
        run's behaviour resets here; the clients, the list and the connection
        budget stay.
        """
        self._resting.clear()
        self._attempts.clear()
        self._failed.clear()
        self._inflight.clear()
        self._blocks.clear()
        self._blocked_by.clear()
        self._platform_blocks.clear()
        self._platform_seen.clear()
        self._seen.clear()
        self._retired.clear()
        self._paused.clear()
        self.paused_reason.clear()
        self._cursor = 0
        self.skipped = 0
        self.misses = 0
        self.stop_reason = ""
        self.retry_after = None

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
            return self._hand_out(proxy)
        usable = [p for p in self._all if p not in self._retired]
        if not usable:
            return None
        # Every live proxy is resting: use whichever comes back first.
        return self._hand_out(min(usable, key=lambda p: self._resting.get(p, 0.0)))

    def _hand_out(self, proxy: str) -> str:
        """Mark a proxy as carrying a request that has not reported yet."""
        self._inflight[proxy] = self._inflight.get(proxy, 0) + 1
        return proxy

    def abandon(self, proxy: str) -> None:
        """A request that will never report -- the run was stopped mid-flight."""
        if self._inflight.get(proxy):
            self._inflight[proxy] -= 1

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
        self._resting.pop(proxy, None)
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
        # The request is over, whatever it decided. Released before the early
        # returns below, or a proxy judged on work still in flight would never
        # come back to zero and could never be retired at all.
        if self._inflight.get(proxy):
            self._inflight[proxy] -= 1

        if retry_after and (self.retry_after is None or retry_after > self.retry_after):
            self.retry_after = retry_after

        # Every request a platform answered is one data point about how often it
        # refuses. Counted before the early returns below, so an unreadable
        # answer still counts as the platform having answered something.
        if platform:
            self._platform_seen[platform] = self._platform_seen.get(platform, 0) + 1
        self._seen[proxy] = self._seen.get(proxy, 0) + 1

        if outcome == OUTCOME_UNKNOWN:
            # The request arrived and the answer was unusable. Not the tunnel's
            # fault, and not a refusal either, so the proxy's record is left
            # exactly as it was: no cooldown, no strike, no attempt counted.
            return

        if outcome == OUTCOME_TIMEOUT:
            # The tunnel carried this request and the answer never came. The
            # proxy's panel shows the request -- it is not dead -- so this never
            # counts as a strike or a retirement. It is rested briefly so load
            # drifts off a host whose targets are answering slowly, and it is
            # free to be handed out again the moment the cooldown lapses.
            if self._cooldown > 0:
                self._resting[proxy] = time.time() + self._cooldown
            return

        # A *request* the proxy was asked to carry. A platform refusal is not
        # counted: the tunnel worked, the exit IP is what the platform disliked,
        # and that is `blocked`'s own record.
        if outcome != OUTCOME_BLOCKED:
            self._attempts[proxy] = self._attempts.get(proxy, 0) + 1

        if outcome == OUTCOME_OK:
            self._resting.pop(proxy, None)
            self._blocks.pop(proxy, None)
            # Only the platform that just answered stops refusing it. Wiping the
            # record for *every* platform here meant a healthy second platform
            # erased the evidence of a blanket 429: discord blocks both proxies,
            # tiktok answers on both, and the run never notices discord is dead.
            refused = self._blocked_by.get(platform or "")
            if refused is not None:
                refused.pop(proxy, None)
            return

        if outcome == OUTCOME_BLOCKED:
            key = platform or ""
            refused = self._blocked_by.setdefault(key, {})
            refused[proxy] = time.monotonic()
            self._platform_blocks[key] = self._platform_blocks.get(key, 0) + 1
            blocks = self._blocks.get(proxy, 0) + 1
            self._blocks[proxy] = blocks
            seen = self._seen.get(proxy, 0)
            # Retired only when refusals *dominate* what this proxy answered, and
            # never when it is the last live proxy -- retiring the only entry
            # does not replace it with anything, it ends the run. A single
            # rotating endpoint sits at a low refusal rate and is refused in
            # bursts; the raw count of 40 retired it and killed a working sweep.
            # A genuinely blanket-blocked host reads ~100% and is still retired.
            if (
                blocks >= self._block_limit
                and seen > 0
                and blocks / seen >= self._proxy_block_rate
                and self.alive() > 1
                and self._inflight.get(proxy, 0) == 0
            ):
                self._retire(proxy)
            elif self._block_cooldown > 0:
                self._resting[proxy] = time.time() + self._block_cooldown
            return

        self._failed[proxy] = self._failed.get(proxy, 0) + 1
        attempts = self._attempts.get(proxy, 0)
        failed = self._failed[proxy]
        # Two ways off the list, and both are about the *rate* rather than a
        # streak, because a streak is wiped by any answer and a host that answers
        # one request in three never built one -- the run limped through the
        # whole name list one error at a time.
        #
        # A host that is simply gone fails every one of its first
        # SNIPE_PROXY_FAIL_LIMIT attempts, which is the fast path. Everything
        # else is judged on SNIPE_PROXY_FAIL_RATE of at least _fail_sample
        # attempts, so a single unlucky opening cannot write off a host that is
        # mostly working, and a host answering one request in three is kept --
        # it is still producing answers, and ending a sweep is worse than
        # reporting its error rate.
        # Only a settled sample gets a vote: while requests are still out, the
        # outcomes in hand are the fast ones, which are the failures. A truly
        # dead host settles instantly and is retired exactly as quickly as
        # before; a host that is merely slow to answer is not written off for
        # losing a race it never ran.
        settled = self._inflight.get(proxy, 0) == 0
        if settled and (
            (attempts >= self._fail_limit and failed == attempts)
            or (attempts >= self._fail_sample and failed >= attempts * SNIPE_PROXY_FAIL_RATE)
        ):
            self._retire(proxy)
        elif self._cooldown > 0:
            self._resting[proxy] = time.time() + self._cooldown

    def attempts(self) -> int:
        """Requests proxies carried, and how many failed at the transport level.

        The two numbers the summary needs to tell "your list is bad" apart from
        "the names are taken": errors on individual checks say nothing about
        whether the *list* is what is failing.
        """
        return sum(self._attempts.values())

    def failures(self) -> int:
        return sum(self._failed.values())

    def alive(self) -> int:
        """Proxies not written off for the rest of the run.

        O(1) on purpose. This is exactly what ``usable()`` returns, and the
        dispatch loop asks ``usable()`` once per check, so walking the list here
        put the pool's own size on the per-check path: measured at 19.6 us a call
        over a 500-proxy list, which is ~20 minutes of the single-threaded event
        loop spent counting across a 60-million-check sweep. Nothing removes from
        ``_all`` and every retired entry came out of it, so the count is just the
        difference.
        """
        return len(self._all) - len(self._retired)

    def resting(self) -> int:
        now = time.time()
        return sum(1 for until in self._resting.values() if until > now)

    def missed(self) -> int:
        """How many names the pool could not carry at all. Never a result row."""
        return self.misses

    def retired(self) -> int:
        return len(self._retired)

    def _refused_now(self, platform: str) -> set[str]:
        """Proxies this platform has refused *within the memory window*.

        The window is what makes this right for a rotating endpoint: the exit IP
        behind a single proxy URL changes between requests, so a 403 ten minutes
        ago says nothing about the request going out now. Older marks age out,
        and the proxy gets tried again rather than being treated as permanently
        refused.
        """
        marks = self._blocked_by.get(platform)
        if not marks:
            return set()
        if self._block_memory <= 0:
            return set(marks)
        cutoff = time.monotonic() - self._block_memory
        return {proxy for proxy, when in marks.items() if when >= cutoff}

    def blocked(self) -> int:
        """Proxies some platform is refusing right now."""
        seen: set[str] = set()
        for platform in self._blocked_by:
            seen |= self._refused_now(platform)
        return len(seen)

    def blocked_by(self) -> dict[str, int]:
        """How many proxies each platform is refusing right now."""
        counts = {name: len(self._refused_now(name)) for name in self._blocked_by}
        return {name: n for name, n in counts.items() if n}

    def all_blocked(self, platforms: Iterable[str]) -> bool:
        """Has every proxy *still alive* been refused by every platform in play?

        Read against the fresh-refusal window, so a rotating endpoint's stale
        403s do not count. It has to be per platform: asking "has anything
        refused these proxies" makes a discord 429 look like a dead list, and a
        three-platform run then stops before tiktok -- which was answering fine
        -- is ever asked.
        """
        if not self._all:
            return False
        # Only proxies still in the run count. A proxy that died at the
        # transport level never appears in any platform's refused set, so
        # counting every proxy ever listed meant a blanket rate limit on the
        # survivors was never noticed.
        live = [p for p in self._all if p not in self._retired]
        if not live:
            return False
        wanted = list(platforms) or list(self._blocked_by)
        if not wanted:
            return False
        return all(
            all(proxy in self._refused_now(one) for proxy in live)
            for one in wanted
        )

    def shortest_pause(self) -> float:
        """Seconds until the soonest-expiring platform pause lifts (0 if none)."""
        now = time.monotonic()
        left = [until - now for until in self._paused.values() if until > now]
        return max(0.0, min(left)) if left else 0.0

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
        # Evidence resets with the pause, so a fresh set of attempts decides the
        # next pause instead of the strikes left over from before.
        self._platform_blocks.pop(platform, None)
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
            refused = self._refused_now(platform)
            # This is asked twice per dispatch iteration. Comparing counts is
            # O(1) where ``all_blocked`` is O(list) -- with a large list and a
            # handful of platforms that walk was the loop's own ceiling. Only
            # when a platform has refused at least as many distinct proxies as
            # are still alive is there any chance the whole pool is blocked.
            if len(refused) < self.alive():
                continue
            # Real evidence, not one unlucky answer. A single rotating endpoint
            # is one proxy, so the count test above passes after a single 403 --
            # which is exactly how one refusal used to end a run. A pause now
            # needs at least `_block_min` refusals, and at least one per live
            # proxy, before it fires.
            evidence = max(self.alive(), self._block_min)
            blocked = self._platform_blocks.get(platform, 0)
            if blocked < evidence:
                continue
            # ...and refusals must dominate what this platform has answered. At
            # a 4% refusal rate (measured live on guns.lol) a pause is noise;
            # at 100% it is a real throttle and the pause is exactly right.
            seen = self._platform_seen.get(platform, 0)
            if seen and blocked / seen < self._block_rate:
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
        """Proxies a check can be dispatched to right now.

        A cooling-down proxy still counts, because ``next()`` hands one out
        anyway: with everything resting it returns whichever comes back first.
        So "nothing usable" has to mean "nothing left", or the two disagree and
        the run stalls on a pool it could still use. That is exactly what a
        60-second cooldown after one transport error did with a single proxy --
        dispatch stopped dead, and a host that answered two requests in three
        looked like a dead list. Whether a proxy is finished is decided in
        ``report()``, on its failure rate, not on a timer.
        """
        return self.alive()

    def cooling(self) -> int:
        """Live proxies currently sitting out a short cooldown.

        Read off the resting map rather than the whole list: this is asked once
        per dispatch iteration, and ``_resting`` only ever holds proxies that
        recently errored, while ``_all`` holds the entire list. Scanning the
        former is O(cooling); scanning the latter was O(list) *per iteration*,
        which is tens of millions of operations across a wide run. Entries are
        popped on recovery and on retirement, so nothing stale is counted.
        """
        now = time.time()
        return sum(1 for until in self._resting.values() if until > now)

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
            if self._blocks:
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
        """Why a run stopped with every platform sitting out.

        Only one situation reaches this now: every platform the run is using has
        refused every proxy in the list. Proxies resting on their own transport
        errors used to land here too, and that is the sentence that was wrong --
        it told the user their list was exhausted when the list was merely flaky.
        """
        counts = self.blocked_by()
        if not counts:
            return "every platform sat a run out with no reason recorded"
        platform = max(counts, key=lambda k: counts[k])
        extra = f" (it asked for {self.retry_after:.0f}s)" if self.retry_after else ""
        return (
            f"{platform} refused every proxy in the list, so it sat out. That is a "
            f"platform-wide throttle, not a dead list -- proxies were only rested "
            f"{int(self._block_cooldown)}s each{extra}. A bigger pool, fewer simultaneous "
            "checks, or a different exit range is what fixes it."
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
        return transport_error(username, "discord", exc)

    if resp.status_code == 200:
        try:
            taken = bool(resp.json().get("taken"))
        except ValueError:
            return SnipeResult(username, "discord", "error", "unreadable body")
        return SnipeResult(username, "discord", "taken" if taken else "available")
    if resp.status_code == 429:
        wait = retry_after_seconds(_json_or_none(resp), resp)
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
    """Read a verdict off the page, or None for "unknown".

    Order matters, and it is *taken first*. The site renders the same default
    title for a free name and for a banned one, so the title alone cannot tell
    them apart -- the old test compared the title to the default and called the
    page free, which is how a banned profile came back "available" and then
    failed to register. So every signal that says the handle is held is checked
    before the claim offer: the ban heading first, because a banned name is the
    one case where the page looks untouched *and* cannot be registered.
    """
    if any(marker in body for marker in GUNS_BANNED_MARKERS):
        return SnipeResult(username, "guns.lol", STATUS_BANNED, "account is banned")
    if GUNS_PROFILE_MARKER in body:
        found = GUNS_IDENTIFIER_RE.search(body.decode("utf-8", errors="ignore"))
        return SnipeResult(
            username, "guns.lol", "taken", f"@{found.group(1)}" if found else "live profile"
        )
    # A registered account whose public profile is gone (banned, hidden) still
    # carries its own payload. Its page renders the default title, so this is
    # the only thing that separates it from a genuinely free name.
    if GUNS_ACCOUNT_MARKER in body:
        return SnipeResult(username, "guns.lol", "taken", "registered account (no public profile)")
    # Free: the page is offering to claim *this* name. That offer is the signal,
    # not the title it shares with a banned profile's page.
    if GUNS_CLAIM + username.encode() in body:
        return SnipeResult(username, "guns.lol", "available", "claimable")
    if b"</title>" not in body:
        return None
    match = TITLE_RE.search(body.decode("utf-8", errors="ignore"))
    if not match:
        return None
    title = match.group(1).strip()
    if GUNS_TITLE_RE.match(title):
        return SnipeResult(username, "guns.lol", "taken", title)
    # A title we do not recognise: no verdict from this page.
    return None


def _guns_challenge(body: bytes) -> bool:
    """Is this the site's clearance interstitial rather than a profile page?"""
    return any(marker in body for marker in GUNS_CHALLENGE_MARKERS)


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
        return transport_error(username, "guns.lol", exc)

    if resp.status_code == 429:
        return _blocked(username, "guns.lol", resp)
    if resp.status_code in (401, 403, 503):
        # A challenge or a gateway refusal, which is what this site answers a
        # datacenter IP with. Reported as a block rather than an error so the
        # platform is paused once it has refused the whole list, instead of the
        # run spending every name on a page that never carries a verdict.
        note = "clearance check" if _guns_challenge(resp.content) else f"status {resp.status_code}"
        return _blocked(username, "guns.lol", resp, note=note)
    if resp.status_code == 404:
        # A free name is a 200 page that offers to claim it. The site answers
        # 404 for its own reserved routes (``/api`` returns ``{"error":"Not
        # found"}``) and for anything the app does not serve, so a 404 is not a
        # free name -- it is a name that cannot be claimed here.
        return SnipeResult(username, "guns.lol", "invalid", "reserved or unserved route")
    if resp.status_code != 200:
        return SnipeResult(username, "guns.lol", "error", f"status {resp.status_code}")

    # The clearance page served under load, with a 200 on it. It is not a profile
    # and it is certainly not a free name, so it is refused rather than read.
    if _guns_challenge(resp.content):
        return _blocked(username, "guns.lol", resp, note="clearance check")

    # The account payload of a banned profile sits past the old 16 KB window;
    # the body is already downloaded, so the wider slice costs a search, not a
    # transfer.
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
        return transport_error(username, "instagram", exc)

    if resp.status_code == 404:
        return SnipeResult(username, "instagram", "available", "404")
    if resp.status_code == 429:
        wait = retry_after_seconds(_json_or_none(resp), resp)
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
def _tiktok_page_verdict(username: str, body: bytes) -> SnipeResult | None:
    """Read the profile page's own verdict, or None when it carries none.

    TikTok's profile page ships its state as JSON inside
    ``webapp.user-detail``: a live account puts a ``userInfo`` block there, and a
    handle with no account at all gets ``"statusCode":10221``. The block is a
    plain string search rather than a parser, because the page is 166-202 KB of
    markup around it and only this one scope decides anything.
    """
    text = body.decode("utf-8", errors="ignore")
    start = text.find(TIKTOK_USER_DETAIL)
    if start < 0:
        return None
    scope = text[start : start + TIKTOK_SCOPE_CHARS]
    found = TIKTOK_UNIQUE_ID_RE.search(scope)
    if found:
        # The account exists. Its status code may be 10222 `ErrBizUserSecret`
        # -- a profile that is not public, which is exactly the case oEmbed
        # reports as missing -- but a handle is never released, so it is taken.
        handle = found.group(1)
        return SnipeResult(
            username, "tiktok", "taken", f"@{handle}" if handle != username else "profile exists"
        )
    if TIKTOK_NO_USER in scope:
        return SnipeResult(username, "tiktok", "available", "no such user")
    return None


async def _tiktok_profile(client: httpx.AsyncClient, username: str) -> SnipeResult:
    """Ask the profile page, which is the only source that sees a hidden account.

    Requested with a mobile Safari user agent on purpose: the desktop page is a
    1.5 KB "enable JavaScript" shell to a datacenter IP, for a live handle and a
    free name alike, so reading it would answer nothing. Anything the page does
    not decide -- the shell, an interstitial, a status we do not read -- comes
    back as an error, which the pool retries and never files as "available".
    """
    headers = {"User-Agent": TIKTOK_IPHONE_UA, "Accept": "text/html"}
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT, read=max(READ_TIMEOUT, TIKTOK_PAGE_TIMEOUT),
        write=READ_TIMEOUT, pool=READ_TIMEOUT,
    )
    try:
        resp = await client.get(
            TIKTOK_PROFILE.format(handle=username), headers=headers, timeout=timeout
        )
    except Exception as exc:
        return transport_error(username, "tiktok", exc)

    if resp.status_code in (403, 429, 503):
        # 503 here is TikTok's own edge page (`overload-protect triggered`, from
        # its load balancer) rather than a gateway in front of the proxy -- a
        # refusal by the platform, which rests the exit and counts towards the
        # platform pause, instead of an error that blames nobody and retries into
        # the same throttle.
        wait = retry_after_seconds(_json_or_none(resp), resp)
        detail = f"status {resp.status_code}" + (f", retry_after {wait:.0f}s" if wait else "")
        return SnipeResult(username, "tiktok", "blocked", detail, retry_after=wait)
    if resp.status_code != 200:
        return SnipeResult(username, "tiktok", "error", f"status {resp.status_code}")
    verdict = _tiktok_page_verdict(username, resp.content)
    if verdict is not None:
        return verdict
    return SnipeResult(username, "tiktok", "error", "no profile data in page")


async def _tiktok_oembed(
    client: httpx.AsyncClient, username: str, handle: str
) -> SnipeResult | None:
    """The 45-byte embed. ``None`` means "no such user", which is not a verdict.

    A 200 is a whole answer -- the handle is live -- and it is asked about the
    lower-cased handle, because the embed only resolves the canonical spelling.
    The 400 is returned as ``None`` rather than as `available`: it covers a name
    nobody holds *and* a live handle the embed will not resolve, and on a random
    4-character sample 7 of the 9 names it called missing were real accounts. It
    is the cheapest source here and the one TikTok throttles hardest -- measured
    through one rotating endpoint at 16 concurrent: 56 of 80 calls came back
    429/503 while the profile page answered 80 of 80 in the same window.
    """
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*"}
    try:
        resp = await client.get(
            TIKTOK_OEMBED,
            params={"url": f"https://www.tiktok.com/@{handle}"},
            headers=headers,
        )
    except Exception as exc:
        return transport_error(username, "tiktok", exc)

    body = _json_or_none(resp)

    if resp.status_code == 200:
        if not isinstance(body, dict):
            return SnipeResult(username, "tiktok", "error", "unreadable body")
        author = str(body.get("author_name") or "")
        if not author:
            return SnipeResult(username, "tiktok", "error", "no author_name in oembed body")
        # The embed names the canonical handle even when the request did not,
        # so a name typed in a different case comes back with the handle that
        # actually holds it rather than a bare "taken".
        found = TIKTOK_HANDLE_RE.search(str(body.get("author_url") or ""))
        canonical = found.group(1) if found else handle
        return SnipeResult(
            username, "tiktok", "taken", f"@{canonical}" if canonical != username else author
        )
    if resp.status_code == 400:
        if not isinstance(body, dict) or body.get("author_name") or body.get("html"):
            # A 400 carrying more than the "no user" body is a different failure
            # wearing the same status, and guessing "free" from it is exactly the
            # false positive this check used to produce.
            return SnipeResult(username, "tiktok", "error", "unexpected 400 body")
        return None
    if resp.status_code in (403, 429, 503):
        wait = retry_after_seconds(body, resp)
        detail = f"status {resp.status_code}" + (f", retry_after {wait:.0f}s" if wait else "")
        return SnipeResult(username, "tiktok", "blocked", detail, retry_after=wait)
    return SnipeResult(username, "tiktok", "error", f"status {resp.status_code}")


async def check_tiktok(client: httpx.AsyncClient, username: str) -> SnipeResult:
    """The profile page leads; the embed is the cheap fallback, not the base.

    The page is authoritative *and* the endpoint TikTok leaves alone: measured
    through one rotating endpoint at 16 concurrent, 80 of 80 page reads came back
    with a verdict while 56 of the same 80 oEmbed calls came back 429/503. It
    also answers in one request, where the embed-first order paid a round trip on
    every name -- 6.9 names/s against 3.6/s. Its price is bytes (~50 KB against
    45), so `TIKTOK_PAGE_FIRST=false` restores the cheap order for a run that
    cares more about the transfer than the answer rate.

    Case is the other reason the page leads: a handle is case-insensitive for
    uniqueness but the embed only resolves the canonical all-lowercase spelling,
    so ``@Nike`` answers 400 with the same body a free name gets, while the page
    resolves any case to the account that holds it.

    A 400 from the embed is never "available" on its own. Everything else -- a
    403, a 429, an HTML body, an empty one -- is a block or an error.
    """
    handle = username.lower()

    if TIKTOK_PAGE_FIRST:
        page = await _tiktok_profile(client, handle)
        if page.status in FINAL_STATUSES or page.status == "blocked":
            return page

    result = await _tiktok_oembed(client, username, handle)
    if result is not None:
        return result
    if TIKTOK_PAGE_FIRST:
        # The page was asked first and had nothing to say -- it served the shell
        # or an interstitial. A 400 from the embed is not evidence about the
        # name, so this is unanswered rather than available, and the pool
        # retries it.
        return SnipeResult(username, "tiktok", "error", "oembed 400, page had no verdict")
    return await _tiktok_profile(client, handle)


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
    """``(status, exception)`` -- send the request, read only the status line."""
    try:
        async with client.stream("GET", url, headers=headers) as resp:
            return resp.status_code, None
    except Exception as exc:
        return None, exc


def _blocked(
    username: str, platform: str, resp: httpx.Response, note: str = "rate limited"
) -> SnipeResult:
    """A platform refusal, which is not the proxy's fault and is not a verdict."""
    wait = retry_after_seconds(_json_or_none(resp), resp)
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
        return transport_error(username, "roblox", exc)

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
        return transport_error(username, "minecraft", exc)

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
        return transport_error(username, "github", exc)

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
        return transport_error(username, "telegram", exc)

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
        return transport_error(username, "youtube", err)
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
        return transport_error(username, "x", err)
    if code == 200:
        return SnipeResult(username, "x", "taken", "profile exists")
    if code == 404:
        return SnipeResult(username, "x", "available", "404")
    if code in (401, 403, 429):
        return SnipeResult(username, "x", "blocked", f"status {code}")
    return SnipeResult(username, "x", "error", f"status {code}")



# --- Bluesky ---------------------------------------------------------------
# resolveHandle answers a DID for a handle that exists and an InvalidRequest for
# one that does not -- 42 bytes against 63, measured. Only the default
# bsky.social namespace can be asked this way, and that is exactly the namespace
# a new account is given, so the name being checked is the local part.
BSKY_RESOLVE = "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={}.bsky.social"


async def check_bluesky(client: httpx.AsyncClient, username: str) -> SnipeResult:
    handle = username.lower()
    try:
        resp = await client.get(
            BSKY_RESOLVE.format(handle),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "bluesky", exc)

    if resp.status_code == 200:
        body = _json_or_none(resp)
        did = str((body or {}).get("did") or "") if isinstance(body, dict) else ""
        if did:
            return SnipeResult(username, "bluesky", "taken", did)
        return SnipeResult(username, "bluesky", "error", "no did in body")
    if resp.status_code == 400:
        # InvalidRequest: nothing holds that handle in the bsky.social namespace.
        # Only read as *free* because the name was already lower-cased and has to
        # pass the namespace rule to get here -- a syntax failure would otherwise
        # look identical.
        return SnipeResult(username, "bluesky", "available", "handle is unclaimed")
    if resp.status_code in (403, 429):
        return _blocked(username, "bluesky", resp)
    return SnipeResult(username, "bluesky", "error", f"status {resp.status_code}")


# --- Kick ------------------------------------------------------------------
# The same JSON the site's own channel page loads. 200 carries the channel,
# 404 is `{"error":"Not Found","message":"Channel not found."}`.
KICK_CHANNEL = "https://kick.com/api/v2/channels/{}"


async def check_kick(client: httpx.AsyncClient, username: str) -> SnipeResult:
    handle = username.lower()
    try:
        resp = await client.get(
            KICK_CHANNEL.format(handle),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "kick", exc)

    if resp.status_code == 200:
        body = _json_or_none(resp)
        slug = str((body or {}).get("slug") or "") if isinstance(body, dict) else ""
        if slug:
            return SnipeResult(username, "kick", "taken", f"@{slug}")
        return SnipeResult(username, "kick", "error", "no channel in body")
    if resp.status_code == 404:
        return SnipeResult(username, "kick", "available", "no channel")
    if resp.status_code in (403, 429):
        return _blocked(username, "kick", resp)
    return SnipeResult(username, "kick", "error", f"status {resp.status_code}")


# --- Steam -----------------------------------------------------------------
# The vanity URL's own XML view: a live profile renders <steamID64>, a free
# vanity renders <error> instead -- 6.3 KB against 144 bytes, measured. Cheap
# enough that the whole page is not worth reading.
STEAM_XML = "https://steamcommunity.com/id/{}/?xml=1"


async def check_steam(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            STEAM_XML.format(username),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/xml, text/xml"},
        )
    except Exception as exc:
        return transport_error(username, "steam", exc)

    if resp.status_code in (403, 429):
        return _blocked(username, "steam", resp)
    if resp.status_code != 200:
        return SnipeResult(username, "steam", "error", f"status {resp.status_code}")
    body = resp.content
    if b"<steamID64>" in body:
        match = re.search(rb"<steamID>([^<]{1,64})</steamID>", body)
        detail = match.group(1).decode("utf-8", "ignore") if match else "profile exists"
        return SnipeResult(username, "steam", "taken", detail)
    if b"<error>" in body:
        return SnipeResult(username, "steam", "available", "no such vanity url")
    return SnipeResult(username, "steam", "error", "unrecognised xml")


# --- SoundCloud ------------------------------------------------------------
# A live profile is a 200; an unclaimed path is a 404. Both pages are large, so
# only the status is read.
async def check_soundcloud(client: httpx.AsyncClient, username: str) -> SnipeResult:
    code, err = await _status_only(
        client, f"https://soundcloud.com/{username}", {"User-Agent": BROWSER_UA, "Accept": "text/html"}
    )
    if err:
        return transport_error(username, "soundcloud", err)
    if code == 200:
        return SnipeResult(username, "soundcloud", "taken", "profile exists")
    if code == 404:
        return SnipeResult(username, "soundcloud", "available", "404")
    if code in (401, 403, 429):
        return SnipeResult(username, "soundcloud", "blocked", f"status {code}")
    return SnipeResult(username, "soundcloud", "error", f"status {code}")


# --- Dribbble --------------------------------------------------------------
async def check_dribbble(client: httpx.AsyncClient, username: str) -> SnipeResult:
    handle = username.lower()
    code, err = await _status_only(
        client, f"https://dribbble.com/{handle}", {"User-Agent": BROWSER_UA, "Accept": "text/html"}
    )
    if err:
        return transport_error(username, "dribbble", err)
    if code == 200:
        return SnipeResult(username, "dribbble", "taken", f"@{handle}")
    if code == 404:
        return SnipeResult(username, "dribbble", "available", "404")
    if code in (401, 403, 429):
        return SnipeResult(username, "dribbble", "blocked", f"status {code}")
    return SnipeResult(username, "dribbble", "error", f"status {code}")


# --- Chess.com -------------------------------------------------------------
# Its public player API: 200 carries the player, 404 is
# `{"code":0,"message":"not found"}` -- 528 bytes, measured.
CHESS_PLAYER = "https://api.chess.com/pub/player/{}"


async def check_chess(client: httpx.AsyncClient, username: str) -> SnipeResult:
    handle = username.lower()
    try:
        resp = await client.get(
            CHESS_PLAYER.format(handle),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "chess.com", exc)

    if resp.status_code == 200:
        body = _json_or_none(resp)
        if isinstance(body, dict) and body.get("username"):
            return SnipeResult(username, "chess.com", "taken", f"@{body['username']}")
        return SnipeResult(username, "chess.com", "error", "no player in body")
    if resp.status_code == 404:
        return SnipeResult(username, "chess.com", "available", "no player")
    if resp.status_code in (403, 429):
        return _blocked(username, "chess.com", resp)
    return SnipeResult(username, "chess.com", "error", f"status {resp.status_code}")


# --- Duolingo --------------------------------------------------------------
# An empty user list is the free answer: 12 bytes of `{"users":[]}` against a
# ~1.9 KB profile for a name that exists.
DUO_USERS = "https://www.duolingo.com/2017-06-30/users?username={}"


async def check_duolingo(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            DUO_USERS.format(username),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "duolingo", exc)

    if resp.status_code in (403, 429):
        return _blocked(username, "duolingo", resp)
    if resp.status_code != 200:
        return SnipeResult(username, "duolingo", "error", f"status {resp.status_code}")
    body = _json_or_none(resp)
    users = body.get("users") if isinstance(body, dict) else None
    if users is None:
        return SnipeResult(username, "duolingo", "error", "no users field in body")
    if users:
        return SnipeResult(username, "duolingo", "taken", str(users[0].get("username") or "profile exists"))
    return SnipeResult(username, "duolingo", "available", "no such user")


# --- Wattpad ---------------------------------------------------------------
# A public user API: 200 with the account, 400 with `not found` for a free name.
WATTPAD_USER = "https://www.wattpad.com/api/v3/users/{}"


async def check_wattpad(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            WATTPAD_USER.format(username),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "wattpad", exc)

    if resp.status_code == 200:
        body = _json_or_none(resp)
        # `username` is the handle; `name` is the display name and is often empty
        # even on an account with thousands of stories.
        handle = str((body or {}).get("username") or "") if isinstance(body, dict) else ""
        if handle:
            return SnipeResult(username, "wattpad", "taken", handle)
        return SnipeResult(username, "wattpad", "error", "no account in body")
    if resp.status_code == 400:
        body = _json_or_none(resp)
        message = str((body or {}).get("message") or "") if isinstance(body, dict) else ""
        if "not found" in message.lower():
            return SnipeResult(username, "wattpad", "available", "no such user")
        return SnipeResult(username, "wattpad", "error", "unexpected 400 body")
    if resp.status_code in (403, 429):
        return _blocked(username, "wattpad", resp)
    return SnipeResult(username, "wattpad", "error", f"status {resp.status_code}")


# --- Disqus ----------------------------------------------------------------
async def check_disqus(client: httpx.AsyncClient, username: str) -> SnipeResult:
    handle = username.lower()
    code, err = await _status_only(
        client, f"https://disqus.com/by/{handle}/", {"User-Agent": BROWSER_UA, "Accept": "text/html"}
    )
    if err:
        return transport_error(username, "disqus", err)
    if code == 200:
        return SnipeResult(username, "disqus", "taken", f"@{handle}")
    if code == 404:
        return SnipeResult(username, "disqus", "available", "404")
    if code in (401, 403, 429):
        return SnipeResult(username, "disqus", "blocked", f"status {code}")
    return SnipeResult(username, "disqus", "error", f"status {code}")


# --- VK --------------------------------------------------------------------
async def check_vk(client: httpx.AsyncClient, username: str) -> SnipeResult:
    code, err = await _status_only(
        client, f"https://vk.com/{username}", {"User-Agent": BROWSER_UA, "Accept": "text/html"}
    )
    if err:
        return transport_error(username, "vk", err)
    if code == 200:
        return SnipeResult(username, "vk", "taken", "profile exists")
    if code == 404:
        return SnipeResult(username, "vk", "available", "404")
    if code in (401, 403, 429):
        return SnipeResult(username, "vk", "blocked", f"status {code}")
    return SnipeResult(username, "vk", "error", f"status {code}")



# --- Docker Hub ------------------------------------------------------------
# The v2 user endpoint answers a small JSON object for an account and a 404 for
# nothing -- the whole verdict is the status, with no body marker to misread.
DOCKERHUB_USER = "https://hub.docker.com/v2/users/{}/"


async def check_dockerhub(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            DOCKERHUB_USER.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "dockerhub", exc)
    if resp.status_code == 200:
        body = _json_or_none(resp)
        uid = body.get("username") if isinstance(body, dict) else None
        return SnipeResult(username, "dockerhub", "taken", "@" + str(uid) if uid else "account exists")
    if resp.status_code == 404:
        return SnipeResult(username, "dockerhub", "available", "no such account")
    if resp.status_code in (403, 429):
        return _blocked(username, "dockerhub", resp)
    return SnipeResult(username, "dockerhub", "error", "status {}".format(resp.status_code))


# --- Codeberg --------------------------------------------------------------
# Gitea's own API. 404 is authoritative for "no such user"; anything else that
# is not a 200 is left unanswered rather than guessed at.
CODEBERG_USER = "https://codeberg.org/api/v1/users/{}"


async def check_codeberg(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            CODEBERG_USER.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "codeberg", exc)
    if resp.status_code == 200:
        body = _json_or_none(resp)
        login = body.get("login") if isinstance(body, dict) else None
        return SnipeResult(username, "codeberg", "taken", "@" + str(login) if login else "account exists")
    if resp.status_code == 404:
        return SnipeResult(username, "codeberg", "available", "no such account")
    if resp.status_code in (403, 429):
        return _blocked(username, "codeberg", resp)
    return SnipeResult(username, "codeberg", "error", "status {}".format(resp.status_code))


# --- Mastodon (mastodon.social) -------------------------------------------
# The lookup endpoint resolves one acct on one instance: 200 an account, 404
# nothing. Handles are case-insensitive on the fediverse, so it is asked lower.
MASTODON_LOOKUP = "https://mastodon.social/api/v1/accounts/lookup?acct={}"


async def check_mastodon(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            MASTODON_LOOKUP.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "mastodon", exc)
    if resp.status_code == 200:
        body = _json_or_none(resp)
        acct = body.get("acct") if isinstance(body, dict) else None
        return SnipeResult(username, "mastodon", "taken", "@" + str(acct) if acct else "account exists")
    if resp.status_code == 404:
        return SnipeResult(username, "mastodon", "available", "no such account")
    if resp.status_code in (403, 429):
        return _blocked(username, "mastodon", resp)
    return SnipeResult(username, "mastodon", "error", "status {}".format(resp.status_code))


# --- DEV Community ---------------------------------------------------------
# The by-username lookup returns the user object or a 404. Two paths, one
# verdict each.
DEVTO_USER = "https://dev.to/api/users/by_username?url={}"


async def check_devto(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            DEVTO_USER.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "dev.to", exc)
    if resp.status_code == 200:
        body = _json_or_none(resp)
        shown = body.get("username") if isinstance(body, dict) else None
        return SnipeResult(username, "dev.to", "taken", "@" + str(shown) if shown else "account exists")
    if resp.status_code == 404:
        return SnipeResult(username, "dev.to", "available", "no such account")
    if resp.status_code in (403, 429):
        return _blocked(username, "dev.to", resp)
    return SnipeResult(username, "dev.to", "error", "status {}".format(resp.status_code))


# --- Lichess ---------------------------------------------------------------
# One JSON object per player, a 404 for a handle nobody holds.
LICHESS_USER = "https://lichess.org/api/user/{}"


async def check_lichess(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            LICHESS_USER.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "lichess", exc)
    if resp.status_code == 200:
        body = _json_or_none(resp)
        name = body.get("username") if isinstance(body, dict) else None
        return SnipeResult(username, "lichess", "taken", "@" + str(name) if name else "player exists")
    if resp.status_code == 404:
        return SnipeResult(username, "lichess", "available", "no such player")
    if resp.status_code in (403, 429):
        return _blocked(username, "lichess", resp)
    return SnipeResult(username, "lichess", "error", "status {}".format(resp.status_code))


# --- Hugging Face ----------------------------------------------------------
# The user overview endpoint: a JSON object for an account, 404 for nothing.
HUGGINGFACE_USER = "https://huggingface.co/api/users/{}/overview"


async def check_huggingface(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            HUGGINGFACE_USER.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "huggingface", exc)
    if resp.status_code == 200:
        body = _json_or_none(resp)
        name = body.get("user") or body.get("name") if isinstance(body, dict) else None
        return SnipeResult(username, "huggingface", "taken", "@" + str(name) if name else "account exists")
    if resp.status_code == 404:
        return SnipeResult(username, "huggingface", "available", "no such account")
    if resp.status_code in (403, 429):
        return _blocked(username, "huggingface", resp)
    return SnipeResult(username, "huggingface", "error", "status {}".format(resp.status_code))


# --- GitLab ----------------------------------------------------------------
# The users search returns a JSON array: one entry for a handle that exists, an
# empty array for one that does not. Read as JSON rather than by size, so the
# verdict is the shape of the answer and not a byte count.
GITLAB_USERS = "https://gitlab.com/api/v4/users?username={}"


async def check_gitlab(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            GITLAB_USERS.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        )
    except Exception as exc:
        return transport_error(username, "gitlab", exc)
    if resp.status_code == 200:
        body = _json_or_none(resp)
        if not isinstance(body, list):
            return SnipeResult(username, "gitlab", "unanswered", "not a user list")
        if not body:
            return SnipeResult(username, "gitlab", "available", "no such account")
        first = body[0] if isinstance(body[0], dict) else {}
        return SnipeResult(username, "gitlab", "taken", "@" + str(first.get("username") or username))
    if resp.status_code in (403, 429):
        return _blocked(username, "gitlab", resp)
    return SnipeResult(username, "gitlab", "error", "status {}".format(resp.status_code))


# --- itch.io ---------------------------------------------------------------
# A subdomain per creator: the profile renders, or the site's own 404 does.
ITCH_PROFILE = "https://{}.itch.io/"


async def check_itch(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            ITCH_PROFILE.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
        )
    except Exception as exc:
        return transport_error(username, "itch.io", exc)
    if resp.status_code == 200:
        return SnipeResult(username, "itch.io", "taken", "profile page")
    if resp.status_code == 404:
        return SnipeResult(username, "itch.io", "available", "no such creator")
    if resp.status_code in (403, 429):
        return _blocked(username, "itch.io", resp)
    return SnipeResult(username, "itch.io", "error", "status {}".format(resp.status_code))


# --- Gumroad ---------------------------------------------------------------
# Same shape as itch: a subdomain that either renders the shop or 404s.
GUMROAD_SHOP = "https://{}.gumroad.com"


async def check_gumroad(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            GUMROAD_SHOP.format(username.lower()),
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
        )
    except Exception as exc:
        return transport_error(username, "gumroad", exc)
    if resp.status_code == 200:
        return SnipeResult(username, "gumroad", "taken", "shop live")
    if resp.status_code == 404:
        return SnipeResult(username, "gumroad", "available", "no such shop")
    if resp.status_code in (403, 429):
        return _blocked(username, "gumroad", resp)
    return SnipeResult(username, "gumroad", "error", "status {}".format(resp.status_code))


# --- MyAnimeList -----------------------------------------------------------
# Profile pages: a live profile is a 200, a free name the site's own 404.
MAL_PROFILE = "https://myanimelist.net/profile/{}"


async def check_myanimelist(client: httpx.AsyncClient, username: str) -> SnipeResult:
    try:
        resp = await client.get(
            MAL_PROFILE.format(username),
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
        )
    except Exception as exc:
        return transport_error(username, "myanimelist", exc)
    if resp.status_code == 200:
        return SnipeResult(username, "myanimelist", "taken", "profile page")
    if resp.status_code == 404:
        return SnipeResult(username, "myanimelist", "available", "no such user")
    if resp.status_code in (403, 429):
        return _blocked(username, "myanimelist", resp)
    return SnipeResult(username, "myanimelist", "error", "status {}".format(resp.status_code))

# One line per platform on how its verdict is reached, served by /info and shown
# on the Platforms page. A checker that cannot explain itself in a sentence is a
# checker nobody can audit.
CHECK_NOTES: dict[str, str] = {
    "discord": "username lookup endpoint; 200 taken, 404 free, 429 with retry_after honoured",
    "roblox": "username-to-id lookup; a returned id is taken, an empty list free",
    "minecraft": "Mojang name lookup; 200 taken, 404 free",
    "telegram": "t.me page; a live handle renders tgme_page_title, a free one the generic page",
    "instagram": "web_profile_info; an account returns data, an unknown name a 404",
    "tiktok": "profile page with a mobile Safari agent; the user-detail payload carries the verdict, oEmbed is only a fallback and a 400 is never a verdict",
    "x": "account lookup; a missing or suspended account reads free",
    "github": "api.github.com/users; 200 taken, 404 free (GITHUB_TOKEN raises the hourly limit)",
    "youtube": "channel handle page; the @handle resolves only when it is held",
    "guns.lol": "profile page; a ban heading is its own verdict and the clearance interstitial is a refusal, never a free name",
    "bluesky": "resolveHandle; a DID for a handle that exists, InvalidRequest for one that does not",
    "kick": "api/v2/channels; 200 the channel, 404 not found",
    "steam": "vanity URL XML; <steamID64> live, <error> free",
    "soundcloud": "profile page; a live permalink renders, an unknown one 404s",
    "dribbble": "profile API; 200 taken, 404 free",
    "chess.com": "pub/player; 200 the player, 404 not found",
    "duolingo": "users?username=; an empty list means free",
    "wattpad": "user page; 200 taken, 404 free",
    "disqus": "profile page; 200 taken, 404 free",
    "vk": "profile page; a held short address renders, an unclaimed one does not",
    "dockerhub": "v2 user API; 200 the account, 404 nothing",
    "codeberg": "Gitea users API; 200 the account, 404 nothing",
    "mastodon": "account lookup on mastodon.social; 200 the account, 404 nothing",
    "dev.to": "by-username lookup; 200 the user, 404 nothing",
    "lichess": "user API; 200 the player, 404 nothing",
    "huggingface": "user overview API; 200 the account, 404 nothing",
    "gitlab": "users search; an array with an entry is taken, an empty array is free",
    "itch.io": "creator subdomain; the profile renders, or the site's own 404 does",
    "gumroad": "shop subdomain; 200 live, 404 free",
    "myanimelist": "profile page; 200 taken, 404 free",
}


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
    "bluesky": check_bluesky,
    "kick": check_kick,
    "steam": check_steam,
    "soundcloud": check_soundcloud,
    "dribbble": check_dribbble,
    "chess.com": check_chess,
    "duolingo": check_duolingo,
    "wattpad": check_wattpad,
    "disqus": check_disqus,
    "vk": check_vk,
    "dockerhub": check_dockerhub,
    "codeberg": check_codeberg,
    "mastodon": check_mastodon,
    "dev.to": check_devto,
    "lichess": check_lichess,
    "huggingface": check_huggingface,
    "gitlab": check_gitlab,
    "itch.io": check_itch,
    "gumroad": check_gumroad,
    "myanimelist": check_myanimelist,
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


class PlatformPacer:
    """How much of each platform is allowed in flight, adjusted from its answers.

    A run's concurrency is a *global* budget and a throttle is *per platform*.
    Throwing the whole window at a host that is refusing a third of what it
    answers is what turns a throttled platform into a column of `blocked` rows
    and, once the refusal rate crosses the pause threshold, a 90-second sit-out
    where nothing at all is asked. Pacing the platform keeps it producing
    verdicts at whatever rate it will accept instead.

    The rule is the usual one for a signal you cannot see directly: cut hard
    when the refusals arrive, grow back one at a time when they stop. Every
    adjustment is recorded with the platform and the numbers behind it, so a run
    can say which platform it slowed down and why rather than just looking
    slow.
    """

    def __init__(self, platforms: Iterable[str], ceiling: int) -> None:
        self.ceiling = max(1, int(ceiling))
        self.caps: dict[str, int] = {p: self.ceiling for p in platforms}
        self.inflight: dict[str, int] = {p: 0 for p in platforms}
        self._seen: dict[str, int] = {}
        self._blocked: dict[str, int] = {}
        self.adjustments: deque[tuple[str, int, int, float]] = deque(maxlen=64)
        self._window = max(4, SNIPE_PLATFORM_PACE_WINDOW)

    def allows(self, platform: str) -> bool:
        """May this platform take another check right now?"""
        return self.inflight.get(platform, 0) < self.caps.get(platform, self.ceiling)

    def start(self, platform: str) -> None:
        self.inflight[platform] = self.inflight.get(platform, 0) + 1

    def finish(self, platform: str, blocked: bool | None) -> None:
        """Release the in-flight slot and judge the platform on what came back.

        ``None`` means the check produced no result at all (it was dropped), so
        the slot is released without counting as an answer -- otherwise a run
        that lost tasks would read as a platform recovering on no evidence.
        """
        if not platform:
            return
        if self.inflight.get(platform):
            self.inflight[platform] -= 1
        if blocked is None:
            return
        self._seen[platform] = self._seen.get(platform, 0) + 1
        if blocked:
            self._blocked[platform] = self._blocked.get(platform, 0) + 1
        if self._seen[platform] < self._window:
            return
        share = self._blocked.get(platform, 0) / self._seen[platform]
        cap = self.caps.get(platform, self.ceiling)
        if share >= SNIPE_PLATFORM_PACE_RATE:
            new = max(SNIPE_PLATFORM_MIN_PARALLEL, cap // 2)
        elif share <= SNIPE_PLATFORM_PACE_RECOVER:
            new = min(self.ceiling, cap + 1)
        else:
            new = cap
        self._seen[platform] = 0
        self._blocked[platform] = 0
        if new != cap:
            self.caps[platform] = new
            self.adjustments.append((platform, cap, new, time.monotonic()))
            log.info(
                "pacing %s: %d -> %d in flight (%d%% refusals of the last %d)",
                platform, cap, new, round(share * 100), self._window,
            )

    def snapshot(self) -> dict[str, dict[str, object]]:
        """The platforms held below the run's ceiling, for the dashboard."""
        out: dict[str, dict[str, object]] = {}
        for platform, cap in self.caps.items():
            if cap >= self.ceiling:
                continue
            out[platform] = {
                "parallel": cap,
                "ceiling": self.ceiling,
                "in_flight": self.inflight.get(platform, 0),
            }
        return out

    def recent(self, limit: int = 6) -> list[dict]:
        """The last few adjustments, newest last."""
        now = time.monotonic()
        return [
            {"platform": p, "was": a, "now": b, "seconds_ago": round(now - t, 1)}
            for p, a, b, t in list(self.adjustments)[-limit:]
        ]


def confirm_check(result: SnipeResult) -> bool:
    """Should an `available` from this check be re-asked before it is reported?

    Off entirely by SNIPE_CONFIRM_FREE. A result the checker itself flagged as
    unreadable is never a free name, so it is never confirmed either.
    """
    return SNIPE_CONFIRM_FREE and result.status == "available"


async def confirm_available(
    platform: str, username: str, pool: ProxyPool, first: SnipeResult
) -> SnipeResult:
    """Ask a free name again through a different exit, and report the truth.

    Three honest outcomes, and no fourth:

    * the second read agrees -> free, and marked ``confirmed``;
    * the second read says the name is held -> **the taken verdict is what is
      reported**, the first read is discarded and the correction is counted, so
      a name is never offered as free on one exit's word;
    * the second read cannot answer -> the free verdict stands, but it is left
      unconfirmed, and the site shows it as unconfirmed rather than as sure.
    """
    second_proxy = pool.next()
    if second_proxy is None:
        first.detail = (first.detail + "; unconfirmed, no second exit").strip("; ")
        return first
    try:
        second = await CHECKERS[platform](pool.client(second_proxy), username)
    except asyncio.CancelledError:
        pool.abandon(second_proxy)
        raise
    except Exception:
        first.detail = (first.detail + "; unconfirmed, second read failed").strip("; ")
        return first
    second.proxy = second_proxy
    if second.status == "available":
        first.confirmed = True
        first.detail = (first.detail + "; confirmed on a second exit").strip("; ")
        return first
    if second.status in FINAL_STATUSES:
        second.corrected = True
        second.detail = "held -- first read said free on another exit"
        return second
    first.detail = (first.detail + "; unconfirmed, second read was " + second.status).strip("; ")
    return first


# --- the batch ------------------------------------------------------------
async def check_once(
    platform: str, username: str, pool: ProxyPool, confirm: bool | None = None
) -> SnipeResult:
    """One name on one platform, through the next proxy the pool hands out."""
    proxy = pool.next()
    if proxy is None:
        detail = POOL_MISS_DETAILS[0] if len(pool) else POOL_MISS_DETAILS[1]
        return SnipeResult(username, platform, "error", detail)
    started = time.perf_counter()
    try:
        result = await CHECKERS[platform](pool.client(proxy), username)
    except asyncio.CancelledError:
        # Stopped mid-flight. Hand the mark back, or this proxy carries a
        # phantom in-flight request for the rest of the run and is never judged.
        pool.abandon(proxy)
        raise
    except Exception as exc:  # pragma: no cover - guard against library surprises
        result = SnipeResult(username, platform, "error", f"unexpected:{type(exc).__name__}")
    result.latency_ms = (time.perf_counter() - started) * 1000.0
    result.proxy = proxy
    # A free name is what the user acts on, so it is re-asked once before the
    # run will call it free. A rotating pool reaches a different exit, which is
    # the whole point: one exit misreading a platform must not become a "hit".
    if confirm_check(result) and result.status == "available":
        result = await confirm_available(platform, username, pool, result)
    # A 407 is the proxy itself demanding credentials, not the target. The
    # checkers see it as an ordinary response, so it is caught here and charged
    # to the proxy -- otherwise an authenticated list with a wrong credential
    # never answers and never retires, and the run limps one unusable host at a
    # time. Only 407: a 5xx can come from either side and is left unanswered.
    if (
        result.status == "error"
        and result.fault == ""
        and result.detail.startswith("status 407")
    ):
        result.fault = FAULT_PROXY
    # How a result reflects on the proxy depends on what happened: a block is the
    # platform's doing, a transport error is the proxy's, and an answer nobody
    # can read is neither. Relabelling here keeps that judgement in one place --
    # every checker still just says "error" when a request does not answer.
    outcome = outcome_for(result)
    if outcome in (OUTCOME_UNKNOWN, OUTCOME_TIMEOUT):
        # Neither produced a verdict about the name. Both are retried on another
        # proxy; only the timeout additionally rests the host it came from.
        result.status = STATUS_UNANSWERED
    pool.report(proxy, outcome, platform, result.retry_after)
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
    # The window is how many checks may be dispatched but not yet finished.
    #
    # It must not exceed what the pool can carry. This loop creates tasks without
    # awaiting, so `pool.usable()` is still the reading from before the first of
    # them ran: a one-proxy list at concurrency 256 happily queued a thousand
    # checks, the proxy rested after its first failure, and every queued check
    # after that came back "proxies exhausted this run". Measured on one dead
    # proxy: 192 of 256 rows were that message, and at concurrency 256 the run
    # finished the whole list without ever saying why.
    window = max(1, min(concurrency * window_factor, max(1, pool.alive() * pool.per_proxy)))
    # Each platform answers for its own share of that window. The pool keeps the
    # pacer so a report can say which platform a run slowed down for.
    pace = PlatformPacer(platforms, max(1, concurrency))
    pool.pace = pace

    async def one(platform: str, username: str) -> SnipeResult | None:
        async with sem:
            attempt = 0
            # The last real attempt at this name, kept so a pool that dies
            # between two retries does not erase a failure that really happened.
            tried: SnipeResult | None = None
            while True:
                if stop is not None and stop.is_set():
                    return None
                result = await check_once(platform, username, pool)
                if result.detail in POOL_MISS_DETAILS:
                    # Nothing in the pool could carry this name. `next()` hands
                    # back None only once every proxy is retired, so this is the
                    # run's ending rather than a judgement on the name -- and
                    # filing it as an error row is what used to fill Results with
                    # hundreds of "proxies exhausted" lines and made a dead list
                    # look like a broken scanner. Counted as a miss and dropped:
                    # only a name that was genuinely asked about, and failed, is
                    # reported, and then as the failure it was.
                    if tried is None:
                        pool.misses += 1
                    return tried
                if result.status not in ("blocked", "error", STATUS_UNANSWERED):
                    return result
                tried = result
                if result.status == "blocked" and result.retry_after:
                    # The platform named the wait, and it is measured in minutes:
                    # discord answered 429 with retry_after 1253, 679 and 187
                    # through this pool. The bucket is per exit IP, so a retry
                    # now asks the same closed bucket and only spends requests --
                    # which is how a throttled name turned into two throttle rows
                    # and twice the pressure that caused it. Reported blocked
                    # once; the platform pause decides when it is worth asking
                    # again.
                    return result
                # How many retries are worth spending depends on who could take
                # them. With a second proxy in play a reprieve is real, so the
                # name gets the full budget on a different exit IP. With one
                # proxy every retry goes down the same tunnel: one more shot is
                # cheap insurance against a transient reset, but three in a row
                # on a host that just failed trades real throughput for a
                # failure the proxy already declared.
                budget = retries if pool.alive() > 1 else min(1, retries)
                if attempt < budget:
                    attempt += 1
                    continue
                return result

    def harvest(task: asyncio.Task) -> SnipeResult | None:
        """Read one finished task, and never let it take the run down with it.

        `one()` is written not to raise, but a task that *did* raise would
        otherwise propagate out of the generator and abort the response -- the
        browser sees a dropped stream (`Load failed`) mid-run instead of the
        rest of the names. A real check that fails is reported as the failure it
        was; an unexpected one is dropped, and the run carries on.
        """
        del pending[task]
        try:
            return task.result()
        except asyncio.CancelledError:
            return None
        except Exception:  # pragma: no cover - guard against library surprises
            # The check never produced a verdict, so it is dropped rather than
            # filed against a platform it never reached. The run continues, and
            # the summary counts it among the checks that were never made.
            log.debug("a check task raised; dropping it", exc_info=True)
            return None

    ordered = shuffled(usernames) if SHUFFLE_NAMES else usernames
    jobs = job_stream(ordered, platforms)
    jobs_done = False
    held: tuple[str, str, str] | None = None
    # Jobs a platform's allowance would not take yet. They go round again on the
    # next pass rather than being dropped, so a paced platform slows the run
    # instead of ending its share of it.
    deferred: deque[tuple[str, str, str]] = deque()
    # task -> the platform it was dispatched for, so a finished check can be
    # released against the right allowance.
    pending: dict[asyncio.Task, str] = {}
    # Timestamp of the last "everything is cooling down" note, so a proxy that
    # alternates fail/answer cannot turn it into a line per check.
    cooling_note = -1e9
    # When the current all-platforms-paused sit-out began, so the wait is bounded.
    pause_since = 0.0
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
                platform = pending.get(task, "")
                result = harvest(task)
                pace.finish(platform, None if result is None else result.status == "blocked")
                if result is not None:
                    answered += 1 if result.answered else 0
                    yield result

            # Every proxy gone means nothing else can be dispatched, so the run
            # stops and says so rather than working through the rest of the name
            # list against a pool that is not there. Checked only once a pool
            # miss has been seen, because alive() walks the whole list.
            if pool.misses and pool.alive() == 0:
                pool.stop_reason = pool.exhausted_reason()
                break

            # Any platform that has now refused the whole pool is sat out with
            # the reason the platform gave. The run continues on the platforms
            # that are still answering instead of stopping on the worst one.
            pool.pause_blocked(platforms)
            if platforms and all(pool.is_paused(p) for p in platforms):
                # Every platform is sitting out at once -- a datacenter list
                # refused by every target is the usual cause, and ending the run
                # here is what reported "17,303 checks were never made": every
                # name not yet asked about was thrown away. Sit out for the
                # shortest pause and resume instead, bounded so a pool that can
                # never answer still ends honestly.
                remaining = pool.shortest_pause()
                now_m = time.monotonic()
                if pause_since == 0.0:
                    pause_since = now_m
                if now_m - pause_since >= SNIPE_PLATFORM_WAIT:
                    pool.stop_reason = pool.blocked_reason()
                    break
                if now_m - cooling_note > 5.0:
                    cooling_note = now_m
                    yield SnipeResult(
                        "", "", WAITING_STATUS,
                        f"every platform is sitting out a throttle -- waiting "
                        f"{remaining:.0f}s for the shortest to lift",
                    )
                await asyncio.sleep(min(1.0, max(0.05, remaining)))
                continue
            pause_since = 0.0

            # Every live proxy is cooling down after an error. Work still flows
            # -- the pool hands out whichever comes back first -- but say so
            # occasionally, because this is the state that used to look frozen.
            # The old code slept here instead, up to ten seconds a time.
            if pool.alive() and pool.cooling() >= pool.alive():
                now_m = time.monotonic()
                if now_m - cooling_note > 15.0:
                    cooling_note = now_m
                    yield SnipeResult(
                        "", "", WAITING_STATUS,
                        f"all {pool.alive()} proxies are cooling down after an error -- still "
                        "dispatching to whichever returns first",
                    )

            # `deferred` has to be drained even once the job list is spent, or a
            # paced platform's last held-back names are dropped at the end of the
            # run -- which is exactly the "checks were never made" this file has
            # spent so long eliminating.
            # A pass may hand a job back to `deferred` at most `window` times.
            # Without that bound a paced platform whose single held-back job is
            # popped and re-appended spins here forever: the allowance never
            # frees while this loop is running, so nothing changes between the
            # pop and the append.
            rotations = 0
            while (not jobs_done or deferred) and len(pending) < window:
                if held is not None:
                    item, held = held, None
                elif deferred:
                    item = deferred.popleft()
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
                if not pace.allows(platform):
                    # Its allowance is full: this is the platform being asked
                    # slower than the run would like, which is the whole point.
                    # The job goes to the back of this pass, and the wait below
                    # is what frees the allowance for the next one.
                    deferred.append(item)
                    rotations += 1
                    if rotations >= window:
                        break
                    continue
                if pool.usable() == 0:
                    held = item
                    break
                pending[asyncio.create_task(one(platform, username))] = platform
                pace.start(platform)

            if jobs_done and not pending:
                break

            # Nothing left to dispatch and nothing in flight. That can only
            # mean the pool is empty: `held` is set only when nothing is left to
            # dispatch to, and every other exit from the loop above either fills
            # the window or runs the job list out.
            #
            # This is where a wait used to happen. Every live proxy sitting in a
            # cooldown made `usable()` zero, so the run slept for ten seconds a time
            # per wait and then abandoned the sweep -- measured on a host that
            # answered 70% of requests: 302 of 3000 names checked in 36s, 30s of
            # it asleep, then "the list is dead". A proxy in a cooldown is not a
            # pool that cannot be used, and only retirement decides that now.
            if not jobs_done and not pending:
                pool.stop_reason = pool.exhausted_reason()
                break

            if not pending:
                break

            waiters: set[asyncio.Task] = set(pending)
            if stop_task is not None and not stop_task.done():
                waiters.add(stop_task)
            # The timeout does not cancel anything -- `asyncio.wait` leaves the
            # pending tasks running -- it only wakes this loop so a heartbeat can
            # go out while the checks are slow.
            done, _ = await asyncio.wait(
                waiters, timeout=HEARTBEAT_AFTER, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                yield SnipeResult("", "", HEARTBEAT_STATUS, "")
                continue
            for task in done:
                if task is stop_task:
                    continue
                platform = pending.get(task, "")
                result = harvest(task)
                pace.finish(platform, None if result is None else result.status == "blocked")
                if result is not None:
                    answered += 1 if result.answered else 0
                    yield result
            if pool.misses and pool.alive() == 0:
                pool.stop_reason = pool.exhausted_reason()
                break
            # Any platform that has now refused the whole pool is sat out with
            # the reason the platform gave. The run continues on the platforms
            # that are still answering instead of stopping on the worst one.
            pool.pause_blocked(platforms)
            if platforms and all(pool.is_paused(p) for p in platforms):
                # Every platform is sitting out at once -- a datacenter list
                # refused by every target is the usual cause, and ending the run
                # here is what reported "17,303 checks were never made": every
                # name not yet asked about was thrown away. Sit out for the
                # shortest pause and resume instead, bounded so a pool that can
                # never answer still ends honestly.
                remaining = pool.shortest_pause()
                now_m = time.monotonic()
                if pause_since == 0.0:
                    pause_since = now_m
                if now_m - pause_since >= SNIPE_PLATFORM_WAIT:
                    pool.stop_reason = pool.blocked_reason()
                    break
                if now_m - cooling_note > 5.0:
                    cooling_note = now_m
                    yield SnipeResult(
                        "", "", WAITING_STATUS,
                        f"every platform is sitting out a throttle -- waiting "
                        f"{remaining:.0f}s for the shortest to lift",
                    )
                await asyncio.sleep(min(1.0, max(0.05, remaining)))
                continue
            pause_since = 0.0
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
