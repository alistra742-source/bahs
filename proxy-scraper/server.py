"""HTTP surface for the proxy scraper.

Read endpoints answer from the in-memory store, which the background scheduler
keeps fresh. A scrape is never run inline on a request: /refresh queues a cycle
and returns immediately, so a slow source list can never time out a caller.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from config import API_KEY, LOG_LEVEL, PORT, REFRESH_INTERVAL, STORE_PATH
from scheduler import RefreshManager
from store import ProxyStore

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("proxy-scraper.server")

store = ProxyStore(STORE_PATH)
manager = RefreshManager(store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.load()
    manager.start()
    log.info("proxy-scraper up on :%d, refresh every %ds", PORT, REFRESH_INTERVAL)
    try:
        yield
    finally:
        await manager.stop()
        store.save()


app = FastAPI(title="proxy-scraper", version="1.0.0", lifespan=lifespan)


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
        "first_seen": record.first_seen,
        "last_ok": record.last_ok,
    }


@app.get("/")
def root() -> dict:
    return {
        "service": "proxy-scraper",
        "endpoints": {
            "GET /health": "service + scheduler status",
            "GET /stats": "store statistics",
            "GET /proxies": "filtered, ranked proxy list",
            "GET /best": "top proxies passing every platform",
            "POST /refresh": "queue a scrape+validate cycle",
        },
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "scheduler": manager.status(), "store": store.stats()}


@app.get("/stats", dependencies=[Depends(require_key)])
def stats() -> dict:
    return {"scheduler": manager.status(), "store": store.stats()}


@app.post("/refresh", dependencies=[Depends(require_key)])
async def refresh() -> dict:
    if manager.running:
        return {"queued": False, "reason": "cycle already running", "scheduler": manager.status()}
    await manager.trigger()
    return {"queued": True, "scheduler": manager.status()}


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


if __name__ == "__main__":  # pragma: no cover - container runs uvicorn directly
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
