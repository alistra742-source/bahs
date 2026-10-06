"""HTTP surface for the proxy scraper.

The dashboard is served at ``/``; the JSON API sits beside it. Read endpoints
answer from the in-memory store, which the background scheduler keeps fresh. A
scrape is never run inline on a request: /refresh queues a cycle and returns
immediately, so a slow source list can never time out a caller.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from config import API_KEY, AUTO_START, LOG_LEVEL, PORT, REFRESH_INTERVAL, STORE_PATH
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


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


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
        },
    }


# --- status ---------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "scheduler": manager.status(), "store": store.stats()}


@app.get("/stats", dependencies=[Depends(require_key)])
def stats() -> dict:
    return {"scheduler": manager.status(), "store": store.stats()}


# --- start / stop ---------------------------------------------------------
# async: manager.start() schedules a task on the running loop, which a sync
# route executed in a threadpool would not have.
@app.post("/start", dependencies=[Depends(require_key)])
async def start() -> dict:
    manager.start()
    return {"enabled": manager.enabled, "scheduler": manager.status()}


@app.post("/stop", dependencies=[Depends(require_key)])
async def stop() -> dict:
    await manager.stop()
    return {"enabled": manager.enabled, "scheduler": manager.status()}


@app.post("/refresh", dependencies=[Depends(require_key)])
async def refresh() -> dict:
    if manager.running:
        return {"queued": False, "reason": "cycle already running", "scheduler": manager.status()}
    await manager.trigger()
    return {"queued": True, "scheduler": manager.status()}


# --- proxy queries --------------------------------------------------------
@app.get("/best", dependencies=[Depends(require_key)])
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


@app.get("/proxies", dependencies=[Depends(require_key)])
def proxies(
    platform: str | None = Query(None, pattern="^(discord|guns\\.lol|instagram)$"),
    anonymity: str | None = Query(None, pattern="^(elite|anonymous|transparent)$"),
    protocol: str | None = Query(None, pattern="^(http|https|socks4|socks5)$"),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    limit: int = Query(100, ge=1, le=1000),
    format: str = Query("json", pattern="^(json|txt)$"),
):
    rows = store.query(
        platform=platform,
        anonymity=anonymity,
        protocol=protocol,
        min_score=min_score,
        limit=limit,
        alive_only=True,
    )
    if format == "txt":
        return PlainTextResponse("\n".join(r.proxy for r in rows))
    return {"count": len(rows), "proxies": [_row(r) for r in rows]}


# --- proxy management -----------------------------------------------------
@app.delete("/proxies", dependencies=[Depends(require_key)])
def remove_proxy(proxy: str = Query(..., min_length=1)) -> dict:
    removed = store.remove(proxy)
    if not removed:
        raise HTTPException(status_code=404, detail="proxy not tracked")
    store.save()
    return {"removed": proxy, "store": store.stats()}


@app.post("/proxies/purge", dependencies=[Depends(require_key)])
def purge_proxies(scope: str = Query("dead", pattern="^(dead|all)$")) -> dict:
    removed = store.purge(scope)
    store.save()
    return {"scope": scope, "removed": removed, "store": store.stats()}


if __name__ == "__main__":  # pragma: no cover - container runs uvicorn directly
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
