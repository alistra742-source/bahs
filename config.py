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

# Optional bearer key. Unset means the read API is open (status/health always are).
API_KEY: str = _str("API_KEY", "")

# --- Scrape / validate cadence --------------------------------------------
# Seconds between full refresh cycles: scrape sources, re-validate, prune.
REFRESH_INTERVAL: int = _int("REFRESH_INTERVAL", 1800)
# Start the refresh loop at boot. 0 leaves it stopped until /start is called.
AUTO_START: bool = _int("AUTO_START", 1) == 1
# Run one refresh immediately at boot instead of waiting a full interval.
REFRESH_ON_START: bool = _int("REFRESH_ON_START", 1) == 1

# Max proxies handed to the validator per cycle, after de-duplication.
MAX_CANDIDATES: int = _int("MAX_CANDIDATES", 20000)
# Simultaneous in-flight proxy checks. Each check is cheap but network-bound.
MAX_CONCURRENCY: int = _int("MAX_CONCURRENCY", 250)
# A proxy that fails this many consecutive checks is dropped from the store.
MAX_FAILURES: int = _int("MAX_FAILURES", 3)

# --- Timeouts (seconds) ----------------------------------------------------
CONNECT_TIMEOUT: float = _float("CONNECT_TIMEOUT", 5.0)
READ_TIMEOUT: float = _float("READ_TIMEOUT", 8.0)
# Whole-judge timeout including connect; hard ceiling per proxy.
JUDGE_TIMEOUT: float = _float("JUDGE_TIMEOUT", 12.0)
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
# Fallback judges tried in order if the primary is unreachable directly.
JUDGE_FALLBACKS: list[str] = [
    u.strip()
    for u in _str(
        "JUDGE_FALLBACKS",
        "https://httpbin.org/headers,https://postman-echo.com/get",
    ).split(",")
    if u.strip()
]
