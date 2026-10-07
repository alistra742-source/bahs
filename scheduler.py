"""Refresh scheduler: periodic scrape -> validate -> prune -> persist cycles.

One cycle is the unit of freshness. It runs in the background on an interval
and can also be triggered on demand; a lock guarantees only one cycle runs at
a time regardless of how it was started.
"""

import asyncio
import logging
import time
from typing import Any

from checker import get_direct_ip, iter_checks
from config import (
    LOW_POOL_INTERVAL,
    MAX_CANDIDATES,
    MAX_CONCURRENCY,
    MIN_ALIVE,
    PROGRESS_EVERY,
    REFRESH_INTERVAL,
    REFRESH_ON_START,
    STORE_SAVE_INTERVAL,
)
from sources import fetch_all
from store import ProxyStore

log = logging.getLogger("proxy-scraper.scheduler")


class RefreshManager:
    def __init__(self, store: ProxyStore) -> None:
        self.store = store
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._stop.set()
        self.direct_ip: str | None = None

        # enabled = the operator has pressed Start; running = a cycle is in flight.
        self.enabled = False
        self.running = False
        self.cycles = 0
        self.last_started: float | None = None
        self.last_finished: float | None = None
        self.last_duration_s: float | None = None
        self.last_candidates = 0
        self.last_alive = 0
        self.last_error = ""

    async def run_cycle(self) -> dict[str, Any]:
        """Scrape, validate, update the store, prune and persist. Idempotent under the lock."""
        async with self._lock:
            self.running = True
            self.last_started = time.time()
            self.last_error = ""
            started = time.perf_counter()
            try:
                if self.direct_ip is None:
                    self.direct_ip = await get_direct_ip()
                    log.info("validator direct IP: %s", self.direct_ip or "unknown")

                fresh = await fetch_all()
                candidates = self.store.candidate_proxies(fresh)[:MAX_CANDIDATES]
                self.last_candidates = len(candidates)

                # Store each verdict as it lands: a cycle over 20k candidates runs for many
                # minutes, and the dashboard must fill while it runs rather than after it ends.
                checked = 0
                last_save = time.monotonic()
                async for result in iter_checks(candidates, self.direct_ip, MAX_CONCURRENCY):
                    self.store.upsert(result)
                    checked += 1
                    # Persist on a wall clock, not a check count: the save is a
                    # full-store dump, so its cost must not scale with how fast
                    # the validator happens to be running.
                    now = time.monotonic()
                    if now - last_save >= STORE_SAVE_INTERVAL:
                        last_save = now
                        self.store.save()
                    if checked % PROGRESS_EVERY == 0:
                        stats = self.store.stats()
                        log.info(
                            "validated %d/%d -- %d alive, %d pass all platforms",
                            checked,
                            len(candidates),
                            stats["alive"],
                            stats["by_platform"]["discord"],
                        )

                dropped = self.store.prune()
                self.store.save(force=True)

                alive = self.store.stats()["alive"]
                self.last_alive = alive
                self.last_duration_s = round(time.perf_counter() - started, 2)
                self.cycles += 1
                log.info(
                    "cycle %d done: %d candidates, %d alive, %d dropped, %.1fs",
                    self.cycles,
                    len(candidates),
                    alive,
                    dropped,
                    self.last_duration_s,
                )
                return {
                    "candidates": len(candidates),
                    "alive": alive,
                    "dropped": dropped,
                    "duration_s": self.last_duration_s,
                }
            except Exception as exc:  # pragma: no cover - keep the loop alive
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_duration_s = round(time.perf_counter() - started, 2)
                log.exception("refresh cycle failed")
                return {"error": self.last_error}
            finally:
                # A cancelled cycle (Stop pressed mid-run) keeps what it already validated.
                try:
                    self.store.save(force=True)
                except OSError:
                    log.warning("could not persist the store at the end of the cycle", exc_info=True)
                self.running = False
                self.last_finished = time.time()

    async def trigger(self) -> None:
        """Kick a cycle in the background if one is not already running."""
        if self.running:
            return
        asyncio.create_task(self.run_cycle())

    def next_wait(self) -> float:
        """Seconds until the next cycle.

        A pool below ``MIN_ALIVE`` cannot answer a scan -- every check comes
        back an error -- so while it is that thin the loop retries on
        ``LOW_POOL_INTERVAL`` instead of sleeping a full interval on a list that
        cannot check anything. A fresh deploy with no mounted volume starts
        empty, which is exactly that case.
        """
        alive = self.store.stats()["alive"]
        return LOW_POOL_INTERVAL if alive < MIN_ALIVE else REFRESH_INTERVAL

    async def _loop(self) -> None:
        if REFRESH_ON_START:
            await self.run_cycle()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.next_wait())
            except asyncio.TimeoutError:
                await self.run_cycle()

    def start(self) -> None:
        """Begin the periodic loop (idempotent)."""
        self.enabled = True
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Halt the periodic loop. Any in-flight cycle is cancelled."""
        self.enabled = False
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.warning("scheduler loop ended with an error", exc_info=True)
            self._task = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "cycles": self.cycles,
            "interval_s": REFRESH_INTERVAL,
            "last_started": self.last_started,
            "last_finished": self.last_finished,
            "last_duration_s": self.last_duration_s,
            "last_candidates": self.last_candidates,
            "last_alive": self.last_alive,
            "direct_ip": self.direct_ip,
            "last_error": self.last_error,
        }
