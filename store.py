"""Proxy record store: scoring, filtering, pruning, JSON persistence.

A record is the remembered verdict on one proxy. Scoring is deterministic and
recomputed on every validation, so a proxy's rank always reflects its most
recent check rather than history.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from checker import CheckResult
from config import (
    FAIL_COOLDOWN,
    MAX_DEAD_REMEMBERED,
    MAX_FAILURES,
    MAX_LATENCY_MS,
    STALE_AFTER,
    W_ANONYMITY,
    W_LATENCY,
    W_PLATFORM,
)

log = logging.getLogger("proxy-scraper.store")

ANONYMITY_SCORE = {"elite": 1.0, "anonymous": 0.6, "transparent": 0.1, "unknown": 0.0}


@dataclass
class ProxyRecord:
    proxy: str
    protocol: str
    host: str
    port: int
    first_seen: float
    last_checked: float
    last_ok: float | None = None
    fail_count: int = 0
    latency_ms: float | None = None
    anonymity: str = "unknown"
    exit_ip: str | None = None
    platforms: dict[str, dict[str, Any]] = field(default_factory=dict)
    score: float = 0.0

    @property
    def platforms_passed(self) -> int:
        return sum(1 for p in self.platforms.values() if p.get("ok"))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["platforms_passed"] = self.platforms_passed
        return data


def compute_score(record: ProxyRecord) -> float:
    anon = ANONYMITY_SCORE.get(record.anonymity, 0.0)
    if record.latency_ms is None:
        lat = 0.0
    else:
        lat = max(0.0, 1.0 - (record.latency_ms / MAX_LATENCY_MS))
    platform_total = len(record.platforms) or 1
    plat = record.platforms_passed / platform_total
    weight = W_ANONYMITY + W_LATENCY + W_PLATFORM
    if weight <= 0:
        return 0.0
    return round(100.0 * (anon * W_ANONYMITY + lat * W_LATENCY + plat * W_PLATFORM) / weight, 2)


class ProxyStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.records: dict[str, ProxyRecord] = {}
        # Set by every mutation, cleared by save(). The scheduler saves on a
        # wall clock, so without this a quiet interval would still rewrite the
        # whole store; with it a save with nothing new to say is free.
        self.dirty = False
        # Cumulative, survives restarts. total_seen counts distinct proxies ever
        # tracked; total_checks counts every validation run, dead or alive.
        self.total_seen = 0
        self.total_checks = 0
        # proxy key -> unix timestamp of the last time it failed. Kept for
        # FAIL_COOLDOWN so a scrape that re-offers a host we just proved dead
        # does not spend another timeout on it.
        self.dead: dict[str, float] = {}

    # --- mutation ---------------------------------------------------------
    def upsert(self, result: CheckResult, now: float | None = None) -> ProxyRecord:
        now = now if now is not None else time.time()
        record = self.records.get(result.proxy)
        if record is None:
            record = ProxyRecord(
                proxy=result.proxy,
                protocol=result.protocol,
                host=result.host,
                port=result.port,
                first_seen=now,
                last_checked=now,
            )
            self.records[result.proxy] = record
            self.total_seen += 1
        self.total_checks += 1
        self.dirty = True

        record.last_checked = now
        if result.ok:
            record.last_ok = now
            record.fail_count = 0
            record.latency_ms = result.latency_ms
            record.anonymity = result.anonymity
            record.exit_ip = result.exit_ip
            record.platforms = {
                name: {
                    "ok": pr.ok,
                    "status": pr.status,
                    "latency_ms": round(pr.latency_ms, 2) if pr.latency_ms is not None else None,
                    "reason": pr.reason,
                }
                for name, pr in result.platforms.items()
            }
        else:
            record.fail_count += 1
            # A failed check must not leave a stale verdict ranked highly.
            record.latency_ms = None
            record.platforms = {}
            record.anonymity = "unknown"

        record.score = compute_score(record)
        return record

    def remove(self, proxy: str) -> bool:
        """Delete one proxy by its canonical key. Returns True if it existed."""
        existed = self.records.pop(proxy, None) is not None
        self.dirty = self.dirty or existed
        return existed

    def purge(self, scope: str = "dead") -> int:
        """Drop proxies: ``dead`` (never validated or failing) or ``all``."""
        if scope == "all":
            count = len(self.records)
            self.records.clear()
            self.dirty = self.dirty or count > 0
            return count
        dead = [
            key
            for key, rec in self.records.items()
            if rec.fail_count > 0 or rec.last_ok is None
        ]
        for key in dead:
            del self.records[key]
        self.dirty = self.dirty or bool(dead)
        return len(dead)

    def prune(self, now: float | None = None) -> int:
        """Drop proxies that are dead beyond MAX_FAILURES or stale. Returns count dropped."""
        now = now if now is not None else time.time()
        drop = [
            key
            for key, rec in self.records.items()
            if rec.fail_count >= MAX_FAILURES
            # A proxy that has never once answered is dropped on its first
            # failure. Re-checking it every cycle would spend the whole
            # timeout budget on hosts that are already known dead.
            or (rec.last_ok is None and rec.fail_count >= 1)
            or (now - rec.last_checked) > STALE_AFTER
        ]
        for key in drop:
            del self.records[key]
        self.remember_dead(drop, now)
        self.dirty = self.dirty or bool(drop)
        return len(drop)

    def remember_dead(self, keys: Iterable[str], now: float | None = None) -> None:
        """Record failed proxies so the next scrape can skip them for a while."""
        if FAIL_COOLDOWN <= 0:
            return
        now = now if now is not None else time.time()
        for key in keys:
            self.dead[key] = now
        self.dirty = self.dirty or bool(keys)
        if len(self.dead) > MAX_DEAD_REMEMBERED:
            # Oldest failures are the most likely to have recovered; forget them
            # first so the list cannot grow without bound.
            for key in sorted(self.dead, key=self.dead.get)[: len(self.dead) - MAX_DEAD_REMEMBERED]:
                del self.dead[key]

    # --- query ------------------------------------------------------------
    def query(
        self,
        platform: str | None = None,
        anonymity: str | None = None,
        protocol: str | None = None,
        min_score: float | None = None,
        alive_only: bool = True,
        limit: int = 100,
    ) -> list[ProxyRecord]:
        rows = list(self.records.values())
        if alive_only:
            rows = [r for r in rows if r.fail_count == 0 and r.last_ok is not None]
        if platform:
            rows = [r for r in rows if r.platforms.get(platform, {}).get("ok")]
        if anonymity:
            rows = [r for r in rows if r.anonymity == anonymity]
        if protocol:
            rows = [r for r in rows if r.protocol == protocol]
        if min_score is not None:
            rows = [r for r in rows if r.score >= min_score]
        rows.sort(key=lambda r: (r.score, -(r.latency_ms or MAX_LATENCY_MS)), reverse=True)
        return rows[:limit]

    def stats(self) -> dict[str, Any]:
        alive = [r for r in self.records.values() if r.fail_count == 0 and r.last_ok is not None]
        by_platform = {}
        for name in ("discord", "guns.lol", "instagram"):
            by_platform[name] = sum(1 for r in alive if r.platforms.get(name, {}).get("ok"))
        by_anon = {}
        for level in ANONYMITY_SCORE:
            by_anon[level] = sum(1 for r in alive if r.anonymity == level)
        by_proto = {}
        for r in alive:
            by_proto[r.protocol] = by_proto.get(r.protocol, 0) + 1
        return {
            "total_tracked": len(self.records),
            "total_seen": self.total_seen,
            "total_checks": self.total_checks,
            "dead_remembered": len(self.dead),
            "alive": len(alive),
            "by_platform": by_platform,
            "by_anonymity": by_anon,
            "by_protocol": by_proto,
        }

    # --- persistence ------------------------------------------------------
    def save(self, force: bool = False) -> None:
        """Write the store to disk, unless there is nothing new to write.

        The whole store is serialised on every call, so the caller leans on
        ``dirty`` and a wall clock rather than calling this per check.
        """
        if not (force or self.dirty):
            return
        self.dirty = False
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {
            "counters": {"total_seen": self.total_seen, "total_checks": self.total_checks},
            "records": {k: asdict(v) for k, v in self.records.items()},
            "dead": {k: round(v, 1) for k, v in self.dead.items()},
        }
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, self.path)

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("store load failed (%s); starting empty", exc)
            return
        if not isinstance(raw, dict):
            log.warning("store file is not an object; starting empty")
            return
        # Files written before counters existed are a bare map of records.
        if "records" in raw:
            records = raw.get("records") or {}
            counters = raw.get("counters") or {}
            self.total_seen = int(counters.get("total_seen") or 0)
            self.total_checks = int(counters.get("total_checks") or 0)
            remembered = raw.get("dead") or {}
            if isinstance(remembered, dict):
                self.dead = {str(k): float(v) for k, v in remembered.items()
                             if isinstance(v, (int, float))}
        else:
            records = raw
        if not isinstance(records, dict):
            log.warning("store records are not an object; starting empty")
            return
        for key, payload in records.items():
            try:
                self.records[key] = ProxyRecord(**payload)
            except TypeError:
                continue
        # A file from before the counters existed still knows how many it holds.
        self.total_seen = max(self.total_seen, len(self.records))
        self.total_checks = max(self.total_checks, self.total_seen)
        log.info("store loaded: %d records, %d seen, %d checks",
                 len(self.records), self.total_seen, self.total_checks)

    def candidate_proxies(self, fresh: Iterable[str], now: float | None = None) -> list[str]:
        """Known-good proxies first, then new ones, then known-dead.

        A cycle re-validates the working set before it spends budget on a fresh
        scrape, so the served list refills in the first seconds of a cycle
        instead of only at the end of it. Hosts remembered as dead inside
        FAIL_COOLDOWN are dropped from the incoming scrape entirely -- a free
        list hands back the same dead addresses every time, and checking them
        again is what made a cycle slow.
        """
        now = now if now is not None else time.time()
        alive: list[str] = []
        dead: list[str] = []
        for key, rec in self.records.items():
            (alive if rec.fail_count == 0 and rec.last_ok is not None else dead).append(key)

        def cooling(key: str) -> bool:
            at = self.dead.get(key)
            return at is not None and (now - at) < FAIL_COOLDOWN

        fresh_live = [k for k in dict.fromkeys(fresh) if not cooling(k)]
        return list(dict.fromkeys([*alive, *fresh_live, *dead]))
