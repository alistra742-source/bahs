"""Runtime configuration.

Every tunable lives here and is read from the environment, so a Railway
variable change is a redeploy away from retuning the service. No module below
this one reads os.environ directly.
"""

import os
import ssl


# One SSL context shared by every outbound client.
#
# ssl.create_default_context() parses the whole CA bundle and costs ~730 KB.
# httpx builds a fresh one per AsyncClient, so every in-flight check used to
# carry ~1.25 MB of TLS context -- ~1.5 GB at 1200 concurrent checks, which is
# what got the process OOM-killed, and a dying process reads to the caller as
# "every proxy is dead". An SSLContext is safe to share across concurrent
# connections (each socket gets its own SSLSession), so sharing one turns
# concurrency back into a network setting instead of a memory setting.
SSL_CONTEXT: ssl.SSLContext = ssl.create_default_context()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _ports(name: str, default: str) -> tuple[int, ...]:
    """A list of TCP ports from one variable, e.g. ``"9050,9150"``.

    A typo must not take the service down: an unreadable token is dropped and
    an entry that is not a port at all is ignored, which leaves the fallback
    short one exit rather than answering every request with a stack trace. If
    *nothing* in the variable is readable the default is used, because an empty
    list here means "look for Tor nowhere" -- the one reading nobody asks for.
    """
    raw = _str(name, default)
    ports: list[int] = []
    for token in raw.replace(";", ",").replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            port = int(token)
        except ValueError:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    if ports:
        return tuple(ports)
    return tuple(int(p) for p in default.split(",") if p.strip().isdigit())


# --- Service ---------------------------------------------------------------
# The user agent every checker sends unless a platform needs its own.
#
# A knob and not a constant on purpose: platforms decide whether to answer a
# client at all, and they change their minds. A year-old Chrome version was once
# refused outright ("Access denied, old Chrome version") while a current one was
# answered, and two platforms here are only answered at all through a link-preview
# agent (see PREVIEW_UA in sniper.py). Either way a UA baked into the source is a
# time-bomb that shows up as a platform refusing the whole datacenter.
USER_AGENT: str = _str(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
)

# Railway injects PORT. Bind 0.0.0.0 or the platform cannot reach the process.
HOST: str = _str("HOST", "0.0.0.0")
PORT: int = _int("PORT", 8080)
LOG_LEVEL: str = _str("LOG_LEVEL", "info")

# --- Proxy list ------------------------------------------------------------
# The list the user pasted or uploaded. This is the only source of proxies --
# nothing is scraped and nothing is validated ahead of time, so what is in the
# file is exactly what a run rotates over. Point this at a Railway volume mount
# to keep the list across deploys.
STORE_PATH: str = _str("STORE_PATH", "data/proxies.json")
# Ceiling on the stored list. 100k lines is ~3 MB of file and ~4 MB in memory;
# past that the list costs more than it can be used for.
MAX_PROXIES: int = _int("MAX_PROXIES", 100000)
# Ceiling on how much of the stored list one run holds at once. Defaults to all
# of it; lower it only to bound memory, since a run rotates the whole list.
SNIPE_POOL: int = _int("SNIPE_POOL", 100000)

# --- Checking the list -----------------------------------------------------
# What a proxy is asked for when the list itself is tested (POST /proxies/check).
#
# Without this the first honest answer about a proxy came from a run: a name
# spent, an error row, and -- if every exit failed -- a sweep that stopped with
# "the list is dead". Finding out whether a list worked before spending names on
# it meant curling from a shell on the host, which is not something the person
# paying for the tool should have to do.
#
# The default target answers with the IP the site sees, so one request proves the
# tunnel carries traffic *and* names the exit -- which is how you find out that a
# "rotating" endpoint is handing out one address. Point it at http:// instead of
# https:// if a list is HTTP-CONNECT only.
PROXY_CHECK_URL: str = _str("PROXY_CHECK_URL", "https://api.ipify.org?format=json")
# Checks in flight at once. Each one is a single request through a single proxy,
# so this is bounded by the list as much as by the network. The whole list is
# swept, so a 500-line list at this width finishes in the time a run would spend
# on a handful of names.
PROXY_CHECK_CONCURRENCY: int = _int("PROXY_CHECK_CONCURRENCY", 32)
# Connect and read ceiling for one check. More generous than a run's, because a
# check is a diagnostic: an exit that answers in five seconds is a usable exit
# that a three-second ceiling would have called dead.
PROXY_CHECK_TIMEOUT: float = _float("PROXY_CHECK_TIMEOUT", 6.0)
# Ceiling on how many entries one check sweeps, and therefore on how long the
# request can run. A list (or paste) larger than this is checked from the top and
# the response says how many were left out rather than truncating silently.
PROXY_CHECK_MAX: int = _int("PROXY_CHECK_MAX", 2000)

# --- Tor (the pool when there is no list) ----------------------------------
# An empty proxy list used to end at a 503, which is a dead end for the people
# the tool is for: paste nothing, get nothing. Tor is the one pool that exists
# without anyone pasting it, so when the stored list is empty the run falls back
# to the Tor exits reachable from this host -- and says in the run itself that
# that is what it is doing.
#
# It is a fallback and not a replacement, and the copy is careful about it: a
# Tor exit is shared by thousands of users and platforms rate-limit or refuse
# those addresses, so a Tor run answers less than a good private list does. It
# is the difference between a tool that does something on the first click and
# one that asks for homework first. A stored list always wins over Tor.
TOR_ENABLED: bool = _bool("TOR_ENABLED", True)
# Where a Tor SOCKS listener may already be found: 9050 is the tor daemon's
# default and 9150 is Tor Browser's. Both are only ever *looked at*; nothing here
# touches a user's browser or its profile.
TOR_PORTS: tuple[int, ...] = _ports("TOR_PORTS", "9050,9150")
# Loopback only, deliberately. A remote SOCKS port would be an exit we do not
# control and cannot vouch for, and "Tor" would stop being a fact about the
# process and become a claim about somebody else's server.
TOR_HOST: str = _str("TOR_HOST", "127.0.0.1")
# The SocksPorts an instance started by this service listens on. Several ports
# rather than one because tor keeps the streams of different SocksPorts on
# different circuits by default, which is the only way to get more than one exit
# (and therefore more than one shared address to be refused) out of one Tor.
# 9051 is skipped: that is the conventional control port and this service never
# opens one.
TOR_MANAGED_PORTS: tuple[int, ...] = _ports("TOR_MANAGED_PORTS", "9050,9052,9054,9056")
# Start our own tor when nothing is listening. It binds 127.0.0.1 only, opens no
# control port, and relays nothing (ClientOnly): a private outbound client, not
# a proxy service. Off leaves the fallback to a Tor the operator runs.
# Check your hosting provider's terms before leaving this on -- the traffic it
# carries is yours, but "anonymisation" in an AUP is a word worth reading twice.
TOR_AUTO_START: bool = _bool("TOR_AUTO_START", True)
TOR_BINARY: str = _str("TOR_BINARY", "tor")
# Where the torrc, its data directory and its log go. Must be writable.
TOR_DATA_DIR: str = _str("TOR_DATA_DIR", "data/tor")
# A first start with a cold data directory takes ~50s to bootstrap and can take
# longer on a bad day, so a run that needs Tor waits this long for it. The boot
# warmup means it is normally already up by then.
TOR_START_TIMEOUT: float = _float("TOR_START_TIMEOUT", 75.0)
# Local SOCKS5 handshake, used to tell "a SOCKS5 listener is here" from "nothing
# is on that port". Loopback, so this is a connect timeout and not a network wait.
TOR_PROBE_TIMEOUT: float = _float("TOR_PROBE_TIMEOUT", 2.0)
# After a start, how long to wait for a SocksPort to actually accept a
# connection before concluding the instance is not usable. Tor opens its
# listeners before it announces 100%, so this is normally instant -- and without
# it the fallback answered "no SOCKS5 listener" a millisecond after starting one.
TOR_BIND_WAIT: float = _float("TOR_BIND_WAIT", 10.0)
# How long an *empty* answer is cached, as opposed to a confirmed set of exits.
# A tor that is still coming up must be noticed in seconds rather than after the
# full TOR_VERIFY_TTL, or the page tells the user there is no fallback while one
# is bootstrapping behind it.
TOR_RETRY_AFTER: float = _float("TOR_RETRY_AFTER", 15.0)
# Tor's own "am I using Tor" answer. One request per exit through the exit, and
# the only evidence this service accepts before it calls an exit Tor: a SOCKS5
# listener on port 9050 could equally be something else, and a page that says
# "Tor" about it would be guessing.
TOR_CHECK_URL: str = _str("TOR_CHECK_URL", "https://check.torproject.org/api/ip")
# Slow on purpose -- three relays in three countries. A verification that
# arrives inside this is an exit that works.
TOR_VERIFY_TIMEOUT: float = _float("TOR_VERIFY_TIMEOUT", 25.0)
# How long a verification is trusted. Tor's own answer does not change minute to
# minute, and re-verifying on every page load would be a request per exit per
# refresh for the same sentence.
TOR_VERIFY_TTL: float = _float("TOR_VERIFY_TTL", 600.0)
# Verifications in flight at once. Four exits is the default, and there is no
# reason to ask check.torproject.org for more at once than that.
TOR_VERIFY_CONCURRENCY: int = _int("TOR_VERIFY_CONCURRENCY", 4)
# Checks in flight through Tor, for both a run and POST /proxies/check. Small on
# purpose: one client's circuits are the bottleneck long before the socket count
# is, and Tor's own documentation makes a throughput promise to nobody. A run
# over Tor is slower than the same run over a private list, and saying so is
# better than opening 64 circuits at the check target and calling it speed.
TOR_CONCURRENCY: int = _int("TOR_CONCURRENCY", 8)
# How many requests one Tor exit is asked to carry at once.
TOR_PER_PROXY: int = _int("TOR_PER_PROXY", 2)
# Connect and read ceiling for one request through Tor, in a run and in a check.
TOR_TIMEOUT: float = _float("TOR_TIMEOUT", 20.0)

# --- Timeouts (seconds) ----------------------------------------------------
# Short on purpose. A dead proxy costs its connect timeout and pasted lists are
# mostly dead, so these two numbers dominate how fast a run moves. Lower them
# and the run gets faster; raise them and more slow-but-alive hosts survive.
CONNECT_TIMEOUT: float = _float("CONNECT_TIMEOUT", 3.0)
READ_TIMEOUT: float = _float("READ_TIMEOUT", 8.0)

# --- Platforms -------------------------------------------------------------
# GitHub's REST API allows 60 requests an hour per IP unauthenticated, which is
# not a check rate: a run hits it in under a minute and then sits out the rest of
# the hour answering 403. A token raises the ceiling to 5000 and is the
# difference between GitHub being a usable platform and a decorative one. Every
# other platform here needs nothing.
GITHUB_TOKEN: str = _str("GITHUB_TOKEN", "")

# --- Confirming a "free" answer ---------------------------------------------
# Two platforms will answer a name that *is* held with the same fallback page
# they serve for a name nobody holds, once they stop answering the client that
# is asking. See SignalGuard in sniper.py for the measurement. So before a free
# verdict from one of those is trusted, the platform is asked about a name that
# is certainly held.
#
# SNIPE_SIGNAL_GUARD_TTL is how long one control answer stands, in seconds, and
# SNIPE_SIGNAL_GUARD_AFTER how many free verdicts may lean on it. The product is
# the longest a signal that has broken can keep producing free verdicts -- 20s x
# 8 by default. A TTL of zero re-reads the control for every free name.
SNIPE_SIGNAL_GUARD_TTL: float = _float("SNIPE_SIGNAL_GUARD_TTL", 20.0)
SNIPE_SIGNAL_GUARD_AFTER: int = _int("SNIPE_SIGNAL_GUARD_AFTER", 8)

# --- TikTok ----------------------------------------------------------------
# Which source leads the check. The profile page does, by default, because it is
# both the authoritative one and the one TikTok does not throttle: measured
# through one rotating endpoint at 16 concurrent, 80 of 80 page reads came back
# with a verdict while 56 of the same 80 oEmbed calls came back 429/503 -- and
# the embed also cost a wasted round trip, so page-first ran at 6.9 names/s
# against 3.6/s. The price is bytes: ~50 KB per check against 45 bytes, which is
# ~880 MB for a 17,576-name sweep. Set false to lead with the 45-byte embed and
# spend the page only on what it cannot answer -- cheaper, and many more rows
# come back `blocked`.
TIKTOK_PAGE_FIRST: bool = _bool("TIKTOK_PAGE_FIRST", True)
# Read ceiling for the profile page. It is 166-202 KB, so it needs more room than
# a normal check; it is a ceiling, not a wait.
TIKTOK_PAGE_TIMEOUT: float = _float("TIKTOK_PAGE_TIMEOUT", 15.0)

# TikTok's own validator, ``GET /api/uniqueid/check/`` -- the endpoint the signup
# page calls when you type a handle -- is the only source that answers the
# question a free name actually raises: not "is there a live profile" but "can it
# be claimed". The profile page gives one answer (10221) for a name nobody has
# ever held and for one held by a banned or deactivated account, so a free read
# from it is only ever a lead. This endpoint settles it, and it needs a logged-in
# ``sessionid`` cookie: asked without one it returns an empty body (measured),
# and the logged-in change-username route answers "no permission".
#
# Empty (the default) means the check keeps reporting free TikTok reads as
# unconfirmed leads, which is honest and works everywhere. Set it and a free
# name is settled by TikTok itself and reported confirmed.
#
# This is a credential for a real account: a sessionid is a bearer token, the
# account it belongs to can be changed through it, and TikTok expires it on
# logout or password change. It is read from the environment only -- never from
# the settings file, which this service serves back over an unauthenticated API.
# Treat it as a secret, and prefer a throwaway account.
TIKTOK_SESSION_ID: str = _str("TIKTOK_SESSION_ID", "")

# --- Alerts ----------------------------------------------------------------
# A Discord webhook told which names came back free. Empty disables alerting.
# The default template matches the one every tool in this space ships with.
# Where the UI's overrides for the alert settings are kept. The environment
# stays the base, so the service runs from Railway variables alone and this file
# only ever holds what someone changed by hand.
SETTINGS_PATH: str = _str("SETTINGS_PATH", "data/settings.json")
# What today's runs actually did. The landing page shows a live strip -- names
# checked today, hits found, the average verdict rate -- and those numbers have
# to survive a restart and be the run's own, not a marketing figure. Written when
# a run ends, reset when the date rolls over.
STATS_PATH: str = _str("STATS_PATH", "data/stats.json")
ALERT_WEBHOOK: str = _str("ALERT_WEBHOOK", "")
ALERT_TEMPLATE: str = _str("ALERT_TEMPLATE", "{platform} username available: `{username}`")
# Posted as the webhook's own name/avatar when set.
ALERT_USERNAME: str = _str("ALERT_USERNAME", "VORTEX")
ALERT_AVATAR: str = _str("ALERT_AVATAR", "")
# Prepended to the first message of a run, so a role or user can be pinged.
# Discord only renders a ping when it is in ``content``, never in an embed.
ALERT_PING: str = _str("ALERT_PING", "")
# Names are digested into embed batches of this size -- Discord's own ceiling is
# ten embeds per message, and a scan can find hundreds of names at once, so one
# message per name would be a rate limit rather than a notification.
ALERT_BATCH: int = _int("ALERT_BATCH", 10)
# Wall-clock floor between two webhook posts, so a fast run cannot hammer
# Discord's endpoint and get the webhook deleted.
ALERT_MIN_INTERVAL: float = _float("ALERT_MIN_INTERVAL", 1.2)
# Ceiling on posts per run, counted in batches.
ALERT_MAX_MESSAGES: int = _int("ALERT_MAX_MESSAGES", 20)

# --- One-off check ---------------------------------------------------------
# Simultaneous checks for POST /snipe. Each check is one in-flight request
# through one proxy, so this is bounded by the list as much as by the network.
SNIPE_CONCURRENCY: int = _int("SNIPE_CONCURRENCY", 64)
# How many times a blocked or proxy-failed name is retried on another proxy.
SNIPE_RETRIES: int = _int("SNIPE_RETRIES", 2)
# Ceiling on names accepted in one /snipe request (before the platform
# multiplier).
SNIPE_MAX_NAMES: int = _int("SNIPE_MAX_NAMES", 20000)
# Floor for the per-proxy connection pool. The real number is worked out per
# run: ceil(concurrency / pool size), so the pool can actually carry the
# concurrency that was asked for instead of being throttled to this floor.
# Measured with a single proxy endpoint: 32 concurrent guns.lol checks ran at
# 3.9/s with 1 connection, 13.0/s with 6, and 53.1/s with 32 -- the connection
# cap, not the check, was the whole ceiling. A list of 500 proxies and a
# concurrency of 256 still only works out to the floor.
SNIPE_PER_PROXY: int = _int("SNIPE_PER_PROXY", 4)
# Ceiling on one proxy's pool, so a one-line list cannot open a thousand sockets
# to the same host. Raised from 64 after measuring the real thing: through one
# rotating endpoint on guns.lol, 200 names at 32 concurrent ran at 12.8/s, at 64
# at 15.3/s, and at 128 at 19.6/s -- a short list was being capped below the
# throughput the proxy would actually carry. Past that it flattens and then
# degrades (192: 11.9/s, 256: 11.0/s), which is where this ceiling sits. A long
# list still divides down to the floor, so this only ever affects a short one.
SNIPE_PER_PROXY_MAX: int = _int("SNIPE_PER_PROXY_MAX", 128)
# Warm clients kept open at once. One client per proxy is what makes a batch
# fast (the CONNECT tunnel and the TLS handshake are paid once per proxy), but
# keeping one for every proxy in a 100k-line list would cost ~1 GB of buffers
# and an fd per socket. Past this many, the least recently used client is
# closed and re-created if its proxy comes round again.
SNIPE_MAX_CLIENTS: int = _int("SNIPE_MAX_CLIENTS", 512)
# A proxy that *errors* (transport, timeout) is rested this long, so a retry
# lands on a different host instead of the one that just refused. 0 disables it.
SNIPE_PROXY_COOLDOWN: float = _float("SNIPE_PROXY_COOLDOWN", 60.0)
# A proxy the *platform* refused (a 429 or a challenge) is a different thing: the
# host is fine, the platform is throttling. It is rested for this long instead
# and counted separately, so a blanket rate limit cannot retire a healthy list
# and turn the run into "proxies exhausted". Short on purpose -- a rotating
# endpoint answers from a different exit IP on the next request.
SNIPE_BLOCK_COOLDOWN: float = _float("SNIPE_BLOCK_COOLDOWN", 5.0)
# How many platform blocks one proxy may collect before it is retired. High,
# because a block that repeats across proxies means the platform is throttling,
# not that the list is full of dead hosts.
SNIPE_PROXY_BLOCK_LIMIT: int = _int("SNIPE_PROXY_BLOCK_LIMIT", 40)
# The share of a proxy's own answers that must be refusals before that count
# retires it. Measured live through one rotating endpoint on guns.lol: 105
# refusals out of 594 answers (17.7%) -- a working proxy -- retired by the raw
# count of 40 alone, which ended the sweep. Refusals now have to dominate what
# the proxy answered for the count to mean anything.
SNIPE_PROXY_BLOCK_RATE: float = _float("SNIPE_PROXY_BLOCK_RATE", 0.8)
# The least attempts a proxy is judged on, and how many of them have to fail
# before it is taken out of the rotation for the rest of the run.
SNIPE_PROXY_FAIL_LIMIT: int = _int("SNIPE_PROXY_FAIL_LIMIT", 6)
# The share of a proxy's attempts that may fail before it is written off, once
# it has carried at least sixteen of them. A *rate* rather than a streak,
# because a streak is wiped by any answer: a host that answers one request in
# three never built one, so it stayed on the list forever while every third name
# came back an error. At 0.75 a list has to be very nearly dead before a run
# stops using it -- stopping a sweep is worse than reporting an error rate, and
# a host still answering two requests in five is still producing answers.
SNIPE_PROXY_FAIL_RATE: float = _float("SNIPE_PROXY_FAIL_RATE", 0.75)
# Seconds the warm pool is reused across requests before it is rebuilt, so
# consecutive batches skip their handshakes.
SNIPE_POOL_TTL: int = _int("SNIPE_POOL_TTL", 300)
# --- Bulk scan -------------------------------------------------------------
# A scan is the same loop at throughput: many names per platform.
SCAN_CONCURRENCY: int = _int("SCAN_CONCURRENCY", 256)
# Ceiling on names accepted in one scan (before the platform multiplier). A run
# that enumerates a whole namespace is bigger than this by design, so it is the
# enumeration ceiling rather than a sampling budget.
SCAN_MAX_NAMES: int = _int("SCAN_MAX_NAMES", 1000000)
# Most names one *buffered* scan may hold. The streaming path hands results to
# the client as they land and holds nothing; the buffered path builds the whole
# result list in memory, so past this it is refused and told to stream rather
# than being allowed to run the process out of memory.
SCAN_BUFFER_MAX: int = _int("SCAN_BUFFER_MAX", 50000)
# A scan retries a refusal once by default. A transient 401/403 through a
# rotating endpoint lands on a different exit IP on the next attempt, so one
# retry turns most of those into a real verdict for 4% more requests; a blanket
# throttle never gets retried because it pauses the platform first (see
# SNIPE_PLATFORM_BLOCK_RATE). Set 0 for pure throughput on a very wide run.
SCAN_RETRIES: int = _int("SCAN_RETRIES", 1)
# Tighter than the check above, because in a scan a dead proxy is pure lost
# time.
SCAN_CONNECT_TIMEOUT: float = _float("SCAN_CONNECT_TIMEOUT", 2.0)
SCAN_READ_TIMEOUT: float = _float("SCAN_READ_TIMEOUT", 6.0)
# Floor for the per-proxy connection pool during a scan; the real number is
# ceil(concurrency / pool size), as above.
SCAN_PER_PROXY: int = _int("SCAN_PER_PROXY", 6)
# The rate a scan aims for; reported against in /scan and on the dashboard.
SCAN_TARGET_RATE: int = _int("SCAN_TARGET_RATE", 100)

# --- Name order ------------------------------------------------------------
# Every run draws its names in a fresh random order. A run that always walks
# a-z first spends the same proxies on the same prefixes and shows the same
# crowded corner of the namespace first; shuffling spreads the load and makes
# two runs over the same selection ask two different questions first. On by
# default -- there is no reason to want the alphabetical order back, and
# "randomize the name list, always" is the standing order.
SHUFFLE_NAMES: bool = _bool("SHUFFLE_NAMES", True)
# How many names are held at once while shuffling. Enumeration is lazy and can
# be tens of millions of names, so a full shuffle would put the whole space in
# memory -- the buffer keeps order random within each block while memory stays
# flat. Any list that fits the window (every snipe batch, and any scan up to
# it) is shuffled in full.
SHUFFLE_WINDOW: int = _int("SHUFFLE_WINDOW", 50000)

# --- Platform pacing -------------------------------------------------------
# A platform is not paced by the run's concurrency. One host answering 429 to a
# third of its requests still gets the whole window thrown at it until the
# refusal *rate* crosses SNIPE_PLATFORM_BLOCK_RATE and the platform is sat out
# for a minute and a half -- which is both the wrong shape (a hard stop instead
# of a slower ask) and the reason a throttled run reads as a wall of `blocked`.
# Instead every platform carries its own in-flight allowance, adjusted from what
# it answers: refusals dominate -> halve it; answers come back clean -> grow it
# back one at a time. A throttled platform slows down and keeps producing
# verdicts, and the run never stops to do it.
# How many checks a platform's allowance is judged on before it is adjusted.
SNIPE_PLATFORM_PACE_WINDOW: int = _int("SNIPE_PLATFORM_PACE_WINDOW", 12)
# The share of that window that has to be refusals before the allowance is
# halved. Measured on a datacenter range: tiktok's embed endpoint refused 70% of
# requests and discord's API 13-20% -- both of those halve; a handful of
# transient 401s (4%, measured on guns.lol) does not.
SNIPE_PLATFORM_PACE_RATE: float = _float("SNIPE_PLATFORM_PACE_RATE", 0.3)
# Once a window comes back at or under a third of that rate, the allowance grows
# again by one. Slow to grow, quick to cut: that is what keeps a platform from
# oscillating between throttled and hammered.
SNIPE_PLATFORM_PACE_RECOVER: float = _float("SNIPE_PLATFORM_PACE_RECOVER", 0.1)
# The floor an allowance can be cut to. One request at a time is still a run.
SNIPE_PLATFORM_MIN_PARALLEL: int = _int("SNIPE_PLATFORM_MIN_PARALLEL", 1)

# --- Platform throttling ---------------------------------------------------
# A platform that has refused every proxy in the list is sat out for this long
# rather than hammered. A blanket 429 is per platform: discord refusing the whole
# pool says nothing about tiktok, and a run that stops entirely -- or that keeps
# spending proxies on discord -- is spending the list on a question that already
# has an answer. When the platform's 429 names a retry_after, that wins, up to
# the ceiling.
SNIPE_PLATFORM_PAUSE: float = _float("SNIPE_PLATFORM_PAUSE", 90.0)
SNIPE_PLATFORM_PAUSE_MAX: float = _float("SNIPE_PLATFORM_PAUSE_MAX", 900.0)
# How long a platform's refusal of *one* proxy is remembered. A rotating
# endpoint answers from a different exit IP on the next request, so a refusal is
# transient by nature -- remembering it forever means every later request is
# treated as coming from a host the platform already refused. Set this above the
# rotation interval.
SNIPE_BLOCK_MEMORY: float = _float("SNIPE_BLOCK_MEMORY", 75.0)
# How many refusals a platform must have recorded before it can be sat out.
# This is the fix for the report that one 403 from a single rotating endpoint
# paused the whole platform and ended the run: a pause now needs real evidence
# (at least this many, and at least as many as there are live proxies), not the
# first refusal of a list that happens to be one line long.
SNIPE_PLATFORM_BLOCK_MIN: int = _int("SNIPE_PLATFORM_BLOCK_MIN", 3)
# The *share* of a platform's answers that must be refusals before it can be sat
# out. This is the guard that was missing: guns.lol intermittently answers 401
# (a clearance check tied to the exit IP), measured live at 46 refusals out of
# 1,036 answers -- 4.4%, a healthy run. Counting raw refusals paused the whole
# platform every few hundred checks at that rate. A pause now needs the platform
# to be refusing *most* of what it sees, which is what a blanket throttle looks
# like, while a handful of transient refusals just get retried.
SNIPE_PLATFORM_BLOCK_RATE: float = _float("SNIPE_PLATFORM_BLOCK_RATE", 0.5)
# When every platform is sitting out at once, the run waits for the shortest
# pause to lift and carries on instead of ending. The old behaviour dropped
# every name that had not been asked about yet. This bounds that wait, after
# which the run stops and says why rather than waiting forever.
SNIPE_PLATFORM_WAIT: float = _float("SNIPE_PLATFORM_WAIT", 300.0)
# A platform is only sat out once it has stopped *answering*. Refusals that
# arrive among working answers are a throttle on some exits, not a closed
# platform -- and pausing on them is what turned a run that was still resolving
# names into a stall. Measured through one rotating endpoint: discord answered
# 429 "retry_after 1979s" on several requests while other requests through the
# same endpoint were resolving names normally, and the pause that followed named
# those 1979 seconds -- 15 minutes off, bounded only by SNIPE_PLATFORM_PAUSE_MAX.
# This is how long a platform has to go without answering anything before its
# refusals are allowed to stop the run. Set 0 to pause on the refusal rate alone.
SNIPE_PLATFORM_QUIET: float = _float("SNIPE_PLATFORM_QUIET", 20.0)

# A free name is the one claim a user acts on -- it is the whole point of the
# run -- so it is the one verdict worth a second look. When a check comes back
# `available`, the same name is asked again through the next proxy (a rotating
# endpoint reaches a different exit), and only a second `available` marks it
# confirmed. If the second read says taken, the taken verdict is what the run
# reports and the correction is counted. A second read that cannot answer
# leaves the name free but *unconfirmed*, and the site says so.
SNIPE_CONFIRM_FREE: bool = _bool("SNIPE_CONFIRM_FREE", True)

# --- Runs ------------------------------------------------------------------
# Cap on the simultaneous snipe/scan runs the server will track. The dashboard
# only ever starts one, but the API is open -- unbounded runs are unbounded
# work.
MAX_CONCURRENT_RUNS: int = _int("MAX_CONCURRENT_RUNS", 8)
# Below this many usable proxies a run cannot answer much, so the response says
# so instead of showing a table of errors.
MIN_POOL_WARN: int = _int("MIN_POOL_WARN", 5)

# --- Username generation ---------------------------------------------------
# Most names one request may run over.
#
# This used to be 1,000,000 and it was a *memory* limit: the names were built as
# a list, and "all of length 4 alphanumeric" (1,679,616) is ~200 MB of Python
# strings while 14,776,336 is ~843 MB. Enumeration is lazy now -- itertools walks
# the space while the run consumes it -- so memory no longer scales with the
# size of the space and the only honest constraint left is time.
#
# So this is a sanity bound, not a resource bound: it admits every bucket the
# picker offers, including 5c at 60,466,176 names. What that costs is wall clock,
# not RAM, which is why the dashboard shows an estimate from the measured rate
# rather than letting anyone start a three-day run by accident. Nothing is ever
# silently sampled: a space over the cap is refused with its real size.
MAX_ENUMERATION: int = _int("MAX_ENUMERATION", 100000000)

# The largest selection the service will build in memory in order to shuffle it
# into a true random order. Above this a run must be given a cap, and the names
# are drawn at random from the whole space instead -- because the alternative is
# walking a hundred-million-name space in enumeration order, which is what made a
# run report a wall of names that all began with the same letters (the last
# character of an itertools.product varies fastest, so the first 46,656 names of a
# 5-character alphanumeric space are all `aaa??`).
SELECTION_BUILD_MAX: int = _int("SELECTION_BUILD_MAX", 2000000)
# "OG" words for the `words` pattern. The bundled wordlists/og.txt is always
# used; OG_WORDS_FILE adds (or replaces it with) a bigger list -- one word per
# line or space separated, # comments ignored -- and OG_WORDS adds inline words.
OG_WORDS_FILE: str = _str("OG_WORDS_FILE", "")
OG_WORDS: str = _str("OG_WORDS", "")
