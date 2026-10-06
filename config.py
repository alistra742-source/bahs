"""Runtime configuration.

Everything tunable lives here and is read from the environment, so a Railway
variable change is a redeploy away from retuning the service. No module below
this one reads os.environ directly.
"""

import os


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
# plus three platform probes), so raise this with an eye on the process's
# open-file limit -- 800 is ~3200 sockets at peak.
MAX_CONCURRENCY: int = _int("MAX_CONCURRENCY", 800)
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
CONNECT_TIMEOUT: float = _float("CONNECT_TIMEOUT", 2.5)
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
        "https://httpbingo.org/get,https://eu.httpbin.org/get,https://postman-echo.com/get",
    ).split(",")
    if u.strip()
]
# The pool actually used: primary first, duplicates dropped.
JUDGE_URLS: list[str] = list(dict.fromkeys([JUDGE_URL, *JUDGE_FALLBACKS]))
# Simultaneous judge requests allowed against any single endpoint. Measured:
# 50 in flight answers cleanly, 400 loses ~40% of requests to connect timeouts,
# so this stays well below the knee.
JUDGE_PER_ENDPOINT: int = _int("JUDGE_PER_ENDPOINT", 60)
