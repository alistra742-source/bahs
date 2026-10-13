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
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from alerts import AlertSettings, Alerter

import alerts
import generator
import proxies as proxy_list_module
import sniper
from config import (
    LOG_LEVEL,
    SHUFFLE_NAMES,
    MAX_CONCURRENT_RUNS,
    MAX_ENUMERATION,
    SELECTION_BUILD_MAX,
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
    STATS_PATH,
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

# The Hits page needs the free names a run found, and it needs them after the
# tab that started the run is gone -- so they are kept here, not in a browser.
# A name can only reach this dict from `_Tally.add`, i.e. from a result the
# checker actually returned as `available`; nothing else may write to it.
HITS_MAX = 5000
_HITS: dict[str, dict] = {}
_HIT_ORDER: list[str] = []


def _record_hit(
    username: str, platform: str, detail: str, confirmed: bool, corrected: bool, run_id: str
) -> None:
    """Remember a free name. Same name twice updates the row, never doubles it."""
    key = platform + ":" + username.lower()
    existing = _HITS.get(key)
    if existing is not None:
        # A later read is better evidence than an earlier one: a name that came
        # back confirmed on a rerun is upgraded, and never downgraded silently.
        existing["confirmed"] = existing["confirmed"] or bool(confirmed)
        if confirmed:
            existing["detail"] = detail
        if run_id:
            existing["run_id"] = run_id
        return
    _HITS[key] = {
        "username": username,
        "platform": platform,
        "detail": detail,
        "confirmed": bool(confirmed),
        "corrected": bool(corrected),
        "run_id": run_id,
        "at": time.time(),
    }
    _HIT_ORDER.append(key)
    while len(_HIT_ORDER) > HITS_MAX:
        _HITS.pop(_HIT_ORDER.pop(0), None)


class DailyStats:
    """What the service has checked today, so the landing page can show it.

    The landing page's stats strip used to be decoration: a number that was true
    of nothing. These are the run's own counters, written when a run ends and
    kept in a small file so a restart does not reset "today". The date is part of
    the record, so a stale file rolls over to zero instead of reporting
    yesterday's sweep as today's.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data = {"date": "", "checks": 0, "verdicts": 0, "hits": 0, "refused": 0,
                      "unanswered": 0, "errors": 0, "seconds": 0.0, "runs": 0}

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            log.warning("could not read %s (%s); starting today's numbers at zero", self.path, exc)
            return
        if isinstance(data, dict) and data.get("date") == self._today():
            for key in ("checks", "verdicts", "hits", "refused", "unanswered", "errors",
                        "seconds", "runs"):
                if isinstance(data.get(key), (int, float)):
                    self._data[key] = data[key]
            self._data["date"] = data["date"]

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        temp = f"{self.path}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle)
        os.replace(temp, self.path)

    def record(self, run: "RunHandle") -> None:
        """Fold one finished run's counters in. Never called twice per run."""
        tally = run.tally
        if tally is None or getattr(run, "stats_recorded", False):
            return
        progress = tally.progress()
        with self._lock:
            self._roll()
            self._data["checks"] += int(progress.get("checked") or 0)
            self._data["verdicts"] += int(progress.get("answered") or 0)
            self._data["hits"] += int(progress.get("available_count") or 0)
            self._data["refused"] += int(progress.get("rate_limits") or 0)
            self._data["unanswered"] += int(progress.get("unanswered") or 0)
            self._data["errors"] += int(progress.get("error") or 0)
            self._data["seconds"] += float(progress.get("elapsed_s") or 0.0)
            self._data["runs"] += 1
            run.stats_recorded = True
        try:
            self.save()
        except OSError as exc:  # pragma: no cover - a read-only volume
            log.warning("could not write %s (%s)", self.path, exc)

    def _roll(self) -> None:
        today = self._today()
        if self._data.get("date") != today:
            self._data = {"date": today, "checks": 0, "verdicts": 0, "hits": 0,
                          "refused": 0, "unanswered": 0, "errors": 0,
                          "seconds": 0.0, "runs": 0}
            self._data["date"] = today

    def snapshot(self, live_checks: int = 0, live_verdicts: int = 0, live_hits: int = 0) -> dict:
        """Today's numbers with the runs in flight folded in, so the strip moves."""
        with self._lock:
            self._roll()
            data = dict(self._data)
        checks = data["checks"] + live_checks
        verdicts = data["verdicts"] + live_verdicts
        seconds = data["seconds"]
        return {
            "date": data["date"],
            "checks": checks,
            "verdicts": verdicts,
            "hits": data["hits"] + live_hits,
            "refused": data["refused"],
            "unanswered": data["unanswered"],
            "errors": data["errors"],
            "runs": data["runs"],
            "seconds": round(seconds, 1),
            # Averaged over the runs' own wall clock, not over the service's
            # uptime: an idle hour is not a slow hour.
            "verdicts_per_second": round(verdicts / seconds, 1) if seconds > 0 else 0.0,
            "checks_per_second": round(checks / seconds, 1) if seconds > 0 else 0.0,
        }


def _build_id() -> str:
    """The commit this process is running, so a live site can be told apart from
    a stale deploy or a cached page.

    Without this there is no way to answer "I changed it, why do I still see the
    old bug?" from the outside: the UI ships with no version anywhere. Railway
    and Docker both hand the commit to the container; a local run falls back to
    the checkout's HEAD, and anything else says so honestly.
    """
    for name in ("RAILWAY_GIT_COMMIT_SHA", "SOURCE_COMMIT", "GIT_COMMIT", "BUILD_ID"):
        value = os.environ.get(name)
        if value:
            return value.strip()[:7]
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        meta = os.path.join(root, ".git")
        # A worktree keeps .git as a one-line pointer ("gitdir: /path"), not a
        # directory, so follow it before reading HEAD.
        if os.path.isfile(meta):
            with open(meta, encoding="utf-8") as fh:
                pointer = fh.read().strip()
            if not pointer.startswith("gitdir:"):
                return "dev"
            meta = pointer.split(":", 1)[1].strip()
        # Branch refs live in the *common* git dir, which a worktree names in
        # its commondir file; only HEAD and the per-worktree state are local.
        common = meta
        pointer = os.path.join(meta, "commondir")
        if os.path.isfile(pointer):
            with open(pointer, encoding="utf-8") as fh:
                common = os.path.join(meta, fh.read().strip())
        with open(os.path.join(meta, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if head.startswith("ref:"):
            ref = os.path.join(common, head.split(" ", 1)[1].strip())
            with open(ref, encoding="utf-8") as fh:
                head = fh.read().strip()
    except OSError:
        return "dev"
    return head[:7] if head else "dev"


BUILD = _build_id()

proxy_list = proxy_list_module.ProxyList(STORE_PATH)

# Today's totals, for the landing page's stats strip.
daily_stats = DailyStats(STATS_PATH)

# Discord alerts. The environment is the base and the UI can override it, so
# this is loaded at boot and saved only when someone changes something by hand.
alert_settings = AlertSettings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    proxy_list.load()
    alert_settings.load()
    daily_stats.load()
    log.info(
        "bahs up on :%d, %d stored proxies, nofile=%s",
        PORT,
        len(proxy_list),
        _RAISED_FD_LIMIT if _RAISED_FD_LIMIT is not None else "unchanged",
    )
    try:
        yield
    finally:
        # A run outlives its client by design, so shutting the process down is
        # the one thing that has to reach in and stop it -- otherwise a detached
        # sweep is still holding sockets while the loop is closing.
        for handle in list(_runs.values()):
            if handle.task is not None and not handle.task.done():
                handle.stop.set()
                handle.task.cancel()
        await _close_pools()
        proxy_list.save()


# docs_url is turned off because /docs is a page of ours now -- the built-in
# Swagger UI would otherwise shadow it, which is a confusing way to find out that
# two things wanted the same path. The schema stays at /openapi.json and ReDoc at
# /redoc.
app = FastAPI(title="bahs", version="3.0.0", lifespan=lifespan, docs_url=None)


# --- site + info ----------------------------------------------------------
# no-cache (not no-store): the browser may keep a page or a script but must
# revalidate it, so a redeploy cannot leave an old build being served.
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


def _page(name: str) -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, name), headers=_NO_CACHE)


# Each of these is one job, on its own page. Nothing is crammed together: the
# console starts and watches a run, hits lists what was found, platforms carries
# the grid, and so on -- the sidebar is the navigation, not a set of panels.
#
# `/` is the front door and `/console` is where the tool lives. The tool used to
# be `/`, which meant the landing page had nowhere to be, and the console was
# reached by guessing the root path. Both jobs are now their own page, and every
# link in the app points at the console explicitly.
@app.get("/", include_in_schema=False)
def page_landing() -> FileResponse:
    return _page("landing.html")


@app.get("/console", include_in_schema=False)
def page_console() -> FileResponse:
    return _page("console.html")


@app.get("/hits/", include_in_schema=False)
def page_hits() -> FileResponse:
    # A trailing slash, because `/hits` is the JSON API. Two things wanting one
    # path is a bug that only shows up as a page silently returning JSON or the
    # other way round -- so the pages that share a name with an endpoint wear the
    # slash and the API keeps the path it always had.
    return _page("hits.html")


@app.get("/platforms", include_in_schema=False)
def page_platforms() -> FileResponse:
    return _page("platforms.html")


@app.get("/proxies/", include_in_schema=False)
def page_proxies() -> FileResponse:
    return _page("proxies.html")


@app.get("/alerts", include_in_schema=False)
def page_alerts() -> FileResponse:
    return _page("alerts.html")


@app.get("/settings/", include_in_schema=False)
def page_settings() -> FileResponse:
    return _page("settings.html")


@app.get("/docs", include_in_schema=False)
def page_docs() -> FileResponse:
    return _page("docs.html")


# The landing page's furniture: section styles, one file per component, and the
# platform marks. A single mount, because there is a file per component and a
# route each would be a dozen lines of nothing. `app.css`/`app.js` stay explicit
# above: they are the app, and they keep their no-cache header.
app.mount("/ui", StaticFiles(directory=os.path.join(STATIC_DIR, "ui")), name="ui")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Browsers ask for this on every single page load.

    Without a route every page logged a 404 in the console, which made "zero
    page errors" a claim the browser plainly contradicted. The mark is an SVG,
    so one small file covers every size the tab, bookmark bar and home screen ask
    for, and it is served for the .ico path as well because that is the path a
    browser reaches for.
    """
    return FileResponse(os.path.join(STATIC_DIR, "ui", "favicon.svg"),
                        media_type="image/svg+xml")


@app.get("/app.css", include_in_schema=False)
def asset_css() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "app.css"), media_type="text/css", headers=_NO_CACHE)


@app.get("/app.js", include_in_schema=False)
def asset_js() -> FileResponse:
    return FileResponse(
        os.path.join(STATIC_DIR, "app.js"), media_type="application/javascript", headers=_NO_CACHE
    )


@app.get("/info")
def info() -> dict:
    words = generator.words_by_length()
    return {
        "service": "bahs",
        "build": BUILD,
        "console": "/console",
        "platforms": list(sniper.PLATFORMS),
        # One line per platform on how its verdict is reached: the Platforms page
        # shows them so nobody has to take a verdict on faith.
        "checks": [{"platform": p, "how": sniper.CHECK_NOTES.get(p, "")} for p in sniper.PLATFORMS],
        # What the numbers mean, stated once, where the client reads it.
        "honesty": {
            "results": "only available and taken are verdicts; refusals, errors and unreadable answers are health, not results",
            "rates": "checks_per_second counts requests completed, verdicts_per_second counts names resolved",
            "free": (
                "a free verdict is re-asked once on a different exit and reported confirmed or "
                "unconfirmed -- except on tiktok, where a page read cannot prove a handle is "
                "claimable, so it stays a lead (unconfirmed, and it says so) unless "
                "TIKTOK_SESSION_ID is set, in which case TikTok's own validator settles it and "
                "the hit is confirmed"
            ),
        },
        "alerts_enabled": bool(alert_settings.get().get("enabled")),
        # Whether a free TikTok read can be settled by TikTok's own validator.
        # The UI says which of the two honest answers it will get.
        "tiktok_validator": bool(sniper.TIKTOK_SESSION_ID.strip()),
        # What the UI needs to show a running total for "every name of this
        # length" before anyone starts a run.
        "generation": {
            "name_order": "random" if SHUFFLE_NAMES else "sequential",
            "charset": {name: len(chars) for name, chars in generator.CHARSETS.items()},
            "words_by_length": words,
            "max_enumeration": MAX_ENUMERATION,
            # The largest selection a run will build in memory and shuffle. Past
            # it the run needs a cap and samples at random -- the client uses this
            # to say so before you press start, not after.
            "selection_build_max": SELECTION_BUILD_MAX,
            "note": "limit omitted or 0 means every name of that length",
        },
        "endpoints": {
            "GET /": "the landing page",
            "GET /console": "the console - start and watch a run",
            "GET /stats": "today's totals: checked, verdicts, hits, average rates",
            "GET /hits": "found names, newest first, with confirmation state",
            "GET /platforms": "the sniper grid and live per-platform counters",
            "GET /proxies": "the proxy list",
            "GET /alerts": "webhook alerts",
            "GET /settings": "customisation",
            "GET /docs": "how a verdict is reached, and every endpoint",
            "GET /hits": "found names, newest first, with confirmation state",
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
            "GET /runs": "in-flight and just-finished runs, with live counters",
            "GET /runs/{id}": "one run's counters",
            "GET /runs/{id}/stream": "reattach to a run that is still going",
            "POST /runs/stop": "stop all runs, or the one named by ?run_id=",
            "GET /generate": "candidate usernames: letters | alnum | numbers | words, by length",
            "POST /generate": "the same for several patterns at once",
            "GET /claim": "the registration route for a name on a platform",
            "GET /settings": "alert settings (webhook url, template, ping)",
            "POST /settings": "change the alert settings",
            "POST /settings/test-webhook": "post one test message to the webhook",
        },
    }


@app.get("/stats")
def stats() -> dict:
    """Today's totals, with whatever is running right now folded in.

    The landing page's strip reads this. Everything in it is counted from real
    runs: names actually checked, verdicts actually resolved, free names actually
    found. A fresh install honestly reports zeroes rather than a flattering
    placeholder.
    """
    live_checks = live_verdicts = live_hits = 0
    for handle in _runs_status():
        if not handle.get("active"):
            continue
        progress = handle.get("progress") or {}
        live_checks += int(progress.get("checked") or 0)
        live_verdicts += int(progress.get("answered") or 0)
        live_hits += int(progress.get("available_count") or 0)
    return {
        "service": "bahs",
        "build": BUILD,
        "platforms": len(sniper.PLATFORMS),
        "proxies": len(proxy_list),
        "hits_remembered": len(_HITS),
        "active_runs": sum(1 for h in _runs.values() if h.active),
        **daily_stats.snapshot(live_checks, live_verdicts, live_hits),
    }


@app.get("/health")
def health() -> dict:
    runs = _runs_status()
    return {
        "status": "ok",
        "build": BUILD,
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
    """One in-flight snipe or scan -- and it outlives whoever is watching it.

    The run is driven by its own task, not by the HTTP response that started it,
    because a sweep is not something a browser tab should be able to kill by
    being closed, backgrounded, or losing signal. Every NDJSON line it produces
    is appended to a bounded ring buffer, and readers follow that buffer: the tab
    that started the run, a tab that comes back to it later
    (``GET /runs/{id}/stream``), or nobody at all. The run ends when it runs out
    of names, when the pool is exhausted, or when ``stop`` is set from
    ``/scan/stop`` -- and for no other reason.

    ``stop`` and every other mutation happen on the event loop with no awaits
    between the read and the write, so no lock is needed.
    """

    # How many NDJSON lines are held for a reader that arrives late. A wide run
    # emits far more than this, so a tab that reattaches sees the recent tail and
    # the counters rather than the whole history -- which is what it draws.
    BUFFER = 4000

    __slots__ = (
        "id", "kind", "stop", "started", "finished", "checked", "task", "tally",
        "rows", "keep_rows", "note", "names", "platforms", "final",
        "stats_recorded", "_lines", "_first", "_last", "_waiters",
    )

    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.stop = asyncio.Event()
        self.started = time.time()
        self.finished: float | None = None
        self.checked = 0
        self.task: asyncio.Task | None = None
        self.tally: "_Tally | None" = None
        # Only a buffered run keeps its rows: a streamed one would hold the whole
        # sweep in memory for a client that is being handed every line anyway.
        self.rows: list[dict] = []
        self.keep_rows = False
        self.note = ""
        self.names = 0
        self.platforms: list[str] = []
        self.final = False
        # Set once this run's counters are folded into the day's totals, so a
        # run that is reaped and re-read cannot be counted twice.
        self.stats_recorded = False
        self._lines: deque[str] = deque(maxlen=self.BUFFER)
        self._first = 0   # index of the oldest line still held
        self._last = 0    # index one past the newest
        self._waiters: list[asyncio.Event] = []

    @property
    def active(self) -> bool:
        return self.finished is None

    def finish(self) -> None:
        if self.finished is None:
            self.finished = time.time()

    # --- the feed ---------------------------------------------------------
    def emit(self, line: str) -> None:
        """Publish one NDJSON line: into the ring buffer and out to readers."""
        self._lines.append(line)
        self._last += 1
        self._first = self._last - len(self._lines)
        for waiter in self._waiters:
            waiter.set()

    def close(self) -> None:
        """No more lines are coming, so a reader following the live edge ends."""
        self.final = True
        for waiter in self._waiters:
            waiter.set()

    @property
    def start_index(self) -> int:
        """The oldest line still held, for a reader that wants the whole tail."""
        return self._first

    async def follow(self, since: int = 0) -> AsyncIterator[str]:
        """Yield the run's lines from ``since``, then live ones, until it ends.

        Closing this generator -- which is exactly what a client going away does
        -- removes this reader and nothing else. The run is untouched.
        """
        index = max(0, since)
        waiter = asyncio.Event()
        self._waiters.append(waiter)
        try:
            while True:
                while index < self._last:
                    if index < self._first:
                        # This reader fell further behind than the buffer holds.
                        # Skip to the oldest line still here rather than replaying
                        # history that is already gone.
                        index = self._first
                    yield self._lines[index - self._first]
                    index += 1
                if self.final and index >= self._last:
                    return
                waiter.clear()
                # No await between the check above and this wait, so a line
                # emitted in between cannot be missed.
                await waiter.wait()
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def status(self) -> dict:
        out = {
            "id": self.id,
            "kind": self.kind,
            "started": self.started,
            "finished": self.finished,
            "checked": self.checked,
            "names": self.names,
            "platforms": self.platforms,
            "note": self.note,
            "stopping": self.stop.is_set(),
            "active": self.active,
            "final": self.final,
            "cursor": self._last,
            "elapsed_s": round((self.finished or time.time()) - self.started, 2),
        }
        if self.tally is not None:
            out["progress"] = self.tally.progress()
        return out


_runs: dict[str, RunHandle] = {}


# How long a finished run stays readable after it ends. A tab that was closed
# (or a phone that slept) comes back to the tail of its run rather than to
# nothing, and a run that ended while nobody was watching can still be read.
RUN_TTL: float = 900.0


def _reap_runs() -> None:
    """Drop runs that finished long enough ago that nobody is coming back."""
    now = time.time()
    for run_id in [
        rid for rid, handle in _runs.items()
        if handle.finished is not None and now - handle.finished > RUN_TTL
    ]:
        _runs.pop(run_id, None)


def _open_run(kind: str) -> RunHandle:
    """Register a run: one per kind, and up to ``MAX_CONCURRENT_RUNS`` overall.

    One run per kind because the proxy pool is cached per kind: a second run of
    the same kind would reset the first one's retirements and strikes out from
    under it (and share its warm tunnels). Refusing says so honestly, and names
    the run that is already going so the caller can reattach to that one instead
    of losing it.
    """
    _reap_runs()
    active = [handle for handle in _runs.values() if handle.active]
    for handle in active:
        if handle.kind == kind:
            raise HTTPException(
                409,
                f"a {kind} run is already in flight ({handle.id}) -- reattach with "
                f"GET /runs/{handle.id}/stream, or stop it with POST /runs/stop",
            )
    if len(active) >= MAX_CONCURRENT_RUNS:
        raise HTTPException(429, f"too many runs in flight (max {MAX_CONCURRENT_RUNS})")
    handle = RunHandle(kind)
    _runs[handle.id] = handle
    return handle


async def _pump(run: RunHandle, lines: AsyncIterator[str]) -> None:
    """Drive a run to completion, publishing every line it produces.

    Nothing here depends on a client being connected: this task owns the sweep.
    Closing the tab, losing signal or backgrounding the browser ends a *reader*,
    never this.
    """
    try:
        async for line in lines:
            run.emit(line)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - the generators are guarded
        log.warning("run %s ended on %s", run.id, type(exc).__name__, exc_info=True)
    finally:
        run.close()
        # The run is done, whatever ended it, so today's totals can have its
        # numbers. Done here rather than in a client's finally because a run
        # outlives every reader it ever had.
        try:
            daily_stats.record(run)
        except Exception:  # pragma: no cover - accounting must never kill a run
            log.warning("could not record run %s in today's totals", run.id, exc_info=True)


def _detach(run: RunHandle, lines: AsyncIterator[str]) -> None:
    """Start a run in the background. Its HTTP response is only a window on it."""
    run.task = asyncio.create_task(_pump(run, lines))


NDJSON_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


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


@app.get("/runs")
def runs_status() -> dict:
    """Every tracked run, active or just finished.

    Newest first. The dict is in insertion order, which is oldest first, and the
    console reads ``runs[0]`` as "the last run" -- so it was showing the oldest
    run still in memory (up to RUN_TTL old) as the one that had just finished.
    """
    runs = _runs_status()
    return {"count": len(runs), "runs": runs, "max": MAX_CONCURRENT_RUNS,
            "hits_count": len(_HITS)}


def _runs_status() -> list[dict]:
    _reap_runs()
    return sorted((handle.status() for handle in _runs.values()),
                  key=lambda r: r["started"], reverse=True)


@app.get("/hits")
def hits(
    platform: str | None = Query(None),
    confirmed: bool | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
) -> dict:
    """Every free name the process has found, newest first.

    Only names a checker returned as `available` are here, and each row carries
    whether a second exit agreed. A name found on one exit's word and never
    re-confirmed is shown as unconfirmed -- the page is not allowed to present
    it as a sure thing.
    """
    rows = list(_HITS.values())
    if platform:
        rows = [r for r in rows if r["platform"].lower() == platform.strip().lower()]
    if confirmed is not None:
        rows = [r for r in rows if bool(r["confirmed"]) is confirmed]
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {
        "count": len(_HITS),
        "returned": len(rows[:limit]),
        "confirmed": sum(1 for r in _HITS.values() if r["confirmed"]),
        "hits": rows[:limit],
    }


@app.delete("/hits")
def clear_hits() -> dict:
    """Forget the found names. Does not touch a run that is in flight."""
    removed = len(_HITS)
    _HITS.clear()
    _HIT_ORDER.clear()
    return {"cleared": removed}


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


@app.get("/runs/{run_id}")
def run_status(run_id: str) -> dict:
    """One run's counters -- what a returning tab paints before it reattaches."""
    _reap_runs()
    handle = _runs.get(run_id)
    if handle is None:
        raise HTTPException(404, "no such run")
    return handle.status()


@app.get("/runs/{run_id}/stream")
async def run_stream(run_id: str, since: int = Query(-1, ge=-1)) -> StreamingResponse:
    """Follow a run that is already going. Cutting this does not stop it.

    ``since`` is the cursor from ``GET /runs/{id}``; the default replays the
    oldest lines still buffered, which is what a tab that just came back wants.
    """
    _reap_runs()
    handle = _runs.get(run_id)
    if handle is None:
        raise HTTPException(404, "no such run")
    return StreamingResponse(
        handle.follow(handle.start_index if since < 0 else since),
        media_type="application/x-ndjson",
        headers=NDJSON_HEADERS,
    )


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
    # A fresh run gets fresh verdicts. The pool object is cached (and its warm
    # tunnels with it), but the previous run's retirements and strikes must not
    # decide this one -- otherwise a list that recovered still reads "the list
    # is dead" until the cache expires.
    pool.begin_run()
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
        "checked": 0, "available": 0, "taken": 0, "invalid": 0, "banned": 0,
        "blocked": 0, "error": 0, "unanswered": 0,
    }

    def __init__(
        self,
        names: int,
        platforms: list[str],
        alerter: Alerter | None = None,
        pool: "sniper.ProxyPool | None" = None,
    ) -> None:
        self.names = names
        self.platforms = platforms
        # The pool is where misses are counted now. A name the pool could not
        # carry is never emitted as a result -- a run must not file an error
        # against a name it never asked about -- so there is nothing in the
        # stream to count and the pool is the only honest source.
        self.pool = pool
        # A run costs names x platforms, not names. Reporting progress against
        # the name count alone showed 0% on a run that was a third of the way
        # through, because every name is checked once per platform.
        self.total_checks = max(1, names * max(1, len(platforms)))
        self.alerter = alerter
        # Two meters, because they measure two different things and reporting
        # the wrong one is how a dashboard lies about its own speed. `meter`
        # ticks on every result -- requests the run completed, refusals included.
        # `answer_meter` ticks only on a result that actually answered the
        # question about the name. A run that is 80% blocked is fast on the first
        # and slow on the second, and only the second is "names resolved".
        self.meter = sniper.RateMeter()
        self.answer_meter = sniper.RateMeter()
        self.checked = 0
        self.available = 0
        self.taken = 0
        self.errors = 0
        self.unanswered = 0
        self.banned = 0
        self.answered = 0
        self.confirmed = 0
        self.corrected = 0
        # Filled by the run's generator so a hit can be traced back to its run.
        self.run_id = ""
        self.rate_limits = 0
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
        if status == "taken":
            self.taken += 1
        elif status == "error":
            self.errors += 1
        elif status == "unanswered":
            self.unanswered += 1
        elif status == "banned":
            self.banned += 1
        if status == "available":
            self.available += 1
            # Queued, not sent: the alerter digests and posts on its own clock.
            if self.alerter is not None:
                self.alerter.offer(result.username, result.platform, result.detail)
        if result.answered:
            self.answered += 1
            self.answer_meter.tick()
        if getattr(result, "confirmed", False):
            self.confirmed += 1
        if getattr(result, "corrected", False):
            self.corrected += 1
        if status == "available" and result.username:
            _record_hit(
                result.username, result.platform, result.detail,
                getattr(result, "confirmed", False), getattr(result, "corrected", False),
                self.run_id,
            )
        if status == "blocked":
            self.rate_limits += 1

    @property
    def elapsed(self) -> float:
        return self.meter.elapsed

    @property
    def unattempted(self) -> int:
        """Checks the run never got to, because the pool died before them.

        Not the same as a proxy miss: a miss is a name the pool refused when it
        was handed one, this is the rest of the list being abandoned when the
        run stopped early. Both are checks that never happened, and a summary
        that reports only the ones it attempted makes a sweep that died at name
        6 of 500 look like it simply finished. Names dropped because a platform
        was sitting out are counted separately, by `skipped`.
        """
        skipped = self.pool.skipped if self.pool is not None else 0
        return max(0, self.total_checks - self.checked - skipped)

    @property
    def proxy_misses(self) -> int:
        """Names the pool could not carry. Zero without a pool attached, which
        is what a unit test tallying a hand-built list of results gets."""
        return self.pool.missed() if self.pool is not None else 0

    def progress(self) -> dict:
        """The per-tick line: everything the dashboard moves while a run is live."""
        return {
            "checked": self.checked,
            "names": self.names,
            "total_checks": self.total_checks,
            "platforms_count": len(self.platforms),
            "progress": round(self.checked / self.total_checks, 4),
            # Checks are requests completed; verdicts are names resolved. Both
            # are sent, explicitly named, and `per_second` stays as an alias of
            # the check rate so nothing can silently read the flattering number.
            "checks_per_second": round(self.meter.recent, 1),
            "verdicts_per_second": round(self.answer_meter.recent, 1),
            "per_second": round(self.meter.recent, 1),
            "average_per_second": round(self.meter.average, 1),
            "average_verdicts_per_second": round(self.answer_meter.average, 1),
            "answered": self.answered,
            "available_count": self.available,
            "taken": self.taken,
            "banned": self.banned,
            "error": self.errors,
            "unanswered": self.unanswered,
            "rate_limits": self.rate_limits,
            "proxy_misses": self.proxy_misses,
            "unattempted": self.unattempted,
            "current_target": self.current,
            "elapsed_s": round(self.elapsed, 2),
            "by_platform": self.by_platform,
            # Platforms the pacer is asking slower right now. Sending it with
            # every tick means a tab that attaches mid-run can see the throttle
            # being handled instead of just a stalled counter.
            "platforms_paced": _pace_snapshot(self.pool),
            # Platforms sat out entirely right now, with how long is left and
            # why. The sniper grid colours a cell from this, so a throttled
            # platform looks throttled instead of merely quiet.
            "platforms_paused": self.pool.paused(),
            # The pool's own numbers, streamed with the tick: the console's proxy
            # KPI and the health panel both read them, and a run whose exits are
            # dying should be able to say so while it is still running rather
            # than only in the summary at the end.
            "pool_size": len(self.pool),
            "pool_alive": self.pool.alive(),
            "pool_resting": self.pool.resting(),
            "pool_retired": self.pool.retired(),
            "pool_blocked": self.pool.blocked(),
            "proxy_attempts": self.pool.attempts(),
            "proxy_failures": self.pool.failures(),
            "confirmed_count": self.confirmed,
            "corrected_count": self.corrected,
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
            # The per-status counters the console paints its KPI row and health
            # panel from. They were only in progress(), so the final `done` line
            # -- the one everybody reads -- reported taken 0, unanswered 0 and
            # errors 0 no matter what the run had actually found.
            "taken": self.taken,
            "banned": self.banned,
            "unanswered": self.unanswered,
            "error": self.errors,
            "rate_limits": self.rate_limits,
            "proxy_misses": self.proxy_misses,
            "unattempted": self.unattempted,
            "current_target": self.current,
            "elapsed_s": round(self.elapsed, 2),
            "checks_per_second": round(self.meter.average, 1),
            "verdicts_per_second": round(self.answer_meter.average, 1),
            "average_verdicts_per_second": round(self.answer_meter.average, 1),
            "answered": self.answered,
            "confirmed_count": self.confirmed,
            "corrected_count": self.corrected,
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
        "confirmed": bool(getattr(result, "confirmed", False)),
        "corrected": bool(getattr(result, "corrected", False)),
    }


def _pace_snapshot(pool: sniper.ProxyPool) -> dict:
    """The pacer's current per-platform caps, or empty when there is no pacer."""
    pace = getattr(pool, "pace", None)
    return pace.snapshot() if pace is not None else {}


def _pool_report(pool: sniper.ProxyPool) -> dict:
    pace = getattr(pool, "pace", None)
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
        # Platforms the run is deliberately asking *slower*, with the numbers
        # behind the decision. That is what a throttle looks like when it is
        # being handled rather than merely survived: the platform keeps producing
        # verdicts, and the dashboard can say which one was cut and by how much
        # instead of showing a wall of refusals.
        "platforms_paced": _pace_snapshot(pool),
        "pace_adjustments": pace.recent() if pace is not None else [],
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


def _buffered_report(
    run: RunHandle,
    pool: sniper.ProxyPool,
    note: str,
    alerter: Alerter | None,
    names_total: int,
    truncated: bool,
) -> dict:
    """A finished buffered run as one JSON document.

    Built from the run rather than from a list this request collected, so the
    answer is the same whether the caller stayed connected for it or came back
    to it afterwards.
    """
    tally = run.tally or _Tally(names_total, run.platforms, alerter, pool)
    rows = run.rows
    available = [r for r in rows if r["status"] == "available"]
    return {
        "run_id": run.id,
        "stopped": run.stop.is_set(),
        "names": names_total,
        "truncated": truncated,
        "platforms": run.platforms,
        "pool_note": note,
        "available": available,
        "results": rows,
        "alerts": _alert_report(alerter),
        **_pool_report(pool),
        **tally.summary(),
    }


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
    tally = _Tally(len(names), platforms, alerter, pool)
    if run:
        run.tally = tally
    run_id = run.id if run else ""
    tally.run_id = run_id
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
            if result.status == sniper.HEARTBEAT_STATUS:
                # Not a row: a keep-alive for a slow stretch.
                yield json.dumps({"type": "heartbeat", **tally.progress()}) + "\n"
                continue
            tally.add(result)
            row = _snipe_row(result)
            if run:
                run.checked = tally.checked
                if run.keep_rows:
                    run.rows.append(row)
            yield json.dumps({"type": "result", **row}) + "\n"
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
    result per line, so a big batch lands incrementally. As with /scan the run is
    detached from the response: it finishes whether or not anybody is still
    reading, and ``GET /runs/{id}/stream`` picks it up again.
    """
    names, platforms, pool, concurrency, retries, note = await _snipe_ready(req)
    run = _open_run("snipe")
    run.names, run.platforms, run.note = len(names), platforms, note
    run.keep_rows = not req.stream
    alerter = Alerter(alert_settings.get())
    _detach(
        run,
        _snipe_ndjson(names, platforms, pool, concurrency, retries, note, run, alerter),
    )
    if req.stream:
        return StreamingResponse(
            run.follow(0), media_type="application/x-ndjson", headers=NDJSON_HEADERS
        )
    await run.task
    return _buffered_report(run, pool, note, alerter, len(names), False)


@app.get("/snipe")
async def snipe_one(
    username: str = Query(..., min_length=1, max_length=32),
    platform: list[str] | None = Query(None),
):
    """Same thing for one name, from the query string."""
    req = SnipeRequest(username=username, platforms=platform)
    return await snipe_batch(req)


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
        # Random draw, not enumeration order. `req.limit` is the cap: left at 0
        # the whole selection is shuffled and checked (up to the size one run
        # will build); set, that many names are drawn at random from the whole
        # space, and `total` below is the sample size so progress is measured
        # against what will actually be checked.
        try:
            generated = generator.iter_selection(
                specs, words=req.words, seed=req.seed, limit=req.limit
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        total = generator.sample_size(specs, req.limit, req.words)
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
    already in flight instead of only ending the response. Nothing else ends a
    run -- a client going away is not a reason, which is why this generator is
    pumped by the run's own task rather than by the response that started it.
    """
    tally = _Tally(names_total, platforms, alerter, pool)
    if run:
        run.tally = tally
    run_id = run.id if run else ""
    tally.run_id = run_id
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
            if result.status == sniper.HEARTBEAT_STATUS:
                # Not a row: a keep-alive so a slow stretch is not read as a
                # dropped connection. Carries live counters so the dashboard
                # stays painted while the checks are slow.
                yield json.dumps({"type": "heartbeat", **tally.progress()}) + "\n"
                continue
            tally.add(result)
            row = _snipe_row(result)
            if run:
                run.checked = tally.checked
                if run.keep_rows:
                    run.rows.append(row)
            yield json.dumps({"type": "result", **row}) + "\n"
            now = time.perf_counter()
            if tally.checked % 10 == 0 and (now - last_report) >= 0.12:
                last_report = now
                yield json.dumps({"type": "progress", **tally.progress()}) + "\n"
                await _flush_alerts(alerter)
    except Exception as exc:  # pragma: no cover - a stream must terminate cleanly
        # A run that dies mid-response used to just drop the socket, which the
        # browser reports as `stream error: Load failed` with no explanation.
        # Emit a real `done` so the dashboard can say what happened.
        log.exception("scan run failed mid-stream")
        yield json.dumps({
            "type": "done",
            "run_id": run_id,
            "stopped": False,
            "names": names_total,
            "platforms": platforms,
            "pool_note": note,
            "per_second": round(tally.meter.average, 1),
            "duration_ms": round(tally.elapsed * 1000, 1),
            "alerts": _alert_report(alerter),
            **_pool_report(pool),
            # After _pool_report, which carries an empty stop_reason of its own.
            "stop_reason": f"the run ended on an internal error: {type(exc).__name__}",
            **tally.summary(),
        }) + "\n"
        return
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
            "stop_reason": pool.stop_reason,
            **tally.summary(),
        }
    ) + "\n"


@app.post("/scan")
async def scan(req: ScanRequest):
    """Bulk-check generated names, through the stored proxies.

    NDJSON by default so a long run can be watched live; ``"stream": false``
    buffers the same data into one JSON document.

    Either way the run itself is detached: it is driven by its own task and the
    response is only a window on it. Closing the tab, losing signal or
    backgrounding the browser ends the *window*, not the sweep -- it keeps
    checking until you stop it or the pool is exhausted, and
    ``GET /runs/{id}/stream`` picks the feed back up where it is.
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
    run.names, run.platforms, run.note = names_total, platforms, note
    run.keep_rows = not req.stream
    alerter = Alerter(alert_settings.get())
    _detach(
        run,
        _scan_stream(
            names, names_total, platforms, pool, concurrency, retries, truncated, note, run, alerter
        ),
    )
    if req.stream:
        return StreamingResponse(
            run.follow(0), media_type="application/x-ndjson", headers=NDJSON_HEADERS
        )
    # Buffered: wait for the run rather than driving it. A dropped client does
    # not take the run with it -- it still finishes and stays readable from
    # /runs/{id}.
    await run.task
    return _buffered_report(run, pool, note, alerter, names_total, truncated)


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
    "discord": {
        "register_url": "https://discord.com/register",
        "existing_account": "User Settings > My Account > Username",
        "note": "Discord adopts a username at signup, or changes it from an existing account's settings.",
    },
    "instagram": {
        "register_url": "https://www.instagram.com/accounts/emailsignup/",
        "existing_account": "Settings > Edit profile > Username",
        "note": "Instagram assigns the handle at signup.",
    },
    "tiktok": {
        "register_url": "https://www.tiktok.com/signup",
        "existing_account": "Profile > Edit profile > Username",
        "note": "TikTok assigns the handle at signup, and refuses a handle made only of digits or one ending in a period. A handle with no profile can still be held by a banned or deactivated account, so a free verdict here is a lead -- check it in the app.",
    },
    "x": {
        "register_url": "https://x.com/i/flow/signup",
        "existing_account": "Settings > Your account > Account information > Username",
        "note": "X assigns the handle at signup and allows changes from settings.",
    },
    "youtube": {
        "register_url": "https://www.youtube.com/handle",
        "existing_account": "youtube.com/handle",
        "note": "A YouTube handle is claimed once the channel has one, at /handle.",
    },
    "facebook": {
        "register_url": "https://www.facebook.com/reg/",
        "existing_account": "Settings & privacy > Settings > Accounts Centre > Username",
        "note": "Facebook takes the username at signup, or from an existing account's settings.",
    },
    "reddit": {
        "register_url": "https://www.reddit.com/register/",
        "existing_account": "Settings > Account > Username",
        "note": "Reddit shows whether a name is free as you type it at signup, and allows one change in settings.",
    },
    "telegram": {
        "register_url": "https://web.telegram.org/",
        "existing_account": "Settings > Username",
        "note": "Telegram handles are set inside the app once you have an account.",
    },
    "github": {
        "register_url": "https://github.com/signup",
        "existing_account": "Settings > Account > Change username",
        "note": "GitHub redirects the old name to the new one after a change.",
    },
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
    "steam": {
        "register_url": "https://store.steampowered.com/join",
        "existing_account": "Profile > Edit Profile > Custom URL",
        "note": "The vanity URL is the custom URL on a Steam profile, not the account name.",
    },
    "kick": {
        "register_url": "https://kick.com/",
        "existing_account": "Settings > Account > Username",
        "note": "Kick takes the username at signup; a change is a paid one.",
    },
    "bluesky": {
        "register_url": "https://bsky.app/",
        "existing_account": "Settings > Change handle",
        "note": "A Bluesky handle is a domain; the .bsky.social one can be changed in settings.",
    },
    "guns.lol": {
        "register_url": "https://guns.lol/register",
        "existing_account": "Dashboard > Settings > Username",
        "note": "guns.lol assigns the handle when you register.",
    },
    "huggingface": {
        "register_url": "https://huggingface.co/join",
        "existing_account": "Settings > Username",
        "note": "A Hugging Face username is the account's namespace; it can be renamed in settings.",
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
