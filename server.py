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

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

import sniper
from config import (
    AUTO_START,
    LOG_LEVEL,
    PORT,
    REFRESH_INTERVAL,
    SNIPE_CONCURRENCY,
    SNIPE_MAX_NAMES,
    SNIPE_PER_PROXY,
    SNIPE_POOL,
    SNIPE_POOL_TTL,
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


# One warm pool, shared by every request, so the second batch of names skips the
# handshakes the first one paid. Rebuilt when it goes stale or the store empties.
_pool_state: dict[str, object] = {"pool": None, "built": 0.0}
_pool_lock = asyncio.Lock()


def _build_pool() -> sniper.ProxyPool:
    """Pool the validated proxies, and note which target each one already passed."""
    rows = store.query(alive_only=True, limit=1_000_000)
    proxies = [r.proxy for r in rows[:SNIPE_POOL]]
    per_platform = {
        name: [r.proxy for r in rows if r.platforms.get(name, {}).get("ok")][:SNIPE_POOL]
        for name in sniper.PLATFORMS
    }
    return sniper.ProxyPool(proxies, per_platform=per_platform, per_proxy=SNIPE_PER_PROXY)


async def _snipe_pool() -> sniper.ProxyPool:
    async with _pool_lock:
        pool = _pool_state["pool"]
        age = time.time() - float(_pool_state["built"])  # type: ignore[arg-type]
        if isinstance(pool, sniper.ProxyPool) and len(pool) and age < SNIPE_POOL_TTL:
            return pool
        fresh = _build_pool()
        if isinstance(pool, sniper.ProxyPool) and pool is not fresh:
            # Retire the old connections in the background; nobody waits on it.
            asyncio.create_task(pool.aclose())
        _pool_state["pool"] = fresh
        _pool_state["built"] = time.time()
        return fresh


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


async def _snipe_ready(req: SnipeRequest) -> tuple[list[str], list[str], sniper.ProxyPool, int, int]:
    """Validate the request and fetch the pool before anything streams."""
    names = _names_of(req)
    pool = await _snipe_pool()
    if len(pool) == 0:
        raise HTTPException(503, "no validated proxies yet -- run a refresh cycle first")
    platforms = sniper.normalize_platforms(req.platforms)
    concurrency = max(1, req.concurrency or SNIPE_CONCURRENCY)
    retries = SNIPE_RETRIES if req.retries is None else max(0, req.retries)
    return names, platforms, pool, concurrency, retries


async def _snipe_ndjson(
    names: list[str],
    platforms: list[str],
    pool: sniper.ProxyPool,
    concurrency: int,
    retries: int,
) -> AsyncIterator[str]:
    """One JSON verdict per line, then a summary line.

    A large batch runs for tens of seconds; streaming means the first answer is
    visible in the first second and no proxy or gateway has to hold the whole
    response open.
    """
    started = time.perf_counter()
    checked = 0
    available = 0
    async for result in sniper.iter_snipes(
        names, platforms, pool, concurrency=concurrency, retries=retries
    ):
        checked += 1
        available += 1 if result.status == "available" else 0
        yield json.dumps({"type": "result", **_snipe_row(result)}) + "\n"
    yield json.dumps(
        {
            "type": "done",
            "names": len(names),
            "platforms": platforms,
            "pool_size": len(pool),
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
    names, platforms, pool, concurrency, retries = await _snipe_ready(req)
    if req.stream:
        return StreamingResponse(
            _snipe_ndjson(names, platforms, pool, concurrency, retries),
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
    names, platforms, pool, concurrency, retries = await _snipe_ready(req)
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
        "checked": len(rows),
        "available_count": len(available),
        "available": available,
        "results": rows,
    }


if __name__ == "__main__":  # pragma: no cover - container runs uvicorn directly
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
