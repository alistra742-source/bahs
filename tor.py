"""Tor: the pool that exists when nobody has pasted one.

An empty proxy list used to be the end of the road -- every endpoint answered
``no proxies saved yet`` and the tool did nothing until the user supplied a list.
Tor is the one pool that is already there, so an empty list now means the run
goes out over the Tor exits this host can reach, and the run says so in its own
first line. A stored list always wins; Tor is what happens when there is none.

Three rules this module holds to, because the alternative is quietly lying:

* **A port is not a Tor.** Something listening on 127.0.0.1:9050 is a SOCKS5
  proxy and nothing more -- it could be an ssh tunnel, a badly configured
  consumer VPN, or a plain ``socks5`` server. An exit is called ``tor`` only
  after Tor's own check answers ``IsTor: true`` for a request made *through*
  that port. A listener that will not confirm is still usable, and is reported
  as unconfirmed, never as Tor.
* **Loopback only.** Discovery looks at ``TOR_HOST`` (loopback by default), and
  the instance this service starts binds ``127.0.0.1`` on every port, opens no
  control port, and sets ``ClientOnly``. There is no configuration here that
  turns this process into a proxy for somebody else.
* **It is a fallback, not a replacement.** A Tor exit is shared by thousands of
  users, so platforms challenge and refuse those addresses far more often than a
  good private list. That is stated in the status, in the run note and in the
  docs -- a Tor run that answers nothing is behaving normally, not failing
  mysteriously.

The cost of that honesty is one HTTP request per exit per ``TOR_VERIFY_TTL``,
cached, and never on the per-check path.
"""

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field

import httpx

from config import (
    SSL_CONTEXT,
    TOR_AUTO_START,
    TOR_BINARY,
    TOR_BIND_WAIT,
    TOR_CHECK_URL,
    TOR_CONCURRENCY,
    TOR_DATA_DIR,
    TOR_ENABLED,
    TOR_HOST,
    TOR_MANAGED_PORTS,
    TOR_PER_PROXY,
    TOR_PORTS,
    TOR_PROBE_TIMEOUT,
    TOR_RETRY_AFTER,
    TOR_START_TIMEOUT,
    TOR_TIMEOUT,
    TOR_VERIFY_CONCURRENCY,
    TOR_VERIFY_TIMEOUT,
    TOR_VERIFY_TTL,
    USER_AGENT,
)

log = logging.getLogger("bahs.tor")

# What one exit is, as far as this process can honestly say.
STATUS_TOR = "tor"          # Tor's own check said IsTor: true for this port
STATUS_NOT_TOR = "not_tor"  # it answered, and said IsTor: false
STATUS_UNKNOWN = "unknown"  # SOCKS5 answered; the confirmation did not arrive

# The statuses a run may be sent over. All three can carry a request -- only the
# wording of the report changes, because only one of them is a proven Tor exit.
USABLE = (STATUS_TOR, STATUS_NOT_TOR, STATUS_UNKNOWN)

# The whole-fallback states, which is what the page and the run note read.
STATE_OFF = "off"
STATE_NONE = "none"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_UNVERIFIED = "unverified"

STATE_LABEL = {
    STATE_OFF: "off",
    STATE_NONE: "not available",
    STATE_STARTING: "starting",
    STATE_READY: "ready",
    STATE_UNVERIFIED: "unconfirmed",
}


def _hostport(host: str, port: int) -> str:
    """``host:port``, re-bracketed when the host is an IPv6 literal."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def url(port: int, host: str | None = None) -> str:
    """The proxy URL for one Tor SOCKS port.

    ``socks5h`` and not ``socks5`` because the name has to be resolved at the
    far end. With ``socks5`` a client may resolve locally and then hand the exit
    an address, which leaks the name being asked about to this host's resolver
    and defeats the point of asking through Tor at all. (httpx resolves at the
    proxy for both schemes today -- see ``httpcore``'s SOCKS5 connection, which
    sends the hostname as a domain -- and the ``h`` is what keeps that true if
    the client stops doing it.)
    """
    return f"socks5h://{_hostport(host or TOR_HOST, port)}"


# --- what one exit is ------------------------------------------------------
@dataclass(frozen=True)
class Exit:
    """One SOCKS5 listener, and what is known about it."""

    port: int
    url: str
    status: str = STATUS_UNKNOWN
    exit_ip: str = ""
    detail: str = ""
    managed: bool = False
    verified_at: float = 0.0

    @property
    def is_tor(self) -> bool:
        return self.status == STATUS_TOR

    @property
    def usable(self) -> bool:
        return self.status in USABLE

    @property
    def label(self) -> str:
        if self.status == STATUS_TOR:
            return "Tor exit"
        if self.status == STATUS_NOT_TOR:
            return "SOCKS5, not Tor"
        return "SOCKS5, unconfirmed"

    def as_dict(self) -> dict:
        return {
            "port": self.port,
            "host": TOR_HOST,
            "url": self.url,
            "status": self.status,
            "label": self.label,
            "is_tor": self.is_tor,
            "exit_ip": self.exit_ip,
            "detail": self.detail,
            "managed": self.managed,
            "verified_at": self.verified_at or None,
        }


def describe_exit(exit_: Exit) -> str:
    """``127.0.0.1:9050 -> 185.x.x.x (Tor)``, for a log line."""
    where = f" -> {exit_.exit_ip}" if exit_.exit_ip else ""
    return f"{_hostport(TOR_HOST, exit_.port)}{where} ({exit_.label})"


# --- discovery -------------------------------------------------------------
def socks5_greeting(
    host: str | None = None, port: int = 0, timeout: float = TOR_PROBE_TIMEOUT
) -> tuple[bool, str]:
    """Is there a usable SOCKS5 listener on this port?

    A real SOCKS5 greeting and not just a TCP connect: an HTTP proxy, a
    webserver or an ssh port forward all accept a connection, and treating any
    open port as an exit is how a page ends up claiming Tor it never used. The
    method byte is read too -- a listener that insists on authentication is not
    usable by a client that has no credentials for it, and saying so is better
    than a run of ``ProxyError`` rows.

    Blocking by design (one connection, a few milliseconds on loopback) and
    called from a thread by the async paths.
    """
    where = host or TOR_HOST
    try:
        with socket.create_connection((where, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"\x05\x01\x00")  # SOCKS5, one method offered: no auth
            reply = sock.recv(2)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if len(reply) < 2 or reply[0] != 0x05:
        return False, "something is listening but it is not SOCKS5"
    if reply[1] == 0x00:
        return True, "SOCKS5"
    if reply[1] == 0xFF:
        return False, "SOCKS5, and it accepts none of the methods offered"
    if reply[1] == 0x02:
        return False, "SOCKS5, and it wants a username and password"
    return False, f"SOCKS5 offering only method 0x{reply[1]:02x}"


def discovery_ports() -> tuple[int, ...]:
    """Every port worth looking at: the ones a tor may already be on, plus ours.

    ``TOR_PORTS`` alone was a real bug, caught by running this against a tor that
    had actually been started: the instance this service starts listens on
    ``TOR_MANAGED_PORTS`` (9050, 9052, 9054, 9056), and a discovery list of
    "9050, 9150" found exactly one of the four ports it had just started. One
    exit instead of four, and nothing in the log to say why.
    """
    ports: list[int] = list(TOR_PORTS)
    for port in TOR_MANAGED_PORTS:
        if port not in ports:
            ports.append(port)
    return tuple(ports)


def discover(ports: tuple[int, ...] | None = None, host: str | None = None) -> list[int]:
    """Every configured port that answers a SOCKS5 greeting, in order."""
    found: list[int] = []
    for port in ports if ports is not None else discovery_ports():
        ok, note = socks5_greeting(host, port)
        if ok:
            log.debug("SOCKS5 listener on %s (%s)", _hostport(host or TOR_HOST, port), note)
            found.append(port)
        else:
            log.debug("no SOCKS5 listener on %s: %s", _hostport(host or TOR_HOST, port), note)
    return found


# --- verification ----------------------------------------------------------
def _verify_client(proxy: str, timeout: float) -> httpx.AsyncClient:
    """A one-shot client for one exit, in the shape ``proxycheck`` uses."""
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        verify=SSL_CONTEXT,
    )


def is_tor_answer(payload: object) -> bool | None:
    """Read Tor's own answer.

    The documented shape is ``{"IsTor": true, "IP": "1.2.3.4"}``; the lowercase
    spelling is accepted because this endpoint is a convenience the Tor Project
    has never promised to freeze, and a rename would otherwise turn every exit
    into "unconfirmed". Anything else returns None -- which is *not* a no: no
    answer is not evidence that a listener is not Tor, and it must never be
    reported as one.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("IsTor", "is_tor", "isTor"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
    return None


def exit_ip_of(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("IP", "ip"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def verify(
    exit_: Exit, timeout: float = TOR_VERIFY_TIMEOUT, client_factory=None
) -> Exit:
    """Ask, through this exit, whether it is a Tor exit.

    ``client_factory`` is injectable for the same reason ``proxycheck`` has one:
    the answers worth asserting on (IsTor false, a mangled body, a timeout) are
    not reproducible against a live service on demand.
    """
    factory = client_factory or _verify_client
    started = time.perf_counter()
    try:
        async with factory(exit_.url, timeout) as client:
            resp = await client.get(TOR_CHECK_URL, headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        return Exit(
            exit_.port, exit_.url, STATUS_UNKNOWN, managed=exit_.managed,
            detail=f"could not confirm as Tor ({type(exc).__name__})",
            verified_at=time.time(),
        )
    ms = max(0, int((time.perf_counter() - started) * 1000))
    if not 200 <= resp.status_code < 300:
        return Exit(
            exit_.port, exit_.url, STATUS_UNKNOWN, managed=exit_.managed,
            detail=f"could not confirm as Tor (HTTP {resp.status_code} from {TOR_CHECK_URL})",
            verified_at=time.time(),
        )
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    answer = is_tor_answer(payload)
    if answer is None:
        return Exit(
            exit_.port, exit_.url, STATUS_UNKNOWN, managed=exit_.managed,
            detail="the check answered something that is not a Tor verdict",
            verified_at=time.time(),
        )
    status = STATUS_TOR if answer else STATUS_NOT_TOR
    detail = (
        f"Tor confirmed by {TOR_CHECK_URL} in {ms} ms"
        if answer
        else "it answered, and not as a Tor exit"
    )
    return Exit(
        exit_.port, exit_.url, status, exit_ip=exit_ip_of(payload),
        detail=detail, managed=exit_.managed, verified_at=time.time(),
    )


async def verify_many(
    exits: list[Exit], concurrency: int = TOR_VERIFY_CONCURRENCY, timeout: float = TOR_VERIFY_TIMEOUT,
    client_factory=None,
) -> list[Exit]:
    """Verify every exit, in order, at most ``concurrency`` at a time."""
    if not exits:
        return []
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(exit_: Exit) -> Exit:
        async with gate:
            return await verify(exit_, timeout=timeout, client_factory=client_factory)

    return list(await asyncio.gather(*(one(e) for e in exits)))


# --- the instance this service starts --------------------------------------
def torrc_text(ports: tuple[int, ...] | None = None, data_dir: str | None = None) -> str:
    """The torrc used when this service starts its own tor.

    Every decision in here is a limit on what that process can be used for:
    ``ClientOnly`` (it will never relay for anyone), ``ControlPort 0`` (no
    control socket, so no cookie or password exists to leak and nothing else can
    signal it), and one loopback ``SocksPort`` per exit -- loopback is stated
    literally rather than left to a default, so this file cannot be read as
    \"a proxy on all interfaces\".
    """
    directory = os.path.abspath(data_dir or TOR_DATA_DIR)
    lines = [
        "# Written by VORTEX. Do not edit: it is rewritten whenever tor starts.",
        "# A client, not a relay, and not a public proxy -- every listener is on",
        "# loopback, no control port is opened, and relaying is off.",
        f"DataDirectory {directory}",
        "ClientOnly 1",
        "ControlPort 0",
        "Log notice stdout",
    ]
    for port in ports or TOR_MANAGED_PORTS:
        lines.append(f"SocksPort 127.0.0.1:{port}")
    return "\n".join(lines) + "\n"


def torrc_path() -> str:
    return os.path.join(os.path.abspath(TOR_DATA_DIR), "torrc")


def log_path() -> str:
    return os.path.join(os.path.abspath(TOR_DATA_DIR), "tor.log")


def binary_path() -> str | None:
    """The tor executable, or None. Checked on PATH, never assumed."""
    return shutil.which(TOR_BINARY)


def _tail(path: str, lines: int = 6, limit: int = 65536) -> list[str]:
    """The last few log lines, read from the end.

    Bounded on purpose: tor's log grows for as long as the process lives, and a
    status endpoint that reads the whole thing is a memory bug with a timer on
    it.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    kept = [line.strip() for line in text.splitlines() if line.strip()]
    return kept[-lines:]


def bootstrapped(path: str | None = None) -> bool:
    """Has tor said it is ready?

    The log line, not an open port: tor opens its SOCKS listeners before it has
    a circuit to attach a stream to, so a connect that succeeds during bootstrap
    is not an exit yet -- it is a request that will fail a second later. Tor's
    own ``Bootstrapped 100% (done)`` is the only statement that it is up.
    """
    return any("Bootstrapped 100%" in line for line in _tail(path or log_path(), lines=200))


def start(timeout: float = TOR_START_TIMEOUT) -> tuple[bool, str]:
    """Start the managed tor and wait for it to bootstrap. Blocking.

    Called from a thread. Returns ``(ready, note)`` and never raises: no tor
    binary, a port already in use, a torrc tor rejects and a slow bootstrap are
    all normal outcomes that the caller reports rather than crashes on.
    """
    binary = binary_path()
    if binary is None:
        return False, (
            f"tor is not installed here (looked for {TOR_BINARY!r} on PATH). "
            "Install tor, or paste a proxy list."
        )
    directory = os.path.abspath(TOR_DATA_DIR)
    try:
        os.makedirs(directory, exist_ok=True)
        path = torrc_path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(torrc_text())
    except OSError as exc:
        return False, f"could not write the tor config under {directory} ({type(exc).__name__})"

    _state.starting = True
    _state.started_at = time.time()
    try:
        # Truncated, not appended. Readiness is read out of this file
        # ("Bootstrapped 100%"), and an appended log made the *previous* run's
        # line the new run's answer: a fresh start reported "bootstrapped in
        # 0.0s" and then found no listener, because it was reading history. A
        # tor's log is only ever a diagnostic for the process that wrote it.
        handle = open(log_path(), "w", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as tor
    except OSError as exc:
        _state.starting = False
        return False, f"could not open {log_path()} ({type(exc).__name__})"
    try:
        proc = subprocess.Popen(  # noqa: S603 - an absolute path found on PATH
            [binary, "-f", path],
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        handle.close()
        _state.starting = False
        return False, f"could not start tor ({type(exc).__name__}: {exc})"
    _state.proc = proc
    _state.log_path = log_path()
    _state.torrc_path = path
    log.info("started tor (pid %d) on %s", proc.pid, ", ".join(str(p) for p in TOR_MANAGED_PORTS))

    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        code = proc.poll()
        if code is not None:
            _state.starting = False
            _state.proc = None
            tail = _tail(_state.log_path, lines=3)
            note = tail[-1] if tail else "no output"
            return False, f"tor exited with code {code}: {note}"
        if bootstrapped(_state.log_path):
            _state.starting = False
            log.info("tor bootstrapped in %.1fs", time.time() - _state.started_at)
            return True, f"tor bootstrapped in {time.time() - _state.started_at:.1f}s"
        time.sleep(0.5)
    # Still running and still bootstrapping: leave it up. A first start with a
    # cold data directory can take longer than any request should wait, and
    # killing it here would throw away the work.
    _state.starting = False
    tail = _tail(_state.log_path, lines=1)
    return False, (
        f"tor is still bootstrapping after {timeout:.0f}s"
        + (f": {tail[0]}" if tail else "")
    )


def wait_for_listener(deadline: float | None = None, host: str | None = None) -> list[int]:
    """Look for the listeners a start just created, for a bounded moment.

    Tor opens its SocksPorts and announces ``Bootstrapped 100%`` in that order,
    so this normally returns on the first call. "Normally" is not a guarantee,
    and the failure mode without it was a fallback that said *no SOCKS5 listener
    answered* immediately after starting four of them. It is a handful of
    loopback connects, and it gives up.
    """
    # Read at call time rather than captured as a default: `TOR_BIND_WAIT` is a
    # setting, and a default argument would freeze whatever the module held when
    # it was imported.
    end = time.time() + max(0.0, TOR_BIND_WAIT if deadline is None else deadline)
    while True:
        ports = discover(host=host)
        if ports or time.time() >= end:
            return ports
        time.sleep(0.25)


# --- the cached picture ----------------------------------------------------
@dataclass
class _State:
    exits: list[Exit] = field(default_factory=list)
    verified_at: float = 0.0
    managed: bool = False
    proc: subprocess.Popen | None = None
    log_path: str = ""
    torrc_path: str = ""
    error: str = ""
    starting: bool = False
    started_at: float = 0.0


_state = _State()
_lock = asyncio.Lock()
# Bumped whenever the set of exits changes, so a pool built over Tor can tell
# that the ground moved under it and rebuild.
version = 0


def state_name() -> str:
    if not TOR_ENABLED:
        return STATE_OFF
    if _state.starting:
        return STATE_STARTING
    usable = [e for e in _state.exits if e.usable]
    if not usable:
        return STATE_NONE
    return STATE_READY if any(e.is_tor for e in usable) else STATE_UNVERIFIED


def usable_urls(exits: list[Exit] | None = None) -> list[str]:
    return [e.url for e in (exits if exits is not None else _state.exits) if e.usable]


def reason() -> str:
    """One sentence saying why the fallback is where it is. Never a guess."""
    if not TOR_ENABLED:
        return "Tor is disabled (TOR_ENABLED=0)"
    if _state.starting:
        return "tor is bootstrapping"
    ports = ", ".join(str(p) for p in discovery_ports())
    if not _state.exits:
        detail = f": {_state.error}" if _state.error else ""
        return f"no SOCKS5 listener answered on {TOR_HOST}, ports {ports}{detail}"
    tor_exits = sum(1 for e in _state.exits if e.is_tor)
    if tor_exits:
        return (
            f"{tor_exits} of {len(_state.exits)} exits confirmed as Tor by "
            f"{TOR_CHECK_URL}"
        )
    if any(e.status == STATUS_NOT_TOR for e in _state.exits):
        return (
            "a SOCKS5 listener answered, but it did not confirm as a Tor exit "
            "(it says it is not one) -- usable, and reported as not Tor"
        )
    return (
        f"{len(_state.exits)} SOCKS5 listener(s) answered; none could be "
        f"confirmed as Tor ({_state.exits[0].detail})"
    )


def note() -> str:
    """What a user should know before trusting a Tor run with names."""
    return (
        "Tor exits are shared by thousands of users, so platforms challenge and refuse "
        "them more often than a private list: expect more blocked checks and fewer "
        "verdicts. It is here so an empty list is not a dead end -- a pasted list is "
        "faster and answers more."
    )


def snapshot() -> dict:
    """The current picture, with no I/O at all.

    Read by ``/health``, ``/info`` and the pools; the blocked-on-network facts
    (is tor installed, is it bootstrapping) come from state this module already
    holds, so a health check never waits on a bootstrap.
    """
    usable = [e for e in _state.exits if e.usable]
    return {
        "enabled": TOR_ENABLED,
        "state": state_name(),
        "state_label": STATE_LABEL.get(state_name(), state_name()),
        "reason": reason(),
        "note": note(),
        "host": TOR_HOST,
        "ports": list(TOR_PORTS),
        "looked_at": list(discovery_ports()),
        "managed_ports": list(TOR_MANAGED_PORTS),
        "auto_start": TOR_AUTO_START,
        "installed": binary_path() is not None,
        "binary": TOR_BINARY,
        "managed": _state.managed,
        "bootstrapping": _state.starting,
        "error": _state.error,
        "check_url": TOR_CHECK_URL,
        "exits": [e.as_dict() for e in _state.exits],
        "usable": len(usable),
        "urls": [e.url for e in usable],
        "confirmed": sum(1 for e in _state.exits if e.is_tor),
        "concurrency": TOR_CONCURRENCY,
        "per_proxy": TOR_PER_PROXY,
        "timeout": TOR_TIMEOUT,
        "verified_at": _state.verified_at or None,
        "log_path": _state.log_path or None,
        "log_tail": _tail(_state.log_path) if _state.log_path else [],
    }


async def exits(
    force: bool = False, start_if_missing: bool = True, respect_auto_start: bool = True
) -> list[Exit]:
    """The usable exits, discovered, started and verified as needed.

    One lock: two requests arriving together with an empty list must not both
    start a tor, and the second one wants the answer the first is already
    waiting for. The result is cached for ``TOR_VERIFY_TTL`` -- an empty result
    included, so a page reload does not re-run a 75-second bootstrap -- and
    ``force`` skips that cache for the refresh button.
    """
    if not TOR_ENABLED:
        return []
    async with _lock:
        now = time.time()
        if not force and _state.verified_at and now - _state.verified_at < TOR_VERIFY_TTL:
            return list(_state.exits)
        ports = await asyncio.to_thread(discover)
        may_start = start_if_missing and (TOR_AUTO_START or not respect_auto_start)
        if ports:
            _state.error = ""
        elif may_start:
            ready, detail = await asyncio.to_thread(start)
            _state.error = "" if ready else detail
            if ready:
                ports = await asyncio.to_thread(wait_for_listener)
                if not ports:
                    _state.error = (
                        "tor reported it was up, but no SocksPort was accepting a "
                        "connection afterwards"
                    )
            elif _state.proc is None:
                log.warning("tor fallback unavailable: %s", detail)
        elif not start_if_missing:
            pass  # a status read that was told not to start anything
        else:
            _state.error = "no listener found and the automatic start is off (TOR_AUTO_START=0)"
        if not ports:
            # Cached, but only briefly: an instance that is still bootstrapping
            # must be picked up in seconds. Reporting "no fallback" for the full
            # TOR_VERIFY_TTL because a tor was 20 seconds slow would be its own
            # kind of lie.
            _replace([], managed=_state.proc is not None, ttl=TOR_RETRY_AFTER)
            return []
        candidates = [
            Exit(port, url(port), managed=_state.proc is not None) for port in ports
        ]
        verified = await verify_many(candidates)
        _replace(verified, managed=_state.proc is not None)
        _state.error = ""
        log.info("tor fallback: %s", describe())
        return list(_state.exits)


def _replace(verified: list[Exit], managed: bool, ttl: float | None = None) -> None:
    """Publish a picture. ``ttl`` backdates it so it ages out sooner.

    ``verified_at`` is the clock the cache is read against, so asking for a
    shorter life means writing the timestamp earlier rather than adding a second
    field every reader would have to know about.
    """
    global version
    _state.exits = verified
    _state.managed = managed
    age = 0.0 if ttl is None else max(0.0, TOR_VERIFY_TTL - max(0.0, ttl))
    _state.verified_at = time.time() - age
    version += 1


def describe() -> str:
    """One log sentence: what a run over Tor would actually be going out on."""
    name = state_name()
    if name == STATE_OFF:
        return "off (TOR_ENABLED=0)"
    if name == STATE_NONE:
        return reason()
    usable = [e for e in _state.exits if e.usable]
    return (
        f"{len(usable)} exit(s) [{', '.join(describe_exit(e) for e in usable)}]"
        + (" (started by this service)" if _state.managed else "")
    )


def pool_kwargs() -> dict:
    """How a pool over Tor must be built, so the server does not guess.

    Three things differ from a pasted list, and each is a mistake waiting to
    happen: the timeouts (a Tor request costs three relays, so a 3-second
    connect ceiling reports healthy exits as dead), the per-exit connection
    count (Tor's own circuits are the bottleneck long before the sockets are),
    and retirement -- see ``sniper.ProxyPool``'s ``retire`` parameter.
    """
    return {
        "per_proxy": max(1, TOR_PER_PROXY),
        "timeout": httpx.Timeout(
            connect=TOR_TIMEOUT, read=TOR_TIMEOUT, write=TOR_TIMEOUT, pool=TOR_TIMEOUT
        ),
        "retire": False,
    }


async def start_now() -> list[Exit]:
    """Start our own tor on an explicit request, whatever ``TOR_AUTO_START`` says.

    ``TOR_AUTO_START`` decides whether a tor starts *by itself* when a run
    finds no list and no listener. Somebody pressing "start Tor" on the page is
    a person asking, so that flag is not consulted here -- the only thing that
    stops this is ``TOR_ENABLED=0``, which is the service saying it wants no
    part of Tor at all.
    """
    if not TOR_ENABLED:
        return []
    return await exits(force=True, start_if_missing=True, respect_auto_start=False)


async def warmup() -> None:
    """Bring the fallback up at boot, so the first run with no list is not a wait.

    Never raises: a host with no tor installed is the common case, and the boot
    log is where it should read as a sentence rather than as a failed startup.
    """
    if not TOR_ENABLED:
        log.info("tor fallback: off (TOR_ENABLED=0)")
        return
    if not TOR_AUTO_START and not await asyncio.to_thread(discover):
        log.info(
            "tor fallback: no listener on %s, auto-start off",
            ", ".join(str(p) for p in discovery_ports()),
        )
        return
    try:
        await exits()
    except Exception as exc:  # pragma: no cover - the fallback must never break boot
        log.warning("tor warmup failed: %s: %s", type(exc).__name__, exc)
        return
    log.info("tor fallback: %s", describe())


async def aclose() -> None:
    """Stop the tor this service started. A listener found in the environment is
    left alone -- it is not ours to stop."""
    proc = _state.proc
    _state.proc = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        await asyncio.to_thread(proc.wait, 5)
    except Exception:  # pragma: no cover - best effort on the way out
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:  # pragma: no cover
            pass
    log.info("stopped the tor this service started")


def reset() -> None:
    """Forget everything (tests, and a manual refresh after a hard start)."""
    global version
    _state.exits = []
    _state.verified_at = 0.0
    _state.error = ""
    _state.starting = False
    version += 1
