"""Ask the list what it can actually do, before a run spends names on it.

The list is the user's and nothing here edits it: a check is a report, and the
only thing it decides is what to show. Everything is measured over the same path
a run uses -- httpx, the same proxy schemes, the same proxy library, the same
shared TLS context -- because a check that measures something other than what the
run does is a promise the run cannot keep.

The vocabulary is one table (``STATUS_LABEL``), so the page, the log line and
this docstring cannot drift apart:

    alive       an answer came back: the exit carried a request.
    auth        the exit asked for credentials -- the line needs ``user:pass``,
                or the ones on it are wrong, expired, or not allowed from here.
    refused     the exit opened a socket and refused the tunnel (403, or a
                non-proxy listening on that port).
    no_answer   nothing answered the CONNECT. Silent, dropped, or unroutable:
                the classic dead-or-saturated exit, and the one a run used to
                discover one name at a time.
    blocked     the tunnel worked and the *target* refused this exit (403, 429,
                503). The proxy is fine; that exit is on the target's list.
    slow        the tunnel opened and the answer never arrived in time. That is
                the target being slow, not the exit -- the same distinction a run
                draws in ``sniper.proxy_fault``.
    unknown     something arrived that is not any of the above.

Nothing in here raises for a bad proxy: a proxy that cannot carry a request is
the normal case this module exists for, so every failure is a ``Probe``.
"""

import asyncio
import ipaddress
import logging
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

import httpx

from config import (
    PROXY_CHECK_CONCURRENCY,
    PROXY_CHECK_TIMEOUT,
    PROXY_CHECK_URL,
    SSL_CONTEXT,
    USER_AGENT,
)
import sniper

log = logging.getLogger("bahs.proxycheck")

STATUS_ALIVE = "alive"
STATUS_AUTH = "auth"
STATUS_REFUSED = "refused"
STATUS_NO_ANSWER = "no_answer"
STATUS_BLOCKED = "blocked"
STATUS_SLOW = "slow"
STATUS_UNKNOWN = "unknown"

# One table, read by the page, the log and the summary line. The wording is what
# a user acts on, so it says the cause rather than the exception class name.
STATUS_LABEL: dict[str, str] = {
    STATUS_ALIVE: "answered",
    STATUS_AUTH: "credentials rejected",
    STATUS_REFUSED: "exit refused the tunnel",
    STATUS_NO_ANSWER: "no answer from the exit",
    STATUS_BLOCKED: "target blocked this exit",
    STATUS_SLOW: "tunnel up, answer late",
    STATUS_UNKNOWN: "unreadable answer",
}

# Statuses that mean the exit itself cannot carry traffic. Everything else is
# either a working exit or the target talking, and the difference is the whole
# point of the report.
EXIT_FAULTS = (STATUS_AUTH, STATUS_REFUSED, STATUS_NO_ANSWER)


@dataclass(frozen=True)
class Probe:
    """What one proxy did when it was asked."""

    proxy: str
    status: str
    ms: int
    http: int | None = None
    exit_ip: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_ALIVE

    @property
    def label(self) -> str:
        return STATUS_LABEL.get(self.status, self.status)

    @property
    def exit_fault(self) -> bool:
        """Is this the proxy's own failure, rather than the target's?"""
        return self.status in EXIT_FAULTS

    def as_dict(self) -> dict:
        return {
            "proxy": self.proxy,
            "status": self.status,
            "label": self.label,
            "ok": self.ok,
            "ms": self.ms,
            "http": self.http,
            "exit_ip": self.exit_ip,
            "detail": self.detail,
        }


def _client(proxy: str, timeout: float) -> httpx.AsyncClient:
    """A one-shot client for one proxy.

    A run keeps a client per proxy because it makes thousands of requests
    through it; a check makes exactly one, so there is nothing to keep alive and
    nothing to leak: the caller closes it. One connection, because nothing else
    is ever asked of it.
    """
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        # Shared context, like every other client here: a check of a 2,000-line
        # list must not build 2,000 TLS contexts.
        verify=SSL_CONTEXT,
    )


def exit_ip(resp: httpx.Response) -> str:
    """The address the target saw, when the target is willing to say.

    The default check target answers ``{"ip": "..."}``; several common ones use
    ``origin`` or ``query``, and a few answer the bare address as text. All of
    them are read here, because the exit address is the one piece of evidence
    that a rotating endpoint actually rotates -- and the only way to notice that
    a "rotating" list is handing out one address. ``IP`` is in the list in its
    own case because that is the key Tor's own checker uses, and a check of the
    Tor fallback is run against exactly that endpoint.
    """
    text = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("ip", "IP", "origin", "query", "address"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                text = value.split(",")[0].strip()
                break
    if not text:
        text = (resp.text or "").strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _wants_credentials(exc: BaseException) -> bool:
    """Did the exit answer by asking for credentials?

    httpx raises the proxy library's ``ProxyError`` for this, and the only place
    the 407 survives is its message ("407 Proxy Authentication Required", or
    "Tunnel connection failed: 407 ..."), so the message is what is read. It
    matters to be specific: this is the one failure the user can fix in the
    paste box rather than by replacing the exit.
    """
    text = str(exc).lower()
    return "407" in text or "proxy authentication" in text


def _from_error(proxy: str, exc: BaseException, started: float) -> Probe:
    detail = sniper.describe_transport_error(exc)
    if _wants_credentials(exc):
        status = STATUS_AUTH
    elif sniper.proxy_fault(exc):
        # A connect timeout is the exit never answering; a connect error is the
        # exit answering with nothing (or refusing). Two different repairs, so
        # two different statuses.
        status = STATUS_NO_ANSWER if isinstance(exc, httpx.TimeoutException) else STATUS_REFUSED
    else:
        # The tunnel opened and the answer was late or malformed: the target's
        # doing, exactly as a run judges it.
        status = STATUS_SLOW
    return Probe(proxy, status, _elapsed_ms(started), detail=detail)


def _from_response(proxy: str, resp: httpx.Response, started: float) -> Probe:
    status_code = resp.status_code
    ms = _elapsed_ms(started)
    if status_code == 407:
        return Probe(proxy, STATUS_AUTH, ms, http=status_code,
                     detail="HTTP 407: the exit wants credentials")
    if 200 <= status_code < 300:
        return Probe(proxy, STATUS_ALIVE, ms, http=status_code, exit_ip=exit_ip(resp),
                     detail=f"HTTP {status_code}")
    # A refusal here came from the far side, not from the tunnel: the request
    # went out through the exit and something answered. 403/429/503 is what a
    # Cloudflare-fronted target does to a range it does not like.
    return Probe(proxy, STATUS_BLOCKED, ms, http=status_code,
                 detail=f"HTTP {status_code} from the target")


async def probe(
    proxy: str,
    url: str = PROXY_CHECK_URL,
    timeout: float = PROXY_CHECK_TIMEOUT,
    client_factory=None,
) -> Probe:
    """Ask one proxy for ``url`` and say what happened.

    ``client_factory`` exists so this can be tested without a network: the tests
    hand in a factory that returns a client backed by ``httpx.MockTransport``,
    which is the only honest way to assert on the classification of an exception
    nobody can produce on demand.
    """
    factory = client_factory or _client
    started = time.perf_counter()
    client = factory(proxy, timeout)
    try:
        async with client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:  # every failure here is an answer, not a crash
        return _from_error(proxy, exc, started)
    return _from_response(proxy, resp, started)


async def check_many(
    proxies: Iterable[str],
    concurrency: int = PROXY_CHECK_CONCURRENCY,
    url: str = PROXY_CHECK_URL,
    timeout: float = PROXY_CHECK_TIMEOUT,
    stop: "asyncio.Event | None" = None,
    client_factory=None,
) -> AsyncIterator[Probe]:
    """Probe every proxy, yielding each ``Probe`` as it settles.

    Results come back in completion order, not list order, which is what makes a
    live page useful: the first rows are the answers, not a wait. ``stop`` is
    checked between results so a client that walks away leaves nothing running --
    the tasks still in flight are cancelled on the way out.
    """
    items = [p for p in dict.fromkeys(proxies) if p]
    if not items:
        return
    width = max(1, min(concurrency, len(items)))
    results: asyncio.Queue[Probe] = asyncio.Queue()
    gate = asyncio.Semaphore(width)

    async def one(proxy: str) -> None:
        async with gate:
            try:
                result = await probe(proxy, url=url, timeout=timeout, client_factory=client_factory)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A probe that blows up is not a verdict about the proxy, but it
                # must not stop the sweep either: the reader is waiting on a line
                # for this proxy, and a worker that dies without publishing one
                # hangs the whole report. Named, counted, and carried on.
                log.warning("proxy check of %s raised %s",
                            sniper.mask_proxy(proxy), type(exc).__name__, exc_info=True)
                result = Probe(proxy, STATUS_UNKNOWN, 0, detail=f"{type(exc).__name__}: {exc}")
            await results.put(result)

    tasks = [asyncio.create_task(one(p)) for p in items]
    try:
        for _ in range(len(tasks)):
            result = await results.get()
            yield result
            if stop is not None and stop.is_set():
                break
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def target_reachable(
    url: str = PROXY_CHECK_URL, timeout: float = PROXY_CHECK_TIMEOUT
) -> tuple[bool, str]:
    """Can this host reach the check target at all, with no proxy in the way?

    A check run against an unreachable target reports every proxy dead. That is
    the worst answer available -- wrong, and completely plausible, so the user
    replaces a working list. One direct request separates "your list is dead"
    from "this service cannot see the target", and the second one names itself
    instead of being blamed on the list.
    """
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
            follow_redirects=True,
            verify=SSL_CONTEXT,
        ) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        return False, sniper.describe_transport_error(exc)
    if 200 <= resp.status_code < 300:
        return True, f"HTTP {resp.status_code} in {_elapsed_ms(started)} ms"
    return False, f"HTTP {resp.status_code} from the check target"


def summarise(probes: Iterable[Probe], elapsed_s: float = 0.0) -> dict:
    """The counts behind a check, in the order a user reads them.

    ``dead`` is what the page offers to remove: the exits that could not carry a
    request themselves. A target that blocked every exit is counted apart from
    that, because deleting a working list for the target's opinion is exactly the
    mistake this report exists to prevent.
    """
    counts: dict[str, int] = {}
    dead = 0
    for result in probes:
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.exit_fault:
            dead += 1
    return {
        "total": sum(counts.values()),
        "alive": counts.get(STATUS_ALIVE, 0),
        "dead": dead,
        "counts": counts,
        "labels": {status: STATUS_LABEL.get(status, status) for status in counts},
        "elapsed_s": round(elapsed_s, 1),
    }


def describe(probes: Iterable[Probe]) -> str:
    """One sentence for the log: what the list is worth."""
    summary = summarise(probes)
    if not summary["total"]:
        return "no proxies to check"
    parts = ", ".join(
        f"{count} {STATUS_LABEL.get(status, status)}" for status, count in sorted(summary["counts"].items())
    )
    return f"{summary['alive']} of {summary['total']} proxies answered ({parts})"


def _log_line(result: Probe) -> str:
    """A failure line that names the exit and what it said.

    The credentials are masked: this is the log, and a log is where a proxy
    password survives for years. ``sniper.mask_proxy`` keeps the endpoint, which
    is the half a diagnostic needs.
    """
    return f"{sniper.mask_proxy(result.proxy)}: {result.status} ({result.detail}) in {result.ms} ms"


def log_result(result: Probe) -> None:
    """Every failure at INFO, an answer at DEBUG.

    The rule the run logger already follows: the interesting line is the one
    that failed, and a check of a healthy 2,000-line list must not bury it under
    2,000 lines saying it worked.
    """
    if result.ok:
        log.debug("proxy check %s", _log_line(result))
        return
    log.info("proxy check %s", _log_line(result))
