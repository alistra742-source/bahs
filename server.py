"""HTTP surface for bahs.

The dashboard is served at ``/``; the JSON API sits beside it. Proxies are the
user's: they are pasted or uploaded, stored, and rotated over by the sniper.
Nothing is scraped and nothing is validated ahead of time, so a run never waits
on anything but the list that was handed to it.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from alerts import AlertSettings, Alerter

import alerts
import generator
import proxies as proxy_list_module
import sniper
from config import (
    LOG_LEVEL,
    MAX_CONCURRENT_RUNS,
    MAX_ENUMERATION,
    MIN_POOL_WARN,
    PORT,
    SCAN_BUFFER_MAX,
    SCAN_CONCURRENCY,
    SCAN_CONNECT_TIMEOUT,
    SCAN_MAX_NAMES,
    SCAN_PER_PROXY,
    SCAN_READ_TIMEOUT,
    SCAN_RETRIES,
    SCAN_TARGET_RATE,
    SNIPE_CONCURRENCY,
    SNIPE_MAX_NAMES,
    SNIPE_PER_PROXY,
    SNIPE_PER_PROXY_MAX,
    SNIPE_POOL,
    SNIPE_POOL_TTL,
    SNIPE_PROXY_COOLDOWN,
    SNIPE_RETRIES,
    STORE_PATH,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs one INFO line per request and a run issues thousands of them, which
# buries the progress lines that matter. Errors still surface at WARNING.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("bahs.server")


def _raise_fd_limit() -> int | None:
    """Lift RLIMIT_NOFILE so a wide run cannot hit EMFILE.

    Every in-flight check owns a socket (and every warm client a small pool of
    them), so the 1024 default is a few hundred concurrent checks away from
    "Too many open files" -- which the sniper can only report as a proxy error,
    i.e. a "dead proxy" that is really a dead file descriptor. The container's
    hard limit is the ceiling; if it is already high enough this is a no-op.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard == resource.RLIM_INFINITY:
            target = 65536
        else:
            target = min(hard, 65536)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            return target
        return soft
    except (ValueError, OSError):
        log.debug("could not raise RLIMIT_NOFILE", exc_info=True)
        return None


_RAISED_FD_LIMIT = _raise_fd_limit()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

proxy_list = proxy_list_module.ProxyList(STORE_PATH)

# Discord alerts. The environment is the base and the UI can override it, so
# this is loaded at boot and saved only when someone changes something by hand.
alert_settings = AlertSettings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    proxy_list.load()
    alert_settings.load()
    log.info(
        "bahs up on :%d, %d stored proxies, nofile=%s",
        PORT,
        len(proxy_list),
        _RAISED_FD_LIMIT if _RAISED_FD_LIMIT is not None else "unchanged",
    )
    try:
        yield
    finally:
        await _close_pools()
        proxy_list.save()


app = FastAPI(title="bahs", version="3.0.0", lifespan=lifespan)


# --- site + info ----------------------------------------------------------
@app.get("/", include_in_schema=False)
def site() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/info")
def info() -> dict:
    words = generator.words_by_length()
    return {
        "service": "bahs",
        "platforms": list(sniper.PLATFORMS),
        "alerts_enabled": bool(alert_settings.get().get("enabled")),
        # What the UI needs to show a running total for "every name of this
        # length" before anyone starts a run.
        "generation": {
            "charset": {name: len(chars) for name, chars in generator.CHARSETS.items()},
            "words_by_length": words,
            "max_enumeration": MAX_ENUMERATION,
            "note": "limit omitted or 0 means every name of that length",
        },
        "endpoints": {
            "GET /": "dashboard",
            "GET /health": "counts: stored proxies, live runs",
            "GET /proxies": "the stored proxy list (add ?format=txt for one per line)",
            "POST /proxies": "save pasted proxies - {text, mode: append|replace}",
            "POST /proxies/upload": "the same, with the list as a raw text request body",
            "DELETE /proxies": "remove one proxy by ?proxy=",
            "POST /proxies/clear": "empty the list",
            "POST /snipe": "check usernames on every platform (JSON, or NDJSON with stream=true)",
            "GET /snipe": "the same for one ?username=",
            "POST /snipe/stop": "stop every in-flight /snipe batch",
            "POST /scan": "generate and check in bulk (NDJSON with stream=true)",
            "POST /scan/stop": "stop every in-flight /scan",
            "GET /runs": "in-flight and just-finished runs",
            "POST /runs/stop": "stop all runs, or the one named by ?run_id=",
            "GET /generate": "candidate usernames: letters | alnum | numbers | words, by length",
            "POST /generate": "the same for several patterns at once",
            "GET /claim": "the registration route for a name on a platform",
            "GET /settings": "alert settings (webhook url, template, ping)",
            "POST /settings": "change the alert settings",
            "POST /settings/test-webhook": "post one test message to the webhook",
        },
    }


@app.get("/health")
def health() -> dict:
    runs = _runs_status()
    return {
        "status": "ok",
        "proxies": proxy_list.stats(),
        "runs": {"active": sum(1 for r in runs if r["active"]), "tracked": len(runs)},
        "platforms": list(sniper.PLATFORMS),
    }


# --- the proxy list -------------------------------------------------------
@app.get("/proxies")
def get_proxies(
    limit: int = Query(1000, ge=1, le=100000),
    offset: int = Query(0, ge=0),
    format: str = Query("json", pattern="^(json|txt)$"),
) -> object:
    """The stored list, paged for the table and complete for the download."""
    stored = proxy_list.all()
    if format == "txt":
        return PlainTextResponse("\n".join(stored))
    window = stored[offset : offset + limit]
    return {
        "count": len(stored),
        "offset": offset,
        "returned": len(window),
        "proxies": window,
        "stats": proxy_list.stats(),
    }


def _store_paste(text: str, mode: str) -> dict:
    report = proxy_list_module.parse_many(text)
    if not report.accepted:
        raise HTTPException(
            400,
            "no usable proxies in that input"
            + (
                f" (rejected {len(report.rejected)} lines, e.g. {report.rejected[0]!r})"
                if report.rejected
                else ""
            ),
        )
    if mode == "replace":
        stored = proxy_list.replace(report.accepted)
        proxy_list.save()
        return {
            "mode": "replace",
            "added": stored,
            "duplicates": report.duplicates,
            "rejected": report.rejected,
            "rejected_count": report.rejected_total,
            "stored": proxy_list.stats(),
        }
    added, already = proxy_list.add(report.accepted)
    proxy_list.save()
    return {
        "mode": "append",
        "added": added,
        "duplicates": report.duplicates + already,
        "rejected": report.rejected,
        "rejected_count": report.rejected_total,
        "stored": proxy_list.stats(),
    }


class ProxyPaste(BaseModel):
    """A blob of pasted proxies, one per line (other separators work too)."""

    text: str = ""
    mode: str = Field("append", pattern="^(append|replace)$")


@app.post("/proxies")
def save_proxies(paste: ProxyPaste) -> dict:
    """Parse a paste, store it, and report exactly what was kept and dropped."""
    return _store_paste(paste.text, paste.mode)


@app.post("/proxies/upload")
async def upload_proxies(
    request: Request, mode: str = Query("append", pattern="^(append|replace)$")
) -> dict:
    """The same, taking the list as the raw request body.

    ``curl -X POST --data-binary @proxies.txt .../proxies/upload`` -- no
    multipart, because the input is one text file and this keeps the surface
    and the dependency list small.
    """
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    return _store_paste(body.decode("utf-8", errors="replace"), mode)


@app.delete("/proxies")
def remove_proxy(proxy: str = Query(..., min_length=1)) -> dict:
    if not proxy_list.remove(proxy):
        raise HTTPException(404, "proxy not in the list")
    proxy_list.save()
    return {"removed": proxy, "stored": proxy_list.stats()}


@app.post("/proxies/clear")
def clear_proxies() -> dict:
    removed = proxy_list.clear()
    proxy_list.save()
    return {"removed": removed, "stored": proxy_list.stats()}


# --- run control ----------------------------------------------------------
class RunHandle:
    """One in-flight snipe or scan, cancellable from another HTTP request.

    A streaming endpoint holds one of these. Setting ``stop`` makes the generator
    stop dispatching, cancel its in-flight checks and finish, so ``/runs/stop``
    is a real halt rather than the client merely closing its socket. Every
    mutation happens on the event loop with no awaits between read and write, so
    no lock is needed.
    """

    __slots__ = ("id", "kind", "stop", "started", "finished", "checked")

    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.stop = asyncio.Event()
        self.started = time.time()
        self.finished: float | None = None
        self.checked = 0

    @property
    def active(self) -> bool:
        return self.finished is None

    def finish(self) -> None:
        if self.finished is None:
            self.finished = time.time()

    def status(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "started": self.started,
            "finished": self.finished,
            "checked": self.checked,
            "stopping": self.stop.is_set(),
            "active": self.active,
            "elapsed_s": round((self.finished or time.time()) - self.started, 2),
        }


_runs: dict[str, RunHandle] = {}


def _reap_runs() -> None:
    for run_id in [rid for rid, handle in _runs.items() if not handle.active]:
        _runs.pop(run_id, None)


def _open_run(kind: str) -> RunHandle:
    """Register a run, refusing once ``MAX_CONCURRENT_RUNS`` are in flight."""
    _reap_runs()
    if len(_runs) >= MAX_CONCURRENT_RUNS:
        raise HTTPException(429, f"too many runs in flight (max {MAX_CONCURRENT_RUNS})")
    handle = RunHandle(kind)
    _runs[handle.id] = handle
    return handle


def _stop_runs(kind: str | None = None, run_id: str | None = None) -> list[str]:
    """Signal stop to the runs matching ``kind``/``run_id``; return their ids."""
    stopped: list[str] = []
    for rid, handle in _runs.items():
        if not handle.active:
            continue
        if run_id and rid != run_id:
            continue
        if kind and handle.kind != kind:
            continue
        handle.stop.set()
        stopped.append(rid)
    return stopped


def _runs_status() -> list[dict]:
    _reap_runs()
    return [handle.status() for handle in _runs.values()]


@app.get("/runs")
def runs_status() -> dict:
    """Every tracked run, active or just finished."""
    runs = _runs_status()
    return {"count": len(runs), "runs": runs, "max": MAX_CONCURRENT_RUNS}


@app.post("/runs/stop")
def stop_all_runs(run_id: str | None = Query(None)) -> dict:
    """Stop every run, or the one named by ``?run_id=``."""
    stopped = _stop_runs(run_id=run_id)
    return {"stopped": stopped, "runs": _runs_status()}


@app.post("/snipe/stop")
def stop_snipe() -> dict:
    """Stop every in-flight /snipe batch."""
    stopped = _stop_runs(kind="snipe")
    return {"stopped": stopped, "runs": _runs_status()}


@app.post("/scan/stop")
def stop_scan() -> dict:
    """Stop every in-flight /scan."""
    stopped = _stop_runs(kind="scan")
    return {"stopped": stopped, "runs": _runs_status()}


# --- the pool -------------------------------------------------------------
# One warm pool per mode, shared by every request, so the second batch of names
# skips the handshakes the first one paid. Rebuilt when the stored list changes
# (the version counter) or the pool goes stale. "scan" gets its own, with
# tighter timeouts and wider per-proxy concurrency, so a scan never
# reconfigures the pool a single check is using.
_pool_state: dict[str, dict[str, object]] = {
    "snipe": {"pool": None, "built": 0.0, "version": -1},
    "scan": {"pool": None, "built": 0.0, "version": -1},
}
_pool_lock = asyncio.Lock()


def _build_pool(scan: bool, concurrency: int) -> sniper.ProxyPool:
    """Pool the stored proxies with a connection budget that can carry it.

    The per-proxy connection count is derived from ``concurrency / len(list)``
    rather than fixed, because a fixed one is a hard throughput ceiling for a
    short list: with one rotating endpoint, asking for 256 concurrent checks
    still got however many connections the fixed number allowed. A long list
    still works out to the floor, so this only ever helps.
    """
    stored = proxy_list.all()[:SNIPE_POOL]
    floor = SCAN_PER_PROXY if scan else SNIPE_PER_PROXY
    per_proxy = sniper.per_proxy_connections(
        len(stored), concurrency, floor, SNIPE_PER_PROXY_MAX
    )
    if not scan:
        return sniper.ProxyPool(stored, cooldown=SNIPE_PROXY_COOLDOWN, per_proxy=per_proxy)
    timeout = httpx.Timeout(
        connect=SCAN_CONNECT_TIMEOUT,
        read=SCAN_READ_TIMEOUT,
        write=SCAN_READ_TIMEOUT,
        pool=SCAN_READ_TIMEOUT,
    )
    return sniper.ProxyPool(stored, per_proxy=per_proxy, timeout=timeout)


async def _pool(mode: str, concurrency: int) -> sniper.ProxyPool:
    scan = mode == "scan"
    floor = SCAN_PER_PROXY if scan else SNIPE_PER_PROXY
    async with _pool_lock:
        state = _pool_state[mode]
        pool = state["pool"]
        age = time.time() - float(state["built"])  # type: ignore[arg-type]
        fresh_list = state["version"] == proxy_list.version
        # Reuse only when this pool's connection budget is already the one this
        # concurrency needs. Rebuilding on every request would throw away the
        # warm tunnels that make a batch fast.
        if (
            isinstance(pool, sniper.ProxyPool)
            and len(pool)
            and fresh_list
            and age < SNIPE_POOL_TTL
            and pool.per_proxy
            == sniper.per_proxy_connections(len(pool), concurrency, floor, SNIPE_PER_PROXY_MAX)
        ):
            return pool
        new_pool = _build_pool(scan, concurrency)
        if isinstance(pool, sniper.ProxyPool) and pool is not new_pool:
            # Retire the old connections in the background; nobody waits on it.
            asyncio.create_task(pool.aclose())
        state["pool"] = new_pool
        state["built"] = time.time()
        state["version"] = proxy_list.version
        return new_pool


async def _close_pools() -> None:
    for state in _pool_state.values():
        pool = state["pool"]
        if isinstance(pool, sniper.ProxyPool):
            await pool.aclose()
        state["pool"] = None


async def _prepare_pool(mode: str, concurrency: int) -> tuple[sniper.ProxyPool, str]:
    """The pool for a run, plus a note when the list is too thin to be useful.

    With an empty list every check comes back an error and the caller just sees
    a table of nothing, so that is a 503 with the reason rather than a run that
    quietly fails. A short list still runs, but says so up front.
    """
    pool = await _pool(mode, concurrency)
    if len(pool) == 0:
        raise HTTPException(503, "no proxies saved yet -- paste or upload a list first")
    if len(pool) < MIN_POOL_WARN:
        return pool, (
            f"only {len(pool)} proxies in the list -- a run this narrow will be slow "
            "and will repeat the same few hosts"
        )
    # Measured on one proxy endpoint: 32 concurrent checks ran at 147/s while the
    # same work at 256 concurrent ran at 65/s. Widening past what a short list can
    # carry makes a run slower, not faster, so say so instead of letting the
    # concurrency box look like a speed dial.
    capacity = len(pool) * pool.per_proxy
    if concurrency > capacity:
        return pool, (
            f"{len(pool)} proxies at {pool.per_proxy} connections each can carry about "
            f"{capacity} checks at once, and {concurrency} were asked for -- the extra "
            "ones queue. More proxies helps here; more concurrency does not."
        )
    return pool, ""


# --- username checks ------------------------------------------------------
class SnipeRequest(BaseModel):
    """A batch of names, or a single one, against one or more platforms."""

    usernames: list[str] = Field(default_factory=list)
    username: str | None = None
    platforms: list[str] | None = None
    concurrency: int | None = None
    retries: int | None = None
    # Stream each verdict as its own NDJSON line instead of buffering the batch.
    stream: bool = False


def _verdict_report(answered: int, checked: int) -> dict:
    """How many checks actually answered the question about the name.

    ``checked`` counts results; ``answered`` counts the ones that came back with
    a verdict (available/taken/invalid). Everything else is blocked or errored,
    which says something about the proxy and the platform and nothing at all
    about the name -- so a run that reports 20,000 checks and 40 answers is not
    a run that checked 20,000 names.
    """
    return {
        "answered": answered,
        "no_verdict": checked - answered,
        "answer_rate": round(answered / checked, 4) if checked else 0.0,
    }


def _names_of(req: SnipeRequest) -> list[str]:
    names: list[str] = []
    for raw in [*req.usernames, req.username or ""]:
        name = (raw or "").strip().lstrip("@")
        if name and name not in names:
            names.append(name)
    if not names:
        raise HTTPException(400, "give at least one username")
    if len(names) > SNIPE_MAX_NAMES:
        raise HTTPException(400, f"at most {SNIPE_MAX_NAMES} names per request")
    return names


class _Tally:
    """Live counters for one run -- the numbers the dashboard shows.

    ``available / occupied / errors / rate limits / proxy misses`` is the set
    every tool in this space puts on a dashboard, and the per-platform split is
    what makes a run legible: a global "40 errors" hides that they are all one
    host, which is the difference between a dead proxy list and a platform
    throttling its share of the run.
    """

    # "unanswered" is its own column on purpose: an error is the transport
    # failing and is evidence about the proxy list, while an answer nobody could
    # read is evidence about the platform. Folding them together is how a run
    # that was limping on a bad host looked like the proxies were exhausted.
    _ZERO = {
        "checked": 0, "available": 0, "taken": 0, "invalid": 0,
        "blocked": 0, "error": 0, "unanswered": 0,
    }

    def __init__(
        self, names: int, platforms: list[str], alerter: Alerter | None = None
    ) -> None:
        self.names = names
        self.platforms = platforms
        # A run costs names x platforms, not names. Reporting progress against
        # the name count alone showed 0% on a run that was a third of the way
        # through, because every name is checked once per platform.
        self.total_checks = max(1, names * max(1, len(platforms)))
        self.alerter = alerter
        self.meter = sniper.RateMeter()
        self.checked = 0
        self.available = 0
        self.answered = 0
        self.rate_limits = 0
        self.proxy_misses = 0
        self.by_status: dict[str, int] = {}
        self.by_platform: dict[str, dict[str, int]] = {p: dict(self._ZERO) for p in platforms}
        self.current = ""

    def add(self, result: sniper.SnipeResult) -> None:
        self.meter.tick()
        self.checked += 1
        self.current = result.username
        status = result.status
        self.by_status[status] = self.by_status.get(status, 0) + 1
        row = self.by_platform.setdefault(result.platform, dict(self._ZERO))
        row["checked"] += 1
        if status in row:
            row[status] += 1
        if status == "available":
            self.available += 1
            # Queued, not sent: the alerter digests and posts on its own clock.
            if self.alerter is not None:
                self.alerter.offer(result.username, result.platform, result.detail)
        if result.answered:
            self.answered += 1
        if status == "blocked":
            self.rate_limits += 1
        if result.detail in sniper.POOL_MISS_DETAILS:
            self.proxy_misses += 1

    @property
    def elapsed(self) -> float:
        return self.meter.elapsed

    def progress(self) -> dict:
        """The per-tick line: everything the dashboard moves while a run is live."""
        return {
            "checked": self.checked,
            "names": self.names,
            "total_checks": self.total_checks,
            "platforms_count": len(self.platforms),
            "progress": round(self.checked / self.total_checks, 4),
            "per_second": round(self.meter.recent, 1),
            "average_per_second": round(self.meter.average, 1),
            "available_count": self.available,
            "rate_limits": self.rate_limits,
            "proxy_misses": self.proxy_misses,
            "current_target": self.current,
            "elapsed_s": round(self.elapsed, 2),
            "by_platform": self.by_platform,
            **_verdict_report(self.answered, self.checked),
        }

    def summary(self) -> dict:
        return {
            "by_status": self.by_status,
            "by_platform": self.by_platform,
            "checked": self.checked,
            # Without this the dashboard's bar and percentage reset to zero on
            # the final line, which is the one everybody looks at.
            "names": self.names,
            "total_checks": self.total_checks,
            "progress": round(self.checked / self.total_checks, 4),
            "available_count": self.available,
            "rate_limits": self.rate_limits,
            "proxy_misses": self.proxy_misses,
            "elapsed_s": round(self.elapsed, 2),
            **_verdict_report(self.answered, self.checked),
        }


def _snipe_row(result: sniper.SnipeResult) -> dict:
    return {
        "username": result.username,
        "platform": result.platform,
        "status": result.status,
        "detail": result.detail,
        "proxy": result.proxy,
        "latency_ms": round(result.latency_ms, 2) if result.latency_ms is not None else None,
    }


def _pool_report(pool: sniper.ProxyPool) -> dict:
    return {
        "pool_size": len(pool),
        "pool_alive": pool.alive(),
        "pool_resting": pool.resting(),
        "pool_retired": pool.retired(),
        "pool_blocked": pool.blocked(),
        "per_proxy_connections": pool.per_proxy,
        # Requests the proxies carried and how many failed at the transport
        # level. This is what tells "your list is bad" apart from "the names are
        # taken" -- the errors column alone cannot, which is how a run that was
        # limping on one flaky host looked like the names were the problem.
        "proxy_attempts": pool.attempts(),
        "proxy_failures": pool.failures(),
        # Set only when the run stopped early because of the pool rather than
        # because it ran out of names -- "the platform throttled every proxy" is
        # a completely different message from "your list is dead".
        "stop_reason": pool.stop_reason,
        # Platforms sat out because they refused the whole list, so a throttle is
        # visible per platform instead of hidden in the blocked count.
        "platforms_paused": pool.paused(),
        "skipped": pool.skipped,
    }


async def _snipe_ready(
    req: SnipeRequest,
) -> tuple[list[str], list[str], sniper.ProxyPool, int, int, str]:
    """Validate the request and fetch the pool before anything streams."""
    names = _names_of(req)
    platforms = sniper.normalize_platforms(req.platforms)
    concurrency = max(1, req.concurrency or SNIPE_CONCURRENCY)
    retries = SNIPE_RETRIES if req.retries is None else max(0, req.retries)
    pool, note = await _prepare_pool("snipe", concurrency)
    return names, platforms, pool, concurrency, retries, note


def _alert_report(alerter: Alerter | None) -> dict:
    if alerter is None or not alerter.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "sent": alerter.sent,
        "failed": alerter.failed,
        "dropped": alerter.dropped,
    }


async def _flush_alerts(alerter: Alerter | None, force: bool = False) -> None:
    """Push queued alerts. A webhook that is down must never touch the run."""
    if alerter is None or not alerter.enabled:
        return
    if not force and alerter.pending < int(alerter.settings.get("batch") or 10):
        return
    try:
        await alerter.flush(force=force)
    except Exception as exc:  # pragma: no cover - a webhook is not a dependency
        log.warning("alert flush failed: %s", type(exc).__name__)


async def _finish_alerts(alerter: Alerter | None) -> None:
    try:
        await _flush_alerts(alerter, force=True)
    finally:
        if alerter is not None:
            try:
                await alerter.aclose()
            except Exception:  # pragma: no cover
                pass


async def _snipe_ndjson(
    names: list[str],
    platforms: list[str],
    pool: sniper.ProxyPool,
    concurrency: int,
    retries: int,
    note: str = "",
    run: RunHandle | None = None,
    alerter: Alerter | None = None,
) -> AsyncIterator[str]:
    """One JSON verdict per line, progress as it goes, then a summary.

    A large batch runs for tens of seconds; streaming means the first answer is
    visible in the first second and no proxy or gateway has to hold the whole
    response open.
    """
    tally = _Tally(len(names), platforms, alerter)
    run_id = run.id if run else ""
    last_report = 0.0
    if note:
        yield json.dumps(
            {"type": "note", "run_id": run_id, "pool_note": note, "pool_size": len(pool)}
        ) + "\n"
    try:
        async for result in sniper.iter_snipes(
            names,
            platforms,
            pool,
            concurrency=concurrency,
            retries=retries,
            stop=run.stop if run else None,
        ):
            if result.status == sniper.WAITING_STATUS:
                # Not a row: the run is parked waiting for a proxy, and saying so
                # is what keeps the dashboard from looking frozen.
                yield json.dumps({"type": "waiting", "detail": result.detail, **tally.progress()}) + "\n"
                continue
            tally.add(result)
            if run:
                run.checked = tally.checked
            yield json.dumps({"type": "result", **_snipe_row(result)}) + "\n"
            now = time.perf_counter()
            if tally.checked % 10 == 0 and (now - last_report) >= 0.12:
                last_report = now
                yield json.dumps({"type": "progress", **tally.progress()}) + "\n"
                await _flush_alerts(alerter)
    finally:
        if run:
            run.finish()
        await _finish_alerts(alerter)
    yield json.dumps(
        {
            "type": "done",
            "run_id": run_id,
            "stopped": bool(run and run.stop.is_set()),
            "names": len(names),
            "platforms": platforms,
            "pool_note": note,
            "duration_ms": round(tally.elapsed * 1000, 1),
            "per_second": round(tally.meter.average, 1),
            "alerts": _alert_report(alerter),
            **_pool_report(pool),
            **tally.summary(),
        }
    ) + "\n"

@app.post("/snipe")
async def snipe_batch(req: SnipeRequest):
    """Check names against the platforms, through the stored proxies.

    JSON by default; ``"stream": true`` returns the same data as NDJSON, one
    result per line, so a big batch lands incrementally.
    """
    names, platforms, pool, concurrency, retries, note = await _snipe_ready(req)
    run = _open_run("snipe")
    alerter = Alerter(alert_settings.get())
    if req.stream:
        return StreamingResponse(
            _snipe_ndjson(names, platforms, pool, concurrency, retries, note, run, alerter),
            media_type="application/x-ndjson",
            # Nothing between here and the browser may buffer the stream.
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    try:
        results = [
            result
            async for result in sniper.iter_snipes(
                names,
                platforms,
                pool,
                concurrency=concurrency,
                retries=retries,
                stop=run.stop,
            )
            if result.status != sniper.WAITING_STATUS
        ]
    finally:
        run.finish()
    # The same tally the streaming path uses, so a buffered run reports the same
    # counters -- the per-platform split and the proxy-miss count used to exist
    # only on the streaming path, which made the two ways of asking the same
    # question answer differently.
    tally = _Tally(len(names), platforms, alerter)
    for result in results:
        tally.add(result)
    await _finish_alerts(alerter)
    rows = [_snipe_row(r) for r in results]
    available = [r for r in rows if r["status"] == "available"]
    return {
        "run_id": run.id,
        "stopped": run.stop.is_set(),
        "names": len(names),
        "platforms": platforms,
        "pool_note": note,
        "available": available,
        "results": rows,
        "alerts": _alert_report(alerter),
        **_pool_report(pool),
        **tally.summary(),
    }


@app.get("/snipe")
async def snipe_one(
    username: str = Query(..., min_length=1, max_length=32),
    platform: list[str] | None = Query(None),
):
    """Same thing for one name, from the query string."""
    req = SnipeRequest(username=username, platforms=platform)
    names, platforms, pool, concurrency, retries, note = await _snipe_ready(req)
    run = _open_run("snipe")
    alerter = Alerter(alert_settings.get())
    results: list[sniper.SnipeResult] = []
    try:
        results = [
            r
            async for r in sniper.iter_snipes(
                names,
                platforms,
                pool,
                concurrency=concurrency,
                retries=retries,
                stop=run.stop,
            )
            if r.status != sniper.WAITING_STATUS
        ]
    finally:
        run.finish()
    tally = _Tally(len(names), platforms, alerter)
    for result in results:
        tally.add(result)
    await _finish_alerts(alerter)
    rows = [_snipe_row(r) for r in results]
    available = [r for r in rows if r["status"] == "available"]
    return {
        "names": len(names),
        "platforms": platforms,
        "pool_note": note,
        "available": available,
        "results": rows,
        "alerts": _alert_report(alerter),
        **_pool_report(pool),
        **tally.summary(),
    }


# --- generation -----------------------------------------------------------
class PatternSpec(BaseModel):
    """One generator request: a pattern, a length, and how many to draw."""

    pattern: str
    length: int
    limit: int | None = None
    mode: str = "random"


class GenerateRequest(BaseModel):
    patterns: list[PatternSpec] = Field(default_factory=list)
    pattern: str | None = None
    length: int | None = None
    # None or 0 means every name of that length, not a sample of 500.
    limit: int | None = None
    seed: int | None = None
    mode: str = "random"
    words: list[str] | None = None


def _generate(req: GenerateRequest) -> dict:
    specs = [spec.model_dump() for spec in req.patterns]
    if not specs:
        if not req.pattern or not req.length:
            raise HTTPException(400, "give pattern+length, or a list of patterns")
        specs = [
            {"pattern": req.pattern, "length": req.length, "limit": req.limit, "mode": req.mode}
        ]
    try:
        names = generator.generate_many(specs, limit=req.limit, seed=req.seed, words=req.words)
    except ValueError as exc:
        # A space too big to enumerate is refused with its real size rather than
        # silently sampled, so "every name of this length" is never a lie.
        raise HTTPException(400, str(exc))
    total = sum(
        generator.space_size(str(spec["pattern"]), int(spec["length"]), req.words)
        for spec in specs
        if spec.get("pattern") and spec.get("length")
    )
    return {
        "count": len(names),
        "total": total,
        "truncated": total > len(names),
        "patterns": specs,
        "seed": req.seed,
        "usernames": names,
    }


@app.get("/generate")
def generate_one(
    pattern: str = Query(..., pattern="^(letters|alnum|numbers|words)$"),
    length: int = Query(..., ge=1, le=32),
    # 0 or omitted enumerates the whole space, subject to MAX_ENUMERATION.
    limit: int | None = Query(None, ge=0, le=100000),
    seed: int | None = Query(None),
    mode: str = Query("random", pattern="^(random|sequential)$"),
) -> dict:
    """Candidate usernames for one pattern: letters, alnum, numbers or words."""
    return _generate(
        GenerateRequest(pattern=pattern, length=length, limit=limit, seed=seed, mode=mode)
    )


@app.post("/generate")
def generate_many(req: GenerateRequest) -> dict:
    """The same for several patterns at once, optionally with an inline word list."""
    return _generate(req)


# --- bucket menu ----------------------------------------------------------
class BucketSpec(BaseModel):
    """One entry in the dashboard's picker: `l` letters, `n` digits, `c` both,
    `og` dictionary words, with the length."""

    kind: str
    length: int


@app.get("/menu")
def bucket_menu() -> dict:
    """Every bucket the picker offers, with its real size.

    The sizes are computed, not guessed: `3l` is 17,576 names, and a couple of
    the buckets are large enough that the number is the most useful thing on the
    screen. Nothing is sampled, so this is exactly what a run over that bucket
    will check.
    """
    return {
        "buckets": generator.menu(),
        "kinds": {kind: generator.KIND_LABEL[kind] for kind in generator.KINDS},
        "max_enumeration": MAX_ENUMERATION,
    }


def _bucket_words(buckets: list[BucketSpec]) -> list[str] | None:
    return None


# --- bulk scan ------------------------------------------------------------
class ScanRequest(BaseModel):
    """Generate (and/or supply) names, then check them at throughput."""

    platforms: list[str] | None = None
    usernames: list[str] = Field(default_factory=list)
    patterns: list[PatternSpec] = Field(default_factory=list)
    # The picker's selection. Each one is a kind and a length, and the union is
    # what gets checked -- once per name, with the redundant buckets dropped.
    buckets: list[BucketSpec] = Field(default_factory=list)
    # None or 0 means every name the patterns describe, not a sample.
    limit: int | None = None
    seed: int | None = None
    words: list[str] | None = None
    concurrency: int | None = None
    retries: int | None = None
    stream: bool = False


def _scan_plan(req: ScanRequest) -> tuple[Iterable[str], int, bool]:
    """The names to run over, lazily, plus how many that is.

    An iterator rather than a list, deliberately: the picker can select
    60,466,176 names, and the run consumes them once from front to back, so
    building the list would make memory the only thing in the process that scales
    with the size of the selection.
    """
    explicit: list[str] = []
    for raw in req.usernames:
        name = (raw or "").strip().lstrip("@")
        if name and name not in explicit:
            explicit.append(name)
    if len(explicit) > SCAN_MAX_NAMES:
        return explicit[:SCAN_MAX_NAMES], SCAN_MAX_NAMES, True

    generated: Iterable[str] = ()
    total = 0
    if req.buckets:
        specs = [bucket.model_dump() for bucket in req.buckets]
        # Overlapping buckets are dropped before the count, so the number on the
        # dashboard is what will actually be checked rather than a total that
        # double-counts `og` inside `l`.
        total = generator.buckets_total(specs, req.words)
        if total > MAX_ENUMERATION:
            raise HTTPException(
                400,
                f"that selection is {total:,} names, over the {MAX_ENUMERATION:,} one run "
                "will enumerate. Drop a bucket or a length.",
            )
        generated = generator.iter_buckets(specs, req.words)
    elif req.patterns:
        try:
            drawn = generator.generate_many(
                [spec.model_dump() for spec in req.patterns],
                limit=req.limit,
                seed=req.seed,
                words=req.words,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        generated = drawn
        total = len(drawn)

    if not explicit and total == 0:
        raise HTTPException(400, "pick at least one bucket, or give usernames")

    def stream() -> Iterator[str]:
        # Only the hand-supplied names are held in a set, so a selection of tens
        # of millions still costs one name at a time.
        seen = set(explicit)
        yield from explicit
        for name in generated:
            if name in seen:
                continue
            yield name

    return stream(), len(explicit) + total, False


async def _scan_stream(
    names: Iterable[str],
    names_total: int,
    platforms: list[str],
    pool: sniper.ProxyPool,
    concurrency: int,
    retries: int,
    truncated: bool,
    note: str = "",
    run: RunHandle | None = None,
    alerter: Alerter | None = None,
) -> AsyncIterator[str]:
    """NDJSON: one verdict per line, progress as it goes, then a summary.

    ``run.stop`` is what makes the dashboard's Stop button real: the dispatch
    loop in ``iter_snipes`` sees the same event, so a stop cancels the checks
    already in flight instead of only ending the response.
    """
    tally = _Tally(names_total, platforms, alerter)
    run_id = run.id if run else ""
    last_report = 0.0
    yield json.dumps(
        {
            "type": "start",
            "run_id": run_id,
            "names": names_total,
            "truncated": truncated,
            "platforms": platforms,
            "pool_note": note,
            "concurrency": concurrency,
            "target_rate": SCAN_TARGET_RATE,
        }
    ) + "\n"

    try:
        async for result in sniper.iter_snipes(
            names,
            platforms,
            pool,
            concurrency=concurrency,
            retries=retries,
            stop=run.stop if run else None,
        ):
            if result.status == sniper.WAITING_STATUS:
                yield json.dumps({"type": "waiting", "detail": result.detail, **tally.progress()}) + "\n"
                continue
            tally.add(result)
            if run:
                run.checked = tally.checked
            yield json.dumps({"type": "result", **_snipe_row(result)}) + "\n"
            now = time.perf_counter()
            if tally.checked % 10 == 0 and (now - last_report) >= 0.12:
                last_report = now
                yield json.dumps({"type": "progress", **tally.progress()}) + "\n"
                await _flush_alerts(alerter)
    finally:
        if run:
            run.finish()
        await _finish_alerts(alerter)

    rate = tally.meter.average
    yield json.dumps(
        {
            "type": "done",
            "run_id": run_id,
            "stopped": bool(run and run.stop.is_set()),
            "names": names_total,
            "platforms": platforms,
            "pool_note": note,
            "per_second": round(rate, 1),
            "recent_per_second": round(tally.meter.recent, 1),
            "target_rate": SCAN_TARGET_RATE,
            "target_met": rate >= SCAN_TARGET_RATE,
            "duration_ms": round(tally.elapsed * 1000, 1),
            "alerts": _alert_report(alerter),
            **_pool_report(pool),
            **tally.summary(),
        }
    ) + "\n"


@app.post("/scan")
async def scan(req: ScanRequest):
    """Bulk-check generated names, through the stored proxies.

    NDJSON by default so a long run can be watched live; ``"stream": false``
    buffers the same data into one JSON document.
    """
    names, names_total, truncated = _scan_plan(req)
    platforms = sniper.normalize_platforms(req.platforms)
    concurrency = max(1, req.concurrency or SCAN_CONCURRENCY)
    retries = SCAN_RETRIES if req.retries is None else max(0, req.retries)
    if not req.stream and names_total > SCAN_BUFFER_MAX:
        # The buffered path keeps every verdict in memory. A selection of
        # millions would be an OOM, so it is refused before the pool is built.
        raise HTTPException(
            400,
            f"that is {names_total:,} names; ask for stream=true, or keep a buffered "
            f"batch under {SCAN_BUFFER_MAX:,}.",
        )
    pool, note = await _prepare_pool("scan", concurrency)
    run = _open_run("scan")
    alerter = Alerter(alert_settings.get())

    if not req.stream:
        try:
            results = [
                result
                async for result in sniper.iter_snipes(
                    names,
                    platforms,
                    pool,
                    concurrency=concurrency,
                    retries=retries,
                    stop=run.stop,
                )
                if result.status != sniper.WAITING_STATUS
            ]
        finally:
            run.finish()
        tally = _Tally(names_total, platforms, alerter)
        for result in results:
            tally.add(result)
        await _finish_alerts(alerter)
        rows = [_snipe_row(r) for r in results]
        available = [r for r in rows if r["status"] == "available"]
        return {
            "run_id": run.id,
            "stopped": run.stop.is_set(),
            "names": names_total,
            "truncated": truncated,
            "platforms": platforms,
            "pool_note": note,
            "available": available,
            "results": rows,
            "alerts": _alert_report(alerter),
            **_pool_report(pool),
            **tally.summary(),
        }

    return StreamingResponse(
        _scan_stream(
            names,
            names_total,
            platforms,
            pool,
            concurrency,
            retries,
            truncated,
            note,
            run,
            alerter,
        ),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# --- alert settings -------------------------------------------------------
def _public_alert_settings() -> dict:
    """Settings as the UI sees them. The webhook url is a capability, so it is
    never logged and never echoed anywhere but here."""
    effective = alert_settings.get()
    webhook = str(effective.get("webhook") or "")
    return {
        "enabled": bool(effective.get("enabled")),
        "webhook": webhook,
        "webhook_set": bool(webhook),
        "template": effective.get("template") or "",
        "username": effective.get("username") or "",
        "avatar": effective.get("avatar") or "",
        "ping": effective.get("ping") or "",
        "batch": effective.get("batch"),
        "min_interval": effective.get("min_interval"),
        "max_messages": effective.get("max_messages"),
    }


class AlertPatch(BaseModel):
    webhook: str | None = None
    template: str | None = None
    username: str | None = None
    avatar: str | None = None
    ping: str | None = None
    enabled: bool | None = None


@app.get("/settings")
def get_settings() -> dict:
    return {"alerts": _public_alert_settings()}


@app.post("/settings")
def save_settings(patch: AlertPatch) -> dict:
    """Change the alert settings. Anything left out keeps its current value."""
    changes = {k: v for k, v in patch.model_dump().items() if v is not None}
    alert_settings.update(changes)
    log.info("alert settings updated: %s", sorted(changes))
    return {"alerts": _public_alert_settings()}


@app.post("/settings/test-webhook")
async def test_webhook() -> dict:
    """Post one message so the webhook can be seen to land, not assumed to."""
    return await alerts.post_test(alert_settings.get())


# --- claiming -------------------------------------------------------------
# A name can only be taken by an account, and an account is created by a person.
# So "claim" here is the route to the platform's own registration, not a bulk
# account factory: automating signups is what gets an IP range banned and is not
# something this service will do.
CLAIM_TARGETS: dict[str, dict[str, str]] = {
    "roblox": {
        "register_url": "https://www.roblox.com/signup",
        "existing_account": "Settings > Account Info > Edit username (1000 Robux)",
        "note": "Roblox assigns the username at signup and allows one paid change later.",
    },
    "minecraft": {
        "register_url": "https://www.minecraft.net/en-us/msa",
        "existing_account": "minecraft.net > Profile > Change username",
        "note": "A Java Edition name is set on the account, not on a profile.",
    },
    "telegram": {
        "register_url": "https://web.telegram.org/",
        "existing_account": "Settings > Username",
        "note": "Telegram handles are set inside the app once you have an account.",
    },
    "x": {
        "register_url": "https://x.com/i/flow/signup",
        "existing_account": "Settings > Your account > Account information > Username",
        "note": "X assigns the handle at signup and allows changes from settings.",
    },
    "github": {
        "register_url": "https://github.com/signup",
        "existing_account": "Settings > Account > Change username",
        "note": "GitHub redirects the old name to the new one after a change.",
    },
    "youtube": {
        "register_url": "https://www.youtube.com/handle",
        "existing_account": "youtube.com/handle",
        "note": "A YouTube handle is claimed once the channel has one, at /handle.",
    },
    "discord": {
        "register_url": "https://discord.com/register",
        "existing_account": "User Settings > My Account > Username",
        "note": "Discord adopts a username at signup, or changes it from an existing account's settings.",
    },
    "guns.lol": {
        "register_url": "https://guns.lol/register",
        "existing_account": "Dashboard > Settings > Username",
        "note": "guns.lol assigns the handle when you register.",
    },
    "instagram": {
        "register_url": "https://www.instagram.com/accounts/emailsignup/",
        "existing_account": "Settings > Edit profile > Username",
        "note": "Instagram assigns the handle at signup.",
    },
    "tiktok": {
        "register_url": "https://www.tiktok.com/signup",
        "existing_account": "Profile > Edit profile > Username",
        "note": "TikTok assigns the handle at signup.",
    },
}


@app.get("/claim")
def claim(
    platform: str = Query(..., min_length=1),
    username: str = Query(..., min_length=1, max_length=32),
) -> dict:
    """Where to register a name the run just reported as free.

    The platform is looked up rather than pattern-matched against a hardcoded
    list, so adding a platform to the grid cannot leave /claim answering 422 for
    a platform the run itself reports on.
    """
    target = CLAIM_TARGETS.get(platform.strip().lower())
    if target is None:
        raise HTTPException(400, f"unknown platform; try one of {sorted(CLAIM_TARGETS)}")
    return {
        "platform": platform,
        "username": username.strip().lstrip("@"),
        "url": target["register_url"],
        "existing_account": target["existing_account"],
        "note": target["note"],
        "automated": False,
        "detail": (
            "Claiming is a signed-in action on the platform: the name is taken by "
            "creating an account, which this service does not do for you. Confirm "
            "the name is still free, then register it yourself at the URL above."
        ),
    }


if __name__ == "__main__":  # pragma: no cover - the container runs uvicorn directly
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
