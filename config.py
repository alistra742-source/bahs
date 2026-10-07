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


# --- Service ---------------------------------------------------------------
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

# --- Alerts ----------------------------------------------------------------
# A Discord webhook told which names came back free. Empty disables alerting.
# The default template matches the one every tool in this space ships with.
# Where the UI's overrides for the alert settings are kept. The environment
# stays the base, so the service runs from Railway variables alone and this file
# only ever holds what someone changed by hand.
SETTINGS_PATH: str = _str("SETTINGS_PATH", "data/settings.json")
ALERT_WEBHOOK: str = _str("ALERT_WEBHOOK", "")
ALERT_TEMPLATE: str = _str("ALERT_TEMPLATE", "{platform} username available: `{username}`")
# Posted as the webhook's own name/avatar when set.
ALERT_USERNAME: str = _str("ALERT_USERNAME", "bahs")
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
# to the same host.
SNIPE_PER_PROXY_MAX: int = _int("SNIPE_PER_PROXY_MAX", 64)
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
# A proxy that fails this many times in one run is taken out of the rotation
# for the rest of the run: hosts that answered once and then died are where a
# wall of ConnectErrors comes from.
SNIPE_PROXY_FAIL_LIMIT: int = _int("SNIPE_PROXY_FAIL_LIMIT", 6)
# Seconds the warm pool is reused across requests before it is rebuilt, so
# consecutive batches skip their handshakes.
SNIPE_POOL_TTL: int = _int("SNIPE_POOL_TTL", 300)
# When nothing in the pool can be handed out -- every proxy resting because the
# platform just refused it -- a run waits this long for the soonest one to come
# back before giving up and saying why. Only *resting* proxies are waited for;
# a list that is retired in full ends the run immediately.
POOL_WAIT_MAX: float = _float("POOL_WAIT_MAX", 10.0)
# Consecutive empty waits before the run stops. A transient throttle recovers on
# the first or second wait; a global rate limit does not recover at all, and
# without this the run would sit there trading waits with the platform forever
# while its remaining names never get dispatched.
POOL_EMPTY_STREAK: int = _int("POOL_EMPTY_STREAK", 3)

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
# A scan does not retry by default: at this width a retry costs more throughput
# than the verdict it buys. It matters more than it looks: every retry of a
# blocked name spends another slot of the pool's block budget, so retrying a
# blanket rate limit is what used to exhaust a list fastest.
SCAN_RETRIES: int = _int("SCAN_RETRIES", 0)
# Tighter than the check above, because in a scan a dead proxy is pure lost
# time.
SCAN_CONNECT_TIMEOUT: float = _float("SCAN_CONNECT_TIMEOUT", 2.0)
SCAN_READ_TIMEOUT: float = _float("SCAN_READ_TIMEOUT", 6.0)
# Floor for the per-proxy connection pool during a scan; the real number is
# ceil(concurrency / pool size), as above.
SCAN_PER_PROXY: int = _int("SCAN_PER_PROXY", 6)
# The rate a scan aims for; reported against in /scan and on the dashboard.
SCAN_TARGET_RATE: int = _int("SCAN_TARGET_RATE", 100)

# --- Platform throttling ---------------------------------------------------
# A platform that has refused every proxy in the list is sat out for this long
# rather than hammered. A blanket 429 is per platform: discord refusing the whole
# pool says nothing about tiktok, and a run that stops entirely -- or that keeps
# spending proxies on discord -- is spending the list on a question that already
# has an answer. When the platform's 429 names a retry_after, that wins, up to
# the ceiling.
SNIPE_PLATFORM_PAUSE: float = _float("SNIPE_PLATFORM_PAUSE", 90.0)
SNIPE_PLATFORM_PAUSE_MAX: float = _float("SNIPE_PLATFORM_PAUSE_MAX", 900.0)

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
# "OG" words for the `words` pattern. The bundled wordlists/og.txt is always
# used; OG_WORDS_FILE adds (or replaces it with) a bigger list -- one word per
# line or space separated, # comments ignored -- and OG_WORDS adds inline words.
OG_WORDS_FILE: str = _str("OG_WORDS_FILE", "")
OG_WORDS: str = _str("OG_WORDS", "")
