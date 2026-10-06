"""HTTP surface for the proxy scraper.

The dashboard is served at ``/``; the JSON API sits beside it. Read endpoints
answer from the in-memory store, which the background scheduler keeps fresh. A
scrape is never run inline on a request: /refresh queues a cycle and returns
immediately, so a slow source list can never time out a caller.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

import generator
import sniper
from config import (
    AUTO_START,
    LOG_LEVEL,
    MIN_ALIVE,
    PORT,
    REFRESH_INTERVAL,
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
    SNIPE_PLATFORM_WEIGHT,
    SNIPE_POOL,
    SNIPE_POOL_TTL,
    SNIPE_PROXY_COOLDOWN,
    SNIPE_RETRIES,
    STORE_PATH,
)
from scheduler import RefreshManager
from store import ProxyStore

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs one INFO line per request and a validation cycle issues tens of
# thousands of them, which buries the progress lines that matter. Errors still
# surface at WARNING and above.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("proxy-scraper.server")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

store = ProxyStore(STORE_PATH)
manager = RefreshManager(store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.load()
    if AUTO_START:
        manager.start()
    log.info(
        "proxy-scraper up on :%d, refresh every %ds, auto_start=%s",
        PORT,
        REFRESH_INTERVAL,
        AUTO_START,
    )
    try:
        yield
    finally:
        await manager.stop()
        store.save()


app = FastAPI(title="proxy-scraper", version="1.1.0", lifespan=lifespan)


def _row(record) -> dict:
    return {
        "proxy": record.proxy,
        "protocol": record.protocol,
        "host": record.host,
        "port": record.port,
        "score": record.score,
        "anonymity": record.anonymity,
        "latency_ms": round(record.latency_ms, 2) if record.latency_ms is not None else None,
        "exit_ip": record.exit_ip,
        "platforms": record.platforms,
        "platforms_passed": record.platforms_passed,
        "fail_count": record.fail_count,
        "first_seen": record.first_seen,
        "last_ok": record.last_ok,
    }


# --- site + info ----------------------------------------------------------
@app.get("/", include_in_schema=False)
def site() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/info")
def info() -> dict:
    return {
        "service": "proxy-scraper",
        "endpoints": {
            "GET /": "dashboard",
            "GET /health": "scheduler + store status",
            "GET /proxies": "filtered, ranked proxy list",
            "GET /best": "top proxies passing every platform",
            "DELETE /proxies": "remove one proxy by ?proxy=",
            "POST /proxies/purge": "drop ?scope=dead|all",
            "POST /start": "start the refresh loop",
            "POST /stop": "stop the refresh loop",
            "POST /refresh": "queue a single scrape+validate cycle",
            "POST /snipe": "check usernames on Discord / guns.lol / Instagram through validated proxies (JSON, or NDJSON with stream=true)",
            "GET /snipe": "the same for one ?username=",
            "GET /generate": "candidate usernames: letters | alnum | numbers | words, by length",
            "POST /generate": "the same for several patterns at once",
            "POST /scan": "generate and check in bulk at a target rate, NDJSON with stream=true",
            "GET /claim": "the registration route for a name on a platform",
        },
    }


# --- status ---------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "scheduler": manager.status(), "store": store.stats()}


@app.get("/stats")
def stats() -> dict:
    return {"scheduler": manager.status(), "store": store.stats()}


# --- start / stop ---------------------------------------------------------
# async: manager.start() schedules a task on the running loop, which a sync
# route executed in a threadpool would not have.
@app.post("/start")
async def start() -> dict:
    manager.start()
    return {"enabled": manager.enabled, "scheduler": manager.status()}


@app.post("/stop")
async def stop() -> dict:
    await manager.stop()
    return {"enabled": manager.enabled, "scheduler": manager.status()}


@app.post("/refresh")
async def refresh() -> dict:
    if manager.running:
        return {"queued": False, "reason": "cycle already running", "scheduler": manager.status()}
    await manager.trigger()
    return {"queued": True, "scheduler": manager.status()}


# --- proxy queries --------------------------------------------------------
@app.get("/best")
def best(
    limit: int = Query(25, ge=1, le=500),
    anonymity: str | None = Query(None, pattern="^(elite|anonymous|transparent)$"),
    format: str = Query("json", pattern="^(json|txt)$"),
):
    rows = [
        r
        for r in store.query(anonymity=anonymity, limit=10_000, alive_only=True)
        if r.platforms_passed == 3
    ][:limit]
    if format == "txt":
        return PlainTextResponse("\n".join(r.proxy for r in rows))
    return {"count": len(rows), "proxies": [_row(r) for r in rows]}


# Every proxy is probed against exactly this many platforms (platforms.PLATFORMS).
PLATFORM_COUNT = 3


@app.get("/proxies")
def proxies(
    platform: str | None = Query(None, pattern="^(discord|guns\\.lol|instagram)$"),
    anonymity: str | None = Query(None, pattern="^(elite|anonymous|transparent)$"),
    protocol: str | None = Query(None, pattern="^(http|https|socks4|socks5)$"),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    all_platforms: bool = Query(False),
    limit: int = Query(100, ge=1, le=1000),
    format: str = Query("json", pattern="^(json|txt)$"),
):
    # The pass-all filter runs before the limit, so asking for the proxies that
    # pass every platform does not silently return fewer than `limit`.
    rows = store.query(
        platform=platform,
        anonymity=anonymity,
        protocol=protocol,
        min_score=min_score,
        limit=10_000 if all_platforms else limit,
        alive_only=True,
    )
    if all_platforms:
        rows = [r for r in rows if r.platforms_passed == PLATFORM_COUNT][:limit]
    if format == "txt":
        return PlainTextResponse("\n".join(r.proxy for r in rows))
    return {"count": len(rows), "proxies": [_row(r) for r in rows]}


# --- proxy management -----------------------------------------------------
@app.delete("/proxies")
def remove_proxy(proxy: str = Query(..., min_length=1)) -> dict:
    removed = store.remove(proxy)
    if not removed:
        raise HTTPException(status_code=404, detail="proxy not tracked")
    store.save()
    return {"removed": proxy, "store": store.stats()}


@app.post("/proxies/purge")
def purge_proxies(scope: str = Query("dead", pattern="^(dead|all)$")) -> dict:
    removed = store.purge(scope)
    store.save()
    return {"scope": scope, "removed": removed, "store": store.stats()}


# --- username sniping -----------------------------------------------------
class SnipeRequest(BaseModel):
    """A batch of names, or a single one, against one or more platforms."""

    usernames: list[str] = Field(default_factory=list)
    username: str | None = None
    platforms: list[str] | None = None
    concurrency: int | None = None
    retries: int | None = None
    # Stream each verdict as its own NDJSON line instead of buffering the batch.
    stream: bool = False


# One warm pool per mode, shared by every request, so the second batch of names
# skips the handshakes the first one paid. Rebuilt when it goes stale or the
# store empties. "scan" gets its own, with tighter timeouts and wider per-proxy
# concurrency, so a scan never reconfigures the pool a single check is using.
_pool_state: dict[str, dict[str, object]] = {
    "snipe": {"pool": None, "built": 0.0},
    "scan": {"pool": None, "built": 0.0},
}
_pool_lock = asyncio.Lock()


def _build_pool(scan: bool = False) -> sniper.ProxyPool:
    """Pool the validated proxies, and note which target each one already passed."""
    rows = store.query(alive_only=True, limit=1_000_000)
    proxies = [r.proxy for r in rows[:SNIPE_POOL]]
    per_platform = {
        name: [r.proxy for r in rows if r.platforms.get(name, {}).get("ok")][:SNIPE_POOL]
        for name in sniper.PLATFORMS
    }
    if scan:
        timeout = httpx.Timeout(
            connect=SCAN_CONNECT_TIMEOUT,
            read=SCAN_READ_TIMEOUT,
            write=SCAN_READ_TIMEOUT,
            pool=SCAN_READ_TIMEOUT,
        )
        return sniper.ProxyPool(
            proxies,
            per_platform=per_platform,
            per_proxy=SCAN_PER_PROXY,
            timeout=timeout,
            platform_weight=SNIPE_PLATFORM_WEIGHT,
        )
    return sniper.ProxyPool(
        proxies,
        per_platform=per_platform,
        per_proxy=SNIPE_PER_PROXY,
        platform_weight=SNIPE_PLATFORM_WEIGHT,
    )


async def _pool(mode: str = "snipe") -> sniper.ProxyPool:
    scan = mode == "scan"
    async with _pool_lock:
        state = _pool_state[mode]
        pool = state["pool"]
        age = time.time() - float(state["built"])  # type: ignore[arg-type]
        if isinstance(pool, sniper.ProxyPool) and len(pool) and age < SNIPE_POOL_TTL:
            return pool
        fresh = _build_pool(scan)
        if isinstance(pool, sniper.ProxyPool) and pool is not fresh:
            # Retire the old connections in the background; nobody waits on it.
            asyncio.create_task(pool.aclose())
        state["pool"] = fresh
        state["built"] = time.time()
        return fresh


def _status_counts(rows: list[dict]) -> dict[str, int]:
    """How the verdicts broke down, so a wall of errors is a number, not a guess."""
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


async def _prepare_pool(mode: str) -> tuple[sniper.ProxyPool, str]:
    """The pool for a run, plus a note when it is too thin to be useful.

    Below ``MIN_ALIVE`` a run cannot answer anything: every check comes back an
    error and the caller just sees a table of nothing. A fresh deploy with no
    mounted volume starts from an empty store, which is exactly that case. So a
    refresh is queued and the reason is handed back for the dashboard to show,
    instead of letting the run fail silently.
    """
    pool = await _pool(mode)
    if len(pool) == 0:
        raise HTTPException(503, "no validated proxies yet -- run a refresh cycle first")
    if len(pool) < MIN_ALIVE:
        note = (
            f"only {len(pool)} validated proxies alive -- a refresh was queued; "
            "checks may come back as errors until the pool refills"
        )
        await manager.trigger()
        return pool, note
    return pool, ""


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


def _snipe_row(result: sniper.SnipeResult) -> dict:
    return {
        "username": result.username,
        "platform": result.platform,
        "status": result.status,
        "detail": result.detail,
        "proxy": result.proxy,
        "latency_ms": round(result.latency_ms, 2) if result.latency_ms is not None else None,
    }


async def _snipe_ready(
    req: SnipeRequest,
) -> tuple[list[str], list[str], sniper.ProxyPool, int, int, str]:
    """Validate the request and fetch the pool before anything streams."""
    names = _names_of(req)
    pool, note = await _prepare_pool("snipe")
    platforms = sniper.normalize_platforms(req.platforms)
    concurrency = max(1, req.concurrency or SNIPE_CONCURRENCY)
    retries = SNIPE_RETRIES if req.retries is None else max(0, req.retries)
    return names, platforms, pool, concurrency, retries, note


async def _snipe_ndjson(
    names: list[str],
    platforms: list[str],
    pool: sniper.ProxyPool,
    concurrency: int,
    retries: int,
    note: str = "",
) -> AsyncIterator[str]:
    """One JSON verdict per line, then a summary line.

    A large batch runs for tens of seconds; streaming means the first answer is
    visible in the first second and no proxy or gateway has to hold the whole
    response open.
    """
    started = time.perf_counter()
    checked = 0
    available = 0
    counts: dict[str, int] = {}
    if note:
        yield json.dumps({"type": "note", "pool_note": note, "pool_size": len(pool)}) + "\n"
    async for result in sniper.iter_snipes(
        names, platforms, pool, concurrency=concurrency, retries=retries
    ):
        checked += 1
        counts[result.status] = counts.get(result.status, 0) + 1
        available += 1 if result.status == "available" else 0
        yield json.dumps({"type": "result", **_snipe_row(result)}) + "\n"
    yield json.dumps(
        {
            "type": "done",
            "names": len(names),
            "platforms": platforms,
            "pool_size": len(pool),
            "pool_note": note,
            "by_status": counts,
            "resting": pool.resting(),
            "checked": checked,
            "available_count": available,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    ) + "\n"


@app.post("/snipe")
async def snipe_batch(req: SnipeRequest):
    """Check names against the platforms, through validated proxies.

    JSON by default; ``"stream": true`` returns the same data as NDJSON, one
    result per line, so a big batch lands incrementally.
    """
    names, platforms, pool, concurrency, retries, note = await _snipe_ready(req)
    if req.stream:
        return StreamingResponse(
            _snipe_ndjson(names, platforms, pool, concurrency, retries, note),
            media_type="application/x-ndjson",
            # Nothing between here and the browser may buffer the stream.
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    results = [
        result
        async for result in sniper.iter_snipes(
            names, platforms, pool, concurrency=concurrency, retries=retries
        )
    ]
    rows = [_snipe_row(r) for r in results]
    available = [r for r in rows if r["status"] == "available"]
    return {
        "names": len(names),
        "platforms": platforms,
        "pool_size": len(pool),
        "pool_note": note,
        "by_status": _status_counts(rows),
        "checked": len(rows),
        "available_count": len(available),
        "available": available,
        "results": rows,
    }


@app.get("/snipe")
async def snipe_one(
    username: str = Query(..., min_length=1, max_length=32),
    platform: list[str] | None = Query(None),
):
    """Same thing for one name, from the query string."""
    req = SnipeRequest(username=username, platforms=platform)
    names, platforms, pool, concurrency, retries, note = await _snipe_ready(req)
    rows = [
        _snipe_row(r)
        async for r in sniper.iter_snipes(
            names, platforms, pool, concurrency=concurrency, retries=retries
        )
    ]
    available = [r for r in rows if r["status"] == "available"]
    return {
        "names": len(names),
        "platforms": platforms,
        "pool_size": len(pool),
        "pool_note": note,
        "by_status": _status_counts(rows),
        "checked": len(rows),
        "available_count": len(available),
        "available": available,
        "results": rows,
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
    limit: int = 500
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
        names = generator.generate_many(
            specs, limit=req.limit, seed=req.seed, words=req.words
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"count": len(names), "patterns": specs, "seed": req.seed, "usernames": names}


@app.get("/generate")
def generate_one(
    pattern: str = Query(..., pattern="^(letters|alnum|numbers|words)$"),
    length: int = Query(..., ge=1, le=32),
    limit: int = Query(500, ge=1, le=100000),
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


# --- bulk scan ------------------------------------------------------------
class ScanRequest(BaseModel):
    """Generate (and/or supply) names, then check them at throughput."""

    platforms: list[str] | None = None
    usernames: list[str] = Field(default_factory=list)
    patterns: list[PatternSpec] = Field(default_factory=list)
    limit: int = 2000
    seed: int | None = None
    words: list[str] | None = None
    concurrency: int | None = None
    retries: int | None = None
    stream: bool = False


def _scan_names(req: ScanRequest) -> tuple[list[str], bool]:
    names: list[str] = []
    for raw in req.usernames:
        name = (raw or "").strip().lstrip("@")
        if name and name not in names:
            names.append(name)
    if req.patterns:
        try:
            drawn = generator.generate_many(
                [spec.model_dump() for spec in req.patterns],
                limit=req.limit,
                seed=req.seed,
                words=req.words,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        for name in drawn:
            if name not in names:
                names.append(name)
    if not names:
        raise HTTPException(400, "give usernames, or at least one pattern to generate")
    if len(names) > SCAN_MAX_NAMES:
        return names[:SCAN_MAX_NAMES], True
    return names, False


async def _scan_stream(
    names: list[str],
    platforms: list[str],
    pool: sniper.ProxyPool,
    concurrency: int,
    retries: int,
    truncated: bool,
    note: str = "",
) -> AsyncIterator[str]:
    """NDJSON: one verdict per line, a rate line every ~100 checks, then a summary."""
    meter = sniper.RateMeter()
    checked = 0
    available = 0
    last_report = 0.0
    counts: dict[str, int] = {}
    yield json.dumps(
        {
            "type": "start",
            "names": len(names),
            "truncated": truncated,
            "platforms": platforms,
            "pool_size": len(pool),
            "pool_note": note,
            "platform_pool": {name: pool.platform_size(name) for name in platforms},
            "concurrency": concurrency,
            "target_rate": SCAN_TARGET_RATE,
        }
    ) + "\n"

    async for result in sniper.iter_snipes(
        names, platforms, pool, concurrency=concurrency, retries=retries
    ):
        meter.tick()
        checked += 1
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status == "available":
            available += 1
        yield json.dumps({"type": "result", **_snipe_row(result)}) + "\n"
        now = time.perf_counter()
        if checked % 100 == 0 and (now - last_report) >= 0.5:
            last_report = now
            yield json.dumps(
                {
                    "type": "progress",
                    "checked": checked,
                    "per_second": round(meter.recent, 1),
                    "average_per_second": round(meter.average, 1),
                    "available_count": available,
                    "elapsed_s": round(meter.elapsed, 2),
                }
            ) + "\n"

    rate = meter.average
    yield json.dumps(
        {
            "type": "done",
            "names": len(names),
            "platforms": platforms,
            "pool_size": len(pool),
            "pool_note": note,
            "by_status": counts,
            "checked": checked,
            "available_count": available,
            "per_second": round(rate, 1),
            "recent_per_second": round(meter.recent, 1),
            "target_rate": SCAN_TARGET_RATE,
            "target_met": rate >= SCAN_TARGET_RATE,
            "duration_ms": round(meter.elapsed * 1000, 1),
        }
    ) + "\n"


@app.post("/scan")
async def scan(req: ScanRequest):
    """Bulk-check generated names, through the validated proxies, at a target rate.

    NDJSON by default so a long scan can be watched live; ``"stream": false``
    buffers the same data into one JSON document.
    """
    names, truncated = _scan_names(req)
    pool, note = await _prepare_pool("scan")
    platforms = sniper.normalize_platforms(req.platforms)
    concurrency = max(1, req.concurrency or SCAN_CONCURRENCY)
    retries = SCAN_RETRIES if req.retries is None else max(0, req.retries)

    if not req.stream:
        results = [
            result
            async for result in sniper.iter_snipes(
                names, platforms, pool, concurrency=concurrency, retries=retries
            )
        ]
        rows = [_snipe_row(r) for r in results]
        available = [r for r in rows if r["status"] == "available"]
        return {
            "names": len(names),
            "truncated": truncated,
            "platforms": platforms,
            "pool_size": len(pool),
            "pool_note": note,
            "by_status": _status_counts(rows),
            "checked": len(rows),
            "available_count": len(available),
            "available": available,
            "results": rows,
        }

    return StreamingResponse(
        _scan_stream(names, platforms, pool, concurrency, retries, truncated, note),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# --- claiming -------------------------------------------------------------
# A name can only be taken by an account, and an account is created by a person.
# So "claim" here is the route to the platform's own registration, not a bulk
# account factory: automating signups is what gets an IP range banned and is not
# something this service will do.
CLAIM_TARGETS: dict[str, dict[str, str]] = {
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
}


@app.get("/claim")
def claim(
    platform: str = Query(..., pattern="^(discord|guns\\.lol|instagram)$"),
    username: str = Query(..., min_length=1, max_length=32),
) -> dict:
    """Where to register a name the scan just reported as free."""
    target = CLAIM_TARGETS[platform]
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


if __name__ == "__main__":  # pragma: no cover - container runs uvicorn directly
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
