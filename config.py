"""Runtime configuration.

Everything tunable lives here and is read from the environment, so a Railway
variable change is a redeploy away from retuning the service. No module below
this one reads os.environ directly.
"""

import os
import ssl


# One SSL context shared by every outbound client.
#
# ssl.create_default_context() parses the whole CA bundle and costs ~730 KB.
# httpx builds a fresh one per AsyncClient, so every in-flight proxy check was
# carrying ~1.25 MB of TLS context -- ~1.5 GB at 1200 concurrent checks, which
# is what got the process OOM-killed, and why a wide cycle "just errored".
# An SSLContext is safe to share across concurrent connections (each socket
# gets its own SSLSession); this turns concurrency back into a network setting
# instead of a memory setting.
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

# --- Scrape / validate cadence --------------------------------------------
# Seconds between full refresh cycles: scrape sources, re-validate, prune.
REFRESH_INTERVAL: int = _int("REFRESH_INTERVAL", 1800)
# Start the refresh loop at boot. 0 leaves it stopped until /start is called.
AUTO_START: bool = _int("AUTO_START", 1) == 1
# Run one refresh immediately at boot instead of waiting a full interval.
REFRESH_ON_START: bool = _int("REFRESH_ON_START", 1) == 1

# Max proxies handed to the validator per cycle, after de-duplication.
MAX_CANDIDATES: int = _int("MAX_CANDIDATES", 20000)
# Simultaneous in-flight proxy checks. Each check is network-bound, so this is
# the main speed lever: a cycle over N candidates takes roughly N/concurrency
# connection attempts. Each check holds up to four sockets at once (the judge
# plus three platform probes), so the process's open-file limit has to cover
# `MAX_CONCURRENCY x 4` -- which is why the server raises RLIMIT_NOFILE at boot
# (see server.py) instead of leaving the 1024 default in place. At 1024 a wide
# cycle dies with EMFILE, which the checker can only report as ConnectError --
# a "dead proxy" that is really a dead file descriptor.
MAX_CONCURRENCY: int = _int("MAX_CONCURRENCY", 1200)
# Sockets one checked proxy's client may hold. Bounds a single slow/hostile
# proxy so it cannot consume an unbounded share of the process's fd budget.
CHECK_MAX_CONNECTIONS: int = _int("CHECK_MAX_CONNECTIONS", 6)
# A proxy that fails this many consecutive checks is dropped. A proxy that has
# *never* been alive is dropped on its first failure instead (see store.prune),
# which is what keeps the second cycle small: dead scraped hosts do not come
# back to burn timeouts again.
MAX_FAILURES: int = _int("MAX_FAILURES", 2)
# Persist the store every N validated proxies during a cycle, and log progress
# at the same cadence. A cycle can run for many minutes; without these the
# process holds everything in memory until the last proxy and the dashboard
# shows nothing in the meantime.
STORE_SAVE_EVERY: int = _int("STORE_SAVE_EVERY", 200)
PROGRESS_EVERY: int = _int("PROGRESS_EVERY", 500)
# Seconds between persistence writes during a cycle. The store save serialises
# the WHOLE store to JSON, so a large store saved by check-count is what makes a
# cycle slow -- at 20k records a count-based save is ~100 full dumps. A wall
# clock between saves fixes the cost regardless of how many checks land in it.
STORE_SAVE_INTERVAL: float = _float("STORE_SAVE_INTERVAL", 10.0)
# A proxy that just failed is remembered as dead for this long, so the next
# scrape does not hand the same dead host straight back to the validator. This
# is the difference between every cycle re-checking ~20k dead hosts and a cycle
# checking only what is alive plus what is genuinely new. 0 disables it.
FAIL_COOLDOWN: int = _int("FAIL_COOLDOWN", 6 * 3600)
# Ceiling on the dead list; the oldest entries are forgotten first.
MAX_DEAD_REMEMBERED: int = _int("MAX_DEAD_REMEMBERED", 200000)

# --- Timeouts (seconds) ----------------------------------------------------
# Short on purpose. A dead proxy costs its connect timeout, and most of a free
# list is dead, so these two numbers dominate cycle time. Lower them and the
# cycle gets faster; raise them and more slow-but-alive proxies survive.
CONNECT_TIMEOUT: float = _float("CONNECT_TIMEOUT", 2.0)
READ_TIMEOUT: float = _float("READ_TIMEOUT", 5.0)
# Whole-judge timeout including connect; hard ceiling per proxy.
JUDGE_TIMEOUT: float = _float("JUDGE_TIMEOUT", 7.0)
# Ceiling on a source-list download.
SOURCE_TIMEOUT: float = _float("SOURCE_TIMEOUT", 20.0)

# A proxy slower than this scores zero on latency.
MAX_LATENCY_MS: float = _float("MAX_LATENCY_MS", 4000.0)

# --- Scoring weights (sum need not be 1; score is normalized to 0-100) -----
W_ANONYMITY: float = _float("W_ANONYMITY", 0.35)
W_LATENCY: float = _float("W_LATENCY", 0.25)
W_PLATFORM: float = _float("W_PLATFORM", 0.40)

# --- Username sniping ------------------------------------------------------
# Simultaneous name checks. Each check is one in-flight request through one
# validated proxy, so this is bounded by the pool as much as by the network.
SNIPE_CONCURRENCY: int = _int("SNIPE_CONCURRENCY", 32)
# How many times a blocked or proxy-failed name is retried on another proxy.
SNIPE_RETRIES: int = _int("SNIPE_RETRIES", 3)
# Ceiling on names accepted in one request.
SNIPE_MAX_NAMES: int = _int("SNIPE_MAX_NAMES", 200)
# How many validated proxies the sniper may rotate over.
SNIPE_POOL: int = _int("SNIPE_POOL", 400)
# How many times a proxy that already passed a platform's probe is repeated in
# that platform's rotation, so a big scan prefers proven hosts without starving
# the rest of the pool. 1 disables the preference (plain round-robin).
SNIPE_PLATFORM_WEIGHT: int = _int("SNIPE_PLATFORM_WEIGHT", 4)

# --- Username generation ---------------------------------------------------
# "OG" words for the `words` pattern. The bundled wordlists/og.txt is always
# used; OG_WORDS_FILE adds (or replaces it with) a bigger list -- one word per
# line or space separated, # comments ignored -- and OG_WORDS adds inline words.
OG_WORDS_FILE: str = _str("OG_WORDS_FILE", "")
OG_WORDS: str = _str("OG_WORDS", "")

# --- Bulk scan -------------------------------------------------------------
# A scan is the snipe loop run at throughput: many names per platform, driven
# by the same validated pool. These defaults exist to hold >=100 completed
# checks a second, which needs ~100x the per-check latency in flight.
SCAN_CONCURRENCY: int = _int("SCAN_CONCURRENCY", 256)
# Ceiling on names accepted in one scan (before the platform multiplier).
SCAN_MAX_NAMES: int = _int("SCAN_MAX_NAMES", 20000)
# A scan does not retry by default: at this width a retry costs more throughput
# than the verdict it buys, and the pool cooldown already spreads the load.
SCAN_RETRIES: int = _int("SCAN_RETRIES", 0)
# Tighter than the sniper's, because in a scan a dead proxy is pure lost time.
SCAN_CONNECT_TIMEOUT: float = _float("SCAN_CONNECT_TIMEOUT", 1.5)
SCAN_READ_TIMEOUT: float = _float("SCAN_READ_TIMEOUT", 4.0)
# Simultaneous requests per proxy during a scan.
SCAN_PER_PROXY: int = _int("SCAN_PER_PROXY", 6)
# A proxy whose checks fail this many times within one run is taken out of the
# rotation for the rest of the run, not just rested: a free list has plenty of
# hosts that answered one probe then died, and continuing to hand those out is
# where a wall of ConnectErrors comes from.
SNIPE_PROXY_FAIL_LIMIT: int = _int("SNIPE_PROXY_FAIL_LIMIT", 6)
# Cap on the number of simultaneous snipe/scan runs the server will track. The
# dashboard only ever starts one, but the API is open -- unbounded runs are
# unbounded work.
MAX_CONCURRENT_RUNS: int = _int("MAX_CONCURRENT_RUNS", 8)
# The rate a scan aims for; reported against in /scan and on the dashboard.
SCAN_TARGET_RATE: int = _int("SCAN_TARGET_RATE", 100)
# A proxy that blocks or errors is rested this long, so a retry lands on a
# different host instead of the one that just refused the name. 0 disables it.
SNIPE_PROXY_COOLDOWN: float = _float("SNIPE_PROXY_COOLDOWN", 60.0)
# Simultaneous requests allowed through any single proxy. The sniper keeps one
# warm client per proxy, so this is the client's own connection-pool ceiling.
SNIPE_PER_PROXY: int = _int("SNIPE_PER_PROXY", 4)
# Sniper timeouts. A validated proxy is known to answer, so these are tighter
# than the validator's: the point is to fail over to the next proxy quickly.
SNIPE_CONNECT_TIMEOUT: float = _float("SNIPE_CONNECT_TIMEOUT", 3.0)
SNIPE_READ_TIMEOUT: float = _float("SNIPE_READ_TIMEOUT", 8.0)
# Seconds a warm proxy pool (and its connections) is reused across requests
# before it is rebuilt from the store. Reuse is what skips the handshake.
SNIPE_POOL_TTL: int = _int("SNIPE_POOL_TTL", 300)

# --- Pool health -----------------------------------------------------------
# Below this many alive proxies a scan cannot produce verdicts -- every check
# comes back an error -- so a refresh is queued whenever a request finds the
# pool this thin, and the refresh loop shortens its wait to LOW_POOL_INTERVAL
# until the pool recovers. A deploy with no mounted volume starts from an empty
# store, which is exactly when this matters.
MIN_ALIVE: int = _int("MIN_ALIVE", 15)
LOW_POOL_INTERVAL: int = _int("LOW_POOL_INTERVAL", 300)

# --- Persistence -----------------------------------------------------------
# Point STORE_PATH at a Railway volume mount for durability across deploys.
STORE_PATH: str = _str("STORE_PATH", "data/proxies.json")
# How long a proxy may go unvalidated before it is dropped, in seconds.
STALE_AFTER: int = _int("STALE_AFTER", 3 * REFRESH_INTERVAL)

# --- Judge ----------------------------------------------------------------
# Returns the client IP as seen by the target plus the request headers it
# received -- one request yields both exit IP and forwarding-header evidence.
JUDGE_URL: str = _str("JUDGE_URL", "https://httpbin.org/get")
# Tried in rotation. One httpbin instance starts failing roughly 40% of
# requests once ~400 are in flight at once, and a failed judge request is
# indistinguishable from a dead proxy -- it reads as "nothing is alive".
JUDGE_FALLBACKS: list[str] = [
    u.strip()
    for u in _str(
        "JUDGE_FALLBACKS",
        # More mirrors, so the judge pool is not the validator's ceiling: with
        # 4 endpoints x JUDGE_PER_ENDPOINT the semaphores, not the network, set
        # how many proxies can be checked at once.
        "https://httpbingo.org/get,https://eu.httpbin.org/get,https://postman-echo.com/get,"
        "https://httpbin.org/headers,https://postman-echo.com/headers",
    ).split(",")
    if u.strip()
]
# The pool actually used: primary first, duplicates dropped.
JUDGE_URLS: list[str] = list(dict.fromkeys([JUDGE_URL, *JUDGE_FALLBACKS]))
# Simultaneous judge requests allowed against any single endpoint. Measured:
# 50 in flight answers cleanly, 400 loses ~40% of requests to connect timeouts,
# so this stays well below the knee.
JUDGE_PER_ENDPOINT: int = _int("JUDGE_PER_ENDPOINT", 90)
