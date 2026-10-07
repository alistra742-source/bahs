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
# How many stored proxies one run may rotate over. The pool walks the list from
# a cursor, so this is a window on the list, not a truncation of it.
SNIPE_POOL: int = _int("SNIPE_POOL", 5000)

# --- Timeouts (seconds) ----------------------------------------------------
# Short on purpose. A dead proxy costs its connect timeout and pasted lists are
# mostly dead, so these two numbers dominate how fast a run moves. Lower them
# and the run gets faster; raise them and more slow-but-alive hosts survive.
CONNECT_TIMEOUT: float = _float("CONNECT_TIMEOUT", 3.0)
READ_TIMEOUT: float = _float("READ_TIMEOUT", 8.0)

# --- One-off check ---------------------------------------------------------
# Simultaneous checks for POST /snipe. Each check is one in-flight request
# through one proxy, so this is bounded by the list as much as by the network.
SNIPE_CONCURRENCY: int = _int("SNIPE_CONCURRENCY", 64)
# How many times a blocked or proxy-failed name is retried on another proxy.
SNIPE_RETRIES: int = _int("SNIPE_RETRIES", 2)
# Ceiling on names accepted in one /snipe request (before the platform
# multiplier).
SNIPE_MAX_NAMES: int = _int("SNIPE_MAX_NAMES", 20000)
# Simultaneous requests allowed through any single proxy: the ceiling on that
# proxy's own connection pool.
SNIPE_PER_PROXY: int = _int("SNIPE_PER_PROXY", 4)
# Warm clients kept open at once. One client per proxy is what makes a batch
# fast (the CONNECT tunnel and the TLS handshake are paid once per proxy), but
# keeping one for every proxy in a 100k-line list would cost ~1 GB of buffers
# and an fd per socket. Past this many, the least recently used client is
# closed and re-created if its proxy comes round again.
SNIPE_MAX_CLIENTS: int = _int("SNIPE_MAX_CLIENTS", 512)
# A proxy that blocks or errors is rested this long, so a retry lands on a
# different host instead of the one that just refused. 0 disables it.
SNIPE_PROXY_COOLDOWN: float = _float("SNIPE_PROXY_COOLDOWN", 60.0)
# A proxy that fails this many times in one run is taken out of the rotation
# for the rest of the run: hosts that answered once and then died are where a
# wall of ConnectErrors comes from.
SNIPE_PROXY_FAIL_LIMIT: int = _int("SNIPE_PROXY_FAIL_LIMIT", 6)
# Seconds the warm pool is reused across requests before it is rebuilt, so
# consecutive batches skip their handshakes.
SNIPE_POOL_TTL: int = _int("SNIPE_POOL_TTL", 300)

# --- Bulk scan -------------------------------------------------------------
# A scan is the same loop at throughput: many names per platform.
SCAN_CONCURRENCY: int = _int("SCAN_CONCURRENCY", 256)
# Ceiling on names accepted in one scan (before the platform multiplier).
SCAN_MAX_NAMES: int = _int("SCAN_MAX_NAMES", 20000)
# A scan does not retry by default: at this width a retry costs more throughput
# than the verdict it buys.
SCAN_RETRIES: int = _int("SCAN_RETRIES", 0)
# Tighter than the check above, because in a scan a dead proxy is pure lost
# time.
SCAN_CONNECT_TIMEOUT: float = _float("SCAN_CONNECT_TIMEOUT", 2.0)
SCAN_READ_TIMEOUT: float = _float("SCAN_READ_TIMEOUT", 6.0)
# Simultaneous requests per proxy during a scan.
SCAN_PER_PROXY: int = _int("SCAN_PER_PROXY", 6)
# The rate a scan aims for; reported against in /scan and on the dashboard.
SCAN_TARGET_RATE: int = _int("SCAN_TARGET_RATE", 100)

# --- Runs ------------------------------------------------------------------
# Cap on the simultaneous snipe/scan runs the server will track. The dashboard
# only ever starts one, but the API is open -- unbounded runs are unbounded
# work.
MAX_CONCURRENT_RUNS: int = _int("MAX_CONCURRENT_RUNS", 8)
# Below this many usable proxies a run cannot answer much, so the response says
# so instead of showing a table of errors.
MIN_POOL_WARN: int = _int("MIN_POOL_WARN", 5)

# --- Username generation ---------------------------------------------------
# "OG" words for the `words` pattern. The bundled wordlists/og.txt is always
# used; OG_WORDS_FILE adds (or replaces it with) a bigger list -- one word per
# line or space separated, # comments ignored -- and OG_WORDS adds inline words.
OG_WORDS_FILE: str = _str("OG_WORDS_FILE", "")
OG_WORDS: str = _str("OG_WORDS", "")
