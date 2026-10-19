"""Tor, the pool that exists when nobody has pasted one.

Four questions, and they are different questions:

* **Is a port actually a Tor exit?** A SOCKS5 greeting against a real loopback
  socket, and then Tor's own answer (``IsTor``) read through that exit with
  ``httpx.MockTransport`` -- because the answer worth asserting on is the one
  that says *no*, and there is no honest way to produce that from a real Tor.
* **Where does the fallback plug in?** An empty list falls back, a stored list
  never does, and neither of those is allowed to leave the process holding one
  of them. ``server._pool`` is called directly for those, with Tor's own
  network calls replaced.
* **What happens to a pool that cannot be replenished?** A Tor pool must not
  retire its way to empty -- the same failure sequence, run against a pool that
  can be replenished and one that cannot, because the difference is the point.
* **What does the HTTP surface say?** The endpoints go through the real session,
  CSRF and middleware, because that is the interface a user has.

Nothing here touches the network, starts a tor, or writes to a real store.
"""

import asyncio
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import accounts
import proxycheck
import server
import sniper
import tor

ORIGIN = "http://testserver"
PORT = 9500


# --- fakes ----------------------------------------------------------------
def factory_for(handler):
    """A ``client_factory`` that never touches the network, and records the proxy."""
    seen: list[str] = []

    def make(proxy: str, timeout: float) -> httpx.AsyncClient:
        seen.append(proxy)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    return make, seen


def answering(payload: str = '{"IsTor": true, "IP": "185.220.101.7"}', status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=payload)

    return handler


def raising(exc: BaseException):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


async def alive(proxy: str, **kwargs) -> proxycheck.Probe:
    """A probe that always answers -- the shape ``check_many`` awaits."""
    return proxycheck.Probe(proxy, proxycheck.STATUS_ALIVE, 12, http=200, exit_ip="185.220.101.7")


class socks5_server:
    """A real loopback socket that answers a SOCKS5 greeting, in a thread.

    A mocked socket would prove nothing about discovery: the whole question is
    whether a *real* connection to a *real* listener is read as SOCKS5, an HTTP
    proxy, or nothing at all.
    """

    def __init__(self, reply: bytes = b"\x05\x00"):
        self.reply = reply
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(3)
                    conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def closed_port() -> int:
    """A port nothing is listening on (bound, then released)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --- configuration --------------------------------------------------------
class PortListTests(unittest.TestCase):
    """One environment variable, several ports, and a typo that must not crash."""

    def ports(self, raw: str) -> tuple[int, ...]:
        with patch.dict(os.environ, {"TEST_TOR_PORTS": raw}):
            from config import _ports

            return _ports("TEST_TOR_PORTS", "9050")

    def test_ports_survive_commas_spaces_semicolons_and_junk(self):
        self.assertEqual(self.ports("9050, 9150;9160 nope 70000 -1 0"), (9050, 9150, 9160))

    def test_duplicates_collapse(self):
        self.assertEqual(self.ports("9050,9050,9052"), (9050, 9052))

    def test_a_variable_that_says_nothing_usable_falls_back_to_the_default(self):
        # "look for Tor nowhere" is the one reading nobody asks for, so an
        # unreadable value must not produce it.
        self.assertEqual(self.ports("nonsense"), (9050,))

    def test_the_url_is_socks5h_so_names_resolve_at_the_exit(self):
        self.assertEqual(tor.url(9050), "socks5h://127.0.0.1:9050")
        self.assertEqual(tor.url(9050, "::1"), "socks5h://[::1]:9050")


class TorrcTests(unittest.TestCase):
    """The instance this service starts is a client on loopback, and nothing else."""

    def test_every_listener_is_bound_to_loopback(self):
        text = tor.torrc_text(ports=(9050, 9052), data_dir="/tmp/vortex-tor")
        self.assertIn("SocksPort 127.0.0.1:9050", text)
        self.assertIn("SocksPort 127.0.0.1:9052", text)
        self.assertNotIn("0.0.0.0", text)
        self.assertEqual(text.count("SocksPort"), 2)

    def test_it_is_a_client_with_no_control_port(self):
        text = tor.torrc_text(ports=(9050,), data_dir="/tmp/vortex-tor")
        self.assertIn("ClientOnly 1", text)
        self.assertIn("ControlPort 0", text)
        self.assertNotIn("ORPort", text)
        self.assertNotIn("ExitRelay", text)
        self.assertIn("DataDirectory /tmp/vortex-tor", text)

    def test_the_data_directory_is_absolute(self):
        text = tor.torrc_text(ports=(9050,), data_dir="data/tor")
        line = next(l for l in text.splitlines() if l.startswith("DataDirectory"))
        self.assertTrue(os.path.isabs(line.split(" ", 1)[1]))


# --- discovery ------------------------------------------------------------
class DiscoveryTests(unittest.TestCase):
    """A port is a SOCKS5 listener, or it is something else, or it is nothing."""

    def test_a_socks5_listener_is_found(self):
        with socks5_server(b"\x05\x00") as server_:
            ok, note = tor.socks5_greeting("127.0.0.1", server_.port)
            self.assertTrue(ok, note)
            self.assertIn("SOCKS5", note)
            self.assertEqual(tor.discover(ports=(server_.port,)), [server_.port])

    def test_an_http_proxy_on_that_port_is_not_an_exit(self):
        with socks5_server(b"HTTP/1.1 200 OK\r\n\r\n") as server_:
            ok, note = tor.socks5_greeting("127.0.0.1", server_.port)
            self.assertFalse(ok)
            self.assertIn("not SOCKS5", note)
            self.assertEqual(tor.discover(ports=(server_.port,)), [])

    def test_nothing_listening_is_reported_as_nothing(self):
        ok, note = tor.socks5_greeting("127.0.0.1", closed_port(), timeout=0.5)
        self.assertFalse(ok)
        self.assertTrue(note, "a failure has to say what it was")
        self.assertEqual(tor.discover(ports=(closed_port(),)), [])

    def test_a_listener_that_wants_a_password_is_not_used(self):
        # This client has no credentials for it, and a run of ProxyErrors is a
        # worse answer than an honest no.
        with socks5_server(b"\x05\x02") as server_:
            ok, note = tor.socks5_greeting("127.0.0.1", server_.port)
            self.assertFalse(ok)
            self.assertIn("username and password", note)

    def test_the_ports_this_service_starts_are_looked_at_too(self):
        # The bug this covers was found by running against a real tor: the
        # instance this service starts listens on 9050/9052/9054/9056, and
        # discovery looked only at TOR_PORTS (9050, 9150) -- so it found one of
        # the four ports it had just started, and said nothing.
        with socks5_server() as mine, \
             patch.object(tor, "TOR_PORTS", (closed_port(),)), \
             patch.object(tor, "TOR_MANAGED_PORTS", (mine.port,)):
            self.assertIn(mine.port, tor.discovery_ports())
            self.assertEqual(tor.discover(), [mine.port])

    def test_the_looked_at_ports_never_repeat_one(self):
        with patch.object(tor, "TOR_PORTS", (9050, 9150)), \
             patch.object(tor, "TOR_MANAGED_PORTS", (9050, 9052)):
            self.assertEqual(tor.discovery_ports(), (9050, 9150, 9052))

    def test_discovery_keeps_the_configured_order(self):
        with socks5_server() as first, socks5_server() as second:
            found = tor.discover(ports=(first.port, closed_port(), second.port))
            self.assertEqual(found, [first.port, second.port])


# --- verification ---------------------------------------------------------
class VerificationTests(unittest.IsolatedAsyncioTestCase):
    """Only Tor's own answer makes an exit Tor, and 'no answer' is not a no."""

    async def verify(self, handler, port: int = PORT) -> tor.Exit:
        factory, seen = factory_for(handler)
        result = await tor.verify(tor.Exit(port, tor.url(port)), client_factory=factory)
        self.assertEqual(seen, [tor.url(port)], "the exit itself must be the way it is asked")
        return result

    async def test_istor_true_is_a_confirmed_exit(self):
        result = await self.verify(answering())
        self.assertEqual(result.status, tor.STATUS_TOR)
        self.assertTrue(result.is_tor)
        self.assertTrue(result.usable)
        self.assertEqual(result.exit_ip, "185.220.101.7")
        self.assertEqual(result.label, "Tor exit")

    async def test_istor_false_is_usable_and_never_called_tor(self):
        result = await self.verify(answering('{"IsTor": false, "IP": "203.0.113.9"}'))
        self.assertEqual(result.status, tor.STATUS_NOT_TOR)
        self.assertFalse(result.is_tor)
        self.assertTrue(result.usable, "it can still carry a request")
        self.assertEqual(result.label, "SOCKS5, not Tor")

    async def test_the_lowercase_and_string_forms_are_read(self):
        self.assertEqual((await self.verify(answering('{"is_tor": true}'))).status, tor.STATUS_TOR)
        self.assertEqual((await self.verify(answering('{"IsTor": "true"}'))).status, tor.STATUS_TOR)
        self.assertEqual((await self.verify(answering('{"IsTor": "false"}'))).status, tor.STATUS_NOT_TOR)

    async def test_a_body_that_is_not_a_verdict_leaves_the_exit_unconfirmed(self):
        result = await self.verify(answering("<html>check.torproject.org</html>"))
        self.assertEqual(result.status, tor.STATUS_UNKNOWN)
        self.assertFalse(result.is_tor)
        self.assertTrue(result.usable)
        self.assertIn("not a Tor verdict", result.detail)

    async def test_a_timeout_is_unconfirmed_and_says_so(self):
        result = await self.verify(raising(httpx.ConnectTimeout("no answer")))
        self.assertEqual(result.status, tor.STATUS_UNKNOWN)
        self.assertIn("could not confirm as Tor", result.detail)
        self.assertIn("ConnectTimeout", result.detail)

    async def test_a_failing_checker_leaves_the_exit_unconfirmed(self):
        result = await self.verify(answering("boom", status=503))
        self.assertEqual(result.status, tor.STATUS_UNKNOWN)
        self.assertIn("503", result.detail)

    async def test_verifying_several_keeps_the_order_and_stays_inside_the_gate(self):
        live = 0
        peak = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            live -= 1
            return httpx.Response(200, text='{"IsTor": true, "IP": "185.220.101.7"}')

        exits = [tor.Exit(p, tor.url(p)) for p in (9500, 9501, 9502, 9503)]
        results = await tor.verify_many(exits, concurrency=2, client_factory=factory_for(handler)[0])
        self.assertEqual([r.port for r in results], [9500, 9501, 9502, 9503])
        self.assertLessEqual(peak, 2)

    async def test_an_exit_that_would_not_confirm_is_reported_as_unconfirmed(self):
        exits = [tor.Exit(9500, tor.url(9500))]
        results = await tor.verify_many(exits, client_factory=factory_for(raising(httpx.ReadTimeout("late")))[0])
        self.assertEqual(results[0].status, tor.STATUS_UNKNOWN)
        self.assertEqual(tor.usable_urls(results), [tor.url(9500)])
        self.assertNotEqual(results[0].label, "Tor exit")


# --- the cached picture ---------------------------------------------------
class SnapshotTests(unittest.TestCase):
    def setUp(self):
        tor.reset()
        self.saved = tor._state.exits

    def tearDown(self):
        tor._state.exits = self.saved
        tor.reset()

    def test_before_anything_is_asked_it_says_nothing_rather_than_guessing(self):
        snap = tor.snapshot()
        self.assertEqual(snap["state"], tor.STATE_NONE)
        self.assertEqual((snap["usable"], snap["confirmed"]), (0, 0))
        self.assertEqual(snap["urls"], [])
        self.assertIn("no SOCKS5 listener answered", snap["reason"])
        self.assertIn("shared", snap["note"])

    def test_disabled_is_a_state_of_its_own(self):
        with patch.object(tor, "TOR_ENABLED", False):
            snap = tor.snapshot()
            self.assertEqual(snap["state"], tor.STATE_OFF)
            self.assertIn("TOR_ENABLED=0", snap["reason"])

    def test_confirmed_counts_only_the_exits_that_said_yes(self):
        tor._state.exits = [
            tor.Exit(9500, tor.url(9500), tor.STATUS_TOR, exit_ip="185.220.101.7"),
            tor.Exit(9502, tor.url(9502), tor.STATUS_UNKNOWN, detail="no answer from the checker"),
        ]
        snap = tor.snapshot()
        self.assertEqual(snap["state"], tor.STATE_READY)
        self.assertEqual((snap["usable"], snap["confirmed"]), (2, 1))
        self.assertIn("185.220.101.7", tor.describe())
        self.assertIn("1 of 2 exits confirmed", snap["reason"])

    def test_a_socks_only_listener_puts_the_fallback_in_unconfirmed(self):
        tor._state.exits = [tor.Exit(9500, tor.url(9500), tor.STATUS_NOT_TOR, detail="it answered, and not as a Tor exit")]
        snap = tor.snapshot()
        self.assertEqual(snap["state"], tor.STATE_UNVERIFIED)
        self.assertEqual(snap["confirmed"], 0)
        self.assertIn("did not confirm as a Tor exit", snap["reason"])

    def test_the_pool_a_tor_run_gets_is_built_for_tor(self):
        kwargs = tor.pool_kwargs()
        self.assertEqual(kwargs["retire"], False, "there is no list to retire an exit from")
        self.assertEqual(kwargs["per_proxy"], tor.TOR_PER_PROXY)
        self.assertEqual(kwargs["timeout"].read, tor.TOR_TIMEOUT)
        self.assertEqual(server._pool_kwargs(False, 64, ["one"], "tor"), kwargs)


# --- starting one ---------------------------------------------------------
class StartTests(unittest.TestCase):
    def setUp(self):
        tor.reset()
        self.saved_proc = tor._state.proc

    def tearDown(self):
        proc = tor._state.proc
        terminate = getattr(proc, "terminate", None)
        if proc is not None and proc is not self.saved_proc and terminate and proc.poll() is None:
            terminate()  # the stand-in for tor must not outlive its test
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        tor._state.proc = self.saved_proc
        tor.reset()

    def test_a_missing_binary_is_a_sentence_not_a_traceback(self):
        with patch.object(tor, "TOR_BINARY", "definitely-not-a-real-tor-binary"):
            ready, note = tor.start(timeout=1)
        self.assertFalse(ready)
        self.assertIn("not installed", note)
        self.assertIsNone(tor._state.proc)

    def test_an_unwritable_data_directory_is_reported(self):
        with patch.object(tor, "TOR_BINARY", "sh"), \
             patch.object(tor, "TOR_DATA_DIR", "/proc/definitely/not/writable"):
            ready, note = tor.start(timeout=1)
        self.assertFalse(ready)
        self.assertIn("could not", note)

    def test_a_bootstrap_that_never_finishes_is_not_reported_as_ready(self):
        # `tail -f <torrc>` is a stand-in for a tor that never finishes: it runs,
        # it never says Bootstrapped 100%, and the point is that a timeout is
        # reported as "still bootstrapping" rather than as success.
        with patch.object(tor, "TOR_BINARY", "tail"), \
             patch.object(tor, "TOR_DATA_DIR", os.path.join(tempfile.mkdtemp(), "tor")):
            started = time.time()
            ready, note = tor.start(timeout=1)
        self.assertFalse(ready)
        self.assertIn("still bootstrapping", note)
        self.assertIsNotNone(tor._state.proc, "a tor still bootstrapping is left running")
        self.assertLess(time.time() - started, 10)

    def test_the_auto_start_flag_decides_whether_it_starts_by_itself(self):
        calls = []

        def fake_start(timeout=tor.TOR_START_TIMEOUT):
            calls.append(timeout)
            return False, "not today"

        with patch.object(tor, "discover", return_value=[]), \
             patch.object(tor, "start", fake_start), \
             patch.object(tor, "TOR_AUTO_START", False):
            self.assertEqual(asyncio.run(tor.exits(force=True)), [])
        self.assertEqual(calls, [], "auto-start is off, so nothing may start")
        self.assertIn("TOR_AUTO_START=0", tor.snapshot()["reason"])

    def test_pressing_start_is_a_request_and_ignores_the_auto_flag(self):
        calls = []

        def fake_start(timeout=tor.TOR_START_TIMEOUT):
            calls.append(timeout)
            return False, "not today"

        with patch.object(tor, "discover", return_value=[]), \
             patch.object(tor, "start", fake_start), \
             patch.object(tor, "TOR_AUTO_START", False):
            self.assertEqual(asyncio.run(tor.start_now()), [])
        self.assertEqual(len(calls), 1)

    def test_a_stale_log_cannot_make_a_new_start_look_ready(self):
        # Readiness is read out of tor's own log, so an *appended* log made the
        # previous run's "Bootstrapped 100%" the new run's answer: a real start
        # reported "bootstrapped in 0.0s" and then found no listener, because it
        # was reading history.
        directory = os.path.join(tempfile.mkdtemp(), "tor")
        os.makedirs(directory)
        with patch.object(tor, "TOR_DATA_DIR", directory):
            open(tor.log_path(), "w", encoding="utf-8").write(
                "[notice] Bootstrapped 100% (done): Done\n")
            with patch.object(tor, "TOR_BINARY", "tail"):  # runs, never bootstraps
                ready, note = tor.start(timeout=1)
            kept = open(tor.log_path(), encoding="utf-8").read() if os.path.exists(tor.log_path()) else ""
        self.assertFalse(ready)
        self.assertIn("still bootstrapping", note)
        self.assertNotIn("Bootstrapped 100%", kept, "the log must start from this run")

    def test_a_listener_that_appears_just_after_the_start_is_still_found(self):
        # tor opens its SocksPorts and announces 100% in that order; if the
        # second half of that is ever reversed, this is what stops a run from
        # being told there is no fallback one millisecond after one started.
        calls = []

        def appear(**kwargs) -> list[int]:
            calls.append(1)
            return [] if len(calls) == 1 else [9050]

        async def fake_verify(exit_, **kwargs):
            return tor.Exit(exit_.port, exit_.url, tor.STATUS_TOR)

        with patch.object(tor, "discover", appear), \
             patch.object(tor, "start", lambda timeout=0: (True, "booted")), \
             patch.object(tor, "verify", fake_verify), \
             patch.object(tor, "TOR_BIND_WAIT", 1.0):
            found = asyncio.run(tor.exits(force=True))
        self.assertEqual([e.port for e in found], [9050])
        self.assertGreater(len(calls), 1, "it has to look more than once")

    def test_a_start_that_says_ready_but_binds_nothing_is_reported(self):
        async def fake_verify(exit_, **kwargs):  # pragma: no cover - never reached
            raise AssertionError("nothing to verify")

        with patch.object(tor, "discover", return_value=[]), \
             patch.object(tor, "start", lambda timeout=0: (True, "booted")), \
             patch.object(tor, "verify", fake_verify), \
             patch.object(tor, "TOR_BIND_WAIT", 0.2):
            self.assertEqual(asyncio.run(tor.exits(force=True)), [])
        self.assertIn("no SocksPort was accepting", tor.snapshot()["error"])

    def test_an_empty_answer_is_retried_sooner_than_a_confirmed_one(self):
        calls = []

        def nothing() -> list[int]:
            calls.append(1)
            return []

        with patch.object(tor, "discover", nothing), \
             patch.object(tor, "TOR_AUTO_START", False):
            asyncio.run(tor.exits(force=True))
            self.assertEqual(len(calls), 1)
            asyncio.run(tor.exits())  # cached: an answer was just given
            self.assertEqual(len(calls), 1, "a fresh answer is not re-asked for")
            # ...but an empty one has aged out after TOR_RETRY_AFTER, not after
            # the full TOR_VERIFY_TTL, because a tor may be bootstrapping.
            tor._state.verified_at -= tor.TOR_VERIFY_TTL - tor.TOR_RETRY_AFTER
            asyncio.run(tor.exits())
            self.assertEqual(len(calls), 2, "an empty answer must be looked at again")

    def test_a_disabled_service_refuses_to_start_at_all(self):
        with patch.object(tor, "TOR_ENABLED", False), \
             patch.object(tor, "start", side_effect=AssertionError("must not start")), \
             patch.object(tor, "discover", side_effect=AssertionError("must not even look")):
            self.assertEqual(asyncio.run(tor.exits(force=True)), [])
            self.assertEqual(asyncio.run(tor.start_now()), [])

    def test_exits_are_cached_and_force_asks_again(self):
        verified = 0

        async def fake_verify(exit_, **kwargs):
            nonlocal verified
            verified += 1
            return tor.Exit(exit_.port, exit_.url, tor.STATUS_TOR, exit_ip="185.220.101.7")

        with patch.object(tor, "discover", return_value=[9500]), \
             patch.object(tor, "verify", fake_verify):
            self.assertEqual(len(asyncio.run(tor.exits(force=True))), 1)
            self.assertEqual(verified, 1)
            first = tor.version
            self.assertEqual(len(asyncio.run(tor.exits())), 1)
            self.assertEqual(verified, 1, "a cached picture must not be re-asked for")
            self.assertEqual(tor.version, first, "nothing changed, so nothing moved")
            asyncio.run(tor.exits(force=True))
            self.assertEqual(verified, 2)

    def test_a_first_start_that_is_still_bootstrapping_is_not_reported_as_failure(self):
        # tor keeps running (pid set), so the next request finds it up instead of
        # starting a second one -- the error text says it is still coming.
        class FakeProc:
            pid = 4242
            def poll(self):
                return None

        tor._state.proc = FakeProc()  # type: ignore[assignment]
        with patch.object(tor, "discover", return_value=[]), \
             patch.object(tor, "start", return_value=(False, "tor is still bootstrapping after 75s")):
            asyncio.run(tor.exits(force=True))
        self.assertIn("still bootstrapping", tor.snapshot()["error"])


# --- the pool -------------------------------------------------------------
class PoolFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Where an empty list takes a run, and what a stored list is allowed to do."""

    def setUp(self):
        self.saved_state = tor._state.exits
        tor.reset()
        self.saved = server.proxy_list
        self.tmp = tempfile.TemporaryDirectory()
        server.proxy_list = server.proxy_list_module.ProxyList(self.tmp.name + "/proxies.json")
        for state in server._pool_state.values():
            state["pool"] = None
            state["source"] = ""
            state["version"] = -1
            state["tor_version"] = -1

    def tearDown(self):
        for state in server._pool_state.values():
            pool = state["pool"]
            if isinstance(pool, sniper.ProxyPool):
                asyncio.run(pool.aclose())
            state["pool"] = None
        server.proxy_list = self.saved
        tor._state.exits = self.saved_state
        tor.reset()
        self.tmp.cleanup()

    def confirmed(self, ports=(9050, 9052)):
        async def fake_verify(exit_, **kwargs):
            return tor.Exit(exit_.port, exit_.url, tor.STATUS_TOR, exit_ip="185.220.101.7")

        return patch.object(tor, "discover", return_value=list(ports)), \
            patch.object(tor, "verify", fake_verify)

    async def test_an_empty_list_goes_out_over_tor(self):
        discovering, verifying = self.confirmed()
        with discovering, verifying:
            pool, source = await server._pool("snipe", 8)
        self.assertEqual(source, "tor")
        self.assertEqual(
            sorted(pool.all()),
            ["socks5h://127.0.0.1:9050", "socks5h://127.0.0.1:9052"],
        )
        self.assertFalse(pool.retirable, "a Tor pool has no replacement for an exit")
        self.assertEqual(pool.per_proxy, tor.TOR_PER_PROXY)

    async def test_a_stored_list_is_never_replaced_by_tor(self):
        server.proxy_list.replace(["http://192.0.2.9:8080"])

        async def refuse(*args, **kwargs):
            raise AssertionError("Tor must not be consulted while a list is stored")

        with patch.object(tor, "exits", refuse):
            pool, source = await server._pool("snipe", 8)
        self.assertEqual(source, "list")
        self.assertEqual(pool.all(), ["http://192.0.2.9:8080"])
        self.assertTrue(pool.retirable)

    async def test_a_list_pasted_while_tor_was_coming_up_wins(self):
        # The Tor wait happens outside the pool lock, so the list really can
        # change underneath it. Re-reading is what keeps a paste from being
        # ignored by a run that started a moment too early.
        async def pasted_while_waiting(*args, **kwargs):
            server.proxy_list.replace(["http://192.0.2.9:8080"])
            return [tor.Exit(9050, tor.url(9050), tor.STATUS_TOR)]

        with patch.object(tor, "exits", pasted_while_waiting):
            pool, source = await server._pool("snipe", 8)
        self.assertEqual(source, "list")
        self.assertEqual(pool.all(), ["http://192.0.2.9:8080"])

    async def test_a_run_over_tor_says_what_it_is_before_it_spends_a_name(self):
        discovering, verifying = self.confirmed()
        with discovering, verifying:
            pool, note = await server._prepare_pool("snipe", 8)
        self.assertIn("Tor fallback", note)
        self.assertIn("no list stored", note)
        self.assertIn("confirmed as Tor", note)
        self.assertIn("refuse them far more often", note)
        self.assertNotIn("only 2 proxies in the list", note, "there is no list to warn about")

    async def test_no_list_and_no_tor_names_both_halves(self):
        def not_installed(timeout=tor.TOR_START_TIMEOUT):
            return False, "tor is not installed here (looked for 'tor' on PATH)"

        with patch.object(tor, "discover", return_value=[]), patch.object(tor, "start", not_installed):
            with self.assertRaises(server.HTTPException) as caught:
                await server._prepare_pool("snipe", 8)
        detail = caught.exception.detail
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("no proxies saved yet", detail)
        self.assertIn("Tor fallback is not available either", detail)
        self.assertIn("not installed", detail)

    async def test_the_pool_is_rebuilt_when_tors_exits_change(self):
        discovering, verifying = self.confirmed()
        with discovering, verifying:
            first, source = await server._pool("snipe", 8)
        self.assertEqual(len(first), 2)
        tor.reset()  # tor's own picture changed under the live pool
        discovering, verifying = self.confirmed(ports=(9050,))
        with discovering, verifying:
            second, source = await server._pool("snipe", 8)
        self.assertEqual(len(second), 1, "a pool over a set of exits that no longer exists is stale")

    async def test_a_warm_tor_pool_is_reused(self):
        discovering, verifying = self.confirmed()
        with discovering, verifying:
            first, _ = await server._pool("snipe", 8)
            second, _ = await server._pool("snipe", 8)
        self.assertIs(first, second, "the warm tunnels are the point of caching the pool")


# --- a pool that cannot be replenished ------------------------------------
class RetirementTests(unittest.TestCase):
    """The same failures, against a list that can be replaced and one that cannot."""

    PROXY = "socks5h://127.0.0.1:9050"

    def fail_a_pool(self, pool: sniper.ProxyPool, times: int = 8) -> None:
        for _ in range(times):
            proxy = pool.next()
            if proxy is None:
                return  # the pool is empty; that is this helper's stopping point
            pool.report(proxy, sniper.OUTCOME_FAILED, platform="discord")

    def test_a_tor_pool_is_never_emptied(self):
        pool = sniper.ProxyPool([self.PROXY], retire=False, fail_limit=2, cooldown=30)
        self.fail_a_pool(pool)
        self.assertEqual(pool.retired(), 0, "deleting the last exit ends the run instead of fixing it")
        self.assertEqual(pool.alive(), 1)
        self.assertEqual(len(pool), 1)
        self.assertGreaterEqual(pool.resting(), 1)

    def test_a_normal_pool_still_retires_a_dead_proxy(self):
        pool = sniper.ProxyPool([self.PROXY], fail_limit=2)
        self.fail_a_pool(pool)
        self.assertEqual(pool.retired(), 1)
        self.assertEqual(pool.alive(), 0)
        self.assertTrue(pool.retirable)

    def test_a_blocked_tor_exit_is_rested_rather_than_deleted(self):
        pool = sniper.ProxyPool([self.PROXY], retire=False, block_limit=3, block_cooldown=5)
        for _ in range(30):
            proxy = pool.next()
            pool.report(proxy, sniper.OUTCOME_BLOCKED, platform="tiktok")
        self.assertEqual(pool.retired(), 0)
        self.assertEqual(pool.alive(), 1)
        self.assertGreaterEqual(pool.resting(), 1)

    def test_a_single_dead_proxy_does_end_a_normal_run(self):
        # No replacement is only *safe* for a pool that never had one to lose.
        # A one-line list whose only proxy cannot carry anything is a run that
        # has to stop, and the pool says so rather than pretending otherwise.
        pool = sniper.ProxyPool([self.PROXY], fail_limit=2)
        self.fail_a_pool(pool, times=2)
        self.assertEqual(pool.retired(), 1)
        self.assertEqual(pool.alive(), 0)


# --- the HTTP surface -----------------------------------------------------
class TorEndpointTests(unittest.TestCase):
    """Through the real session, CSRF and middleware -- the interface a user has."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "APP_ORIGIN": ORIGIN, "AUTH_SECRET": "test-only-" + "x" * 40,
            "ACCOUNT_DB_PATH": self.tmp.name + "/accounts.db", "APP_ENV": "test",
            "STORE_PATH": self.tmp.name + "/proxies.json", "TRUSTED_PROXY_CIDRS": "",
        })
        self.env.start()
        self.saved_store = accounts.store
        accounts.store = accounts.Store("")
        accounts.store.initialize()
        self.saved_list = server.proxy_list
        server.proxy_list = server.proxy_list_module.ProxyList(self.tmp.name + "/proxies.json")
        self.saved_tor = tor._state.exits
        tor.reset()
        server._checks_in_flight.clear()
        server._runs.clear()
        self.client = TestClient(server.app, client=("127.0.0.1", 50000))
        self.headers = {"Origin": ORIGIN}
        self.session = self.signup("toruser", self.client)

    def tearDown(self):
        server._checks_in_flight.clear()
        server._runs.clear()
        server.proxy_list = self.saved_list
        tor._state.exits = self.saved_tor
        tor.reset()
        self.client.close()
        accounts.store = self.saved_store
        self.env.stop()
        self.tmp.cleanup()

    def solve(self, action, client=None):
        c = client or self.client
        challenge = c.get("/auth/challenge?action=" + action).json()
        with accounts.store.transaction() as db:
            entry = db.one("SELECT word,target FROM challenges WHERE id=?", (challenge["id"],))
        return {"challenge_id": challenge["id"], "drag": entry["target"], "word": entry["word"]}

    def signup(self, name, client=None):
        c = client or self.client
        r = c.post("/auth/signup", headers=self.headers, json={
            "username": name, "password": "correct horse 42", **self.solve("signup", c)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def csrf(self, session=None):
        return {**self.headers, "X-CSRF-Token": (session or self.session)["csrf"]}

    def verified(self, ports=(9050, 9052)):
        async def fake_exits(*args, **kwargs):
            return [tor.Exit(p, tor.url(p), tor.STATUS_TOR, exit_ip="185.220.101.7") for p in ports]

        return patch.object(tor, "exits", fake_exits)

    def lines(self, response) -> list[dict]:
        return [json.loads(line) for line in response.iter_lines() if line.strip()]

    # --- status ---
    def test_the_status_names_the_state_and_never_calls_a_listener_tor(self):
        with patch.object(tor, "discover", return_value=[9500]), \
             patch.object(tor, "verify", new=_unknown), \
             patch.object(tor, "TOR_AUTO_START", False):
            r = self.client.get("/proxies/tor")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["state"], tor.STATE_UNVERIFIED)
        self.assertEqual(data["confirmed"], 0)
        self.assertEqual([e["status"] for e in data["exits"]], [tor.STATUS_UNKNOWN])
        # A listener that answered and would not confirm is reported as exactly
        # that -- not as Tor, and not as nothing.
        self.assertIn("none could be confirmed as Tor", data["reason"])
        self.assertEqual(data["exits"][0]["label"], "SOCKS5, unconfirmed")

    def test_a_listener_that_says_it_is_not_tor_is_reported_as_not_tor(self):
        async def not_tor(exit_, **kwargs):
            return tor.Exit(exit_.port, exit_.url, tor.STATUS_NOT_TOR,
                            detail="it answered, and not as a Tor exit")

        with patch.object(tor, "discover", return_value=[9500]), \
             patch.object(tor, "verify", new=not_tor), \
             patch.object(tor, "TOR_AUTO_START", False):
            data = self.client.get("/proxies/tor").json()
        self.assertEqual(data["state"], tor.STATE_UNVERIFIED)
        self.assertIn("did not confirm as a Tor exit", data["reason"])
        self.assertEqual(data["exits"][0]["label"], "SOCKS5, not Tor")

    def test_a_confirmed_exit_shows_up_with_its_address(self):
        with patch.object(tor, "discover", return_value=[9050]), \
             patch.object(tor, "verify", new=_confirmed), \
             patch.object(tor, "TOR_AUTO_START", False):
            data = self.client.get("/proxies/tor").json()
        self.assertEqual(data["state"], tor.STATE_READY)
        self.assertEqual(data["confirmed"], 1)
        self.assertEqual(data["urls"], [tor.url(9050)])
        self.assertEqual(data["exits"][0]["exit_ip"], "185.220.101.7")

    def test_starting_is_a_post_with_a_token(self):
        # The route reads the flag the server imported, which is the value the
        # service booted with.
        with patch.object(server, "TOR_ENABLED", False):
            r = self.client.post("/proxies/tor/start", headers=self.csrf())
            self.assertEqual(r.status_code, 409)
            self.assertIn("TOR_ENABLED=0", r.json()["detail"])
        called = []

        async def fake_start_now():
            called.append(1)
            return []

        with patch.object(tor, "start_now", fake_start_now):
            r = self.client.post("/proxies/tor/start", headers=self.csrf())
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["state"], tor.STATE_NONE)
        self.assertEqual(len(called), 1)
        anon = TestClient(server.app, client=("127.0.0.1", 50003))
        try:
            self.assertEqual(anon.post("/proxies/tor/start", headers=self.headers).status_code, 401)
        finally:
            anon.close()
        with patch.object(tor, "start_now", fake_start_now):
            self.assertEqual(
                self.client.post("/proxies/tor/start", headers=self.headers).status_code, 403,
                "a signed-in request without the CSRF token is a different refusal",
            )

    def test_checking_the_fallback_asks_the_exits_a_run_would_use(self):
        with self.verified(), patch.object(proxycheck, "probe", alive):
            events = self.lines(self.client.post(
                "/proxies/check", headers=self.csrf(), json={"source": "tor"}))
        self.assertEqual(events[0]["type"], "start")
        self.assertEqual(events[0]["source"], "tor")
        self.assertEqual(events[0]["target"], server.TOR_CHECK_URL)
        self.assertEqual(events[0]["concurrency"], server.TOR_CONCURRENCY)
        self.assertEqual(events[0]["total"], 2)
        results = [e for e in events if e["type"] == "result"]
        self.assertEqual({r["proxy"] for r in results}, {tor.url(9050), tor.url(9052)})
        self.assertEqual(events[-1]["alive"], 2)
        self.assertEqual(server._checks_in_flight, {}, "the check must release its slot")

    def test_checking_the_fallback_with_nothing_to_check_says_why(self):
        with patch.object(tor, "discover", return_value=[]), \
             patch.object(tor, "TOR_AUTO_START", False):
            r = self.client.post("/proxies/check", headers=self.csrf(), json={"source": "tor"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("no Tor exits to test", r.json()["detail"])

    def test_the_check_does_not_ask_the_target_from_this_host_first(self):
        # Requests go out through the exits, so a host that cannot reach the
        # target directly is exactly the host the fallback exists for.
        def refuse(*args, **kwargs):
            raise AssertionError("a Tor check must not gate on this host's own egress")

        with self.verified(), patch.object(proxycheck, "probe", alive), \
             patch.object(proxycheck, "target_reachable", refuse):
            r = self.client.post("/proxies/check", headers=self.csrf(), json={"source": "tor"})
            self.assertEqual(r.status_code, 200, r.text)

    def test_an_inferred_source_still_means_the_stored_list(self):
        server.proxy_list.replace(["http://192.0.2.9:8080"])

        async def reachable(*args, **kwargs):
            return True, "HTTP 200 in 5 ms"

        with patch.object(proxycheck, "probe", alive), \
             patch.object(proxycheck, "target_reachable", reachable):
            events = self.lines(self.client.post("/proxies/check", headers=self.csrf(), json={}))
        self.assertEqual(events[0]["source"], "stored")
        self.assertEqual(events[0]["target"], server.PROXY_CHECK_URL)

    def test_health_and_info_report_the_fallback_without_asking_it_anything(self):
        with patch.object(tor, "discover", side_effect=AssertionError("health must not look")), \
             patch.object(tor, "start", side_effect=AssertionError("health must not start")):
            health = self.client.get("/health").json()
            info = self.client.get("/info").json()
        self.assertIn("tor", health)
        self.assertEqual(health["tor"]["state"], tor.STATE_NONE)
        self.assertIn("tor", info)
        self.assertIn("GET /proxies/tor", info["endpoints"])
        self.assertIn("POST /proxies/tor/start", info["endpoints"])
        self.assertIn("fallback", info["honesty"])

    def test_the_new_surface_is_behind_the_same_paywall(self):
        # The fallback is part of the /proxies surface, so an account with no
        # access is answered the same way it is for a stored list -- and that has
        # to hold for both new routes, not just the one the page reads first.
        second = TestClient(server.app, client=("127.0.0.1", 50005))
        try:
            session = self.signup("torless", second)
            with accounts.store.transaction() as db:
                db.execute(
                    "UPDATE users SET trial_available=0,trial_started=?,trial_until=? WHERE id=?",
                    (time.time() - accounts.TRIAL_SECONDS, time.time() - 1, session["user"]["id"]),
                )
            self.assertEqual(second.get("/proxies/tor").status_code, 402)
            self.assertEqual(
                second.post("/proxies/tor/start", headers=self.csrf(session)).status_code, 402)
        finally:
            second.close()

    def test_signed_out_requests_are_refused(self):
        anon = TestClient(server.app, client=("127.0.0.1", 50004))
        try:
            self.assertEqual(anon.get("/proxies/tor").status_code, 401)
        finally:
            anon.close()


async def _unknown(exit_: tor.Exit, **kwargs) -> tor.Exit:
    return tor.Exit(exit_.port, exit_.url, tor.STATUS_UNKNOWN, detail="the check answered something that is not a Tor verdict")


async def _confirmed(exit_: tor.Exit, **kwargs) -> tor.Exit:
    return tor.Exit(exit_.port, exit_.url, tor.STATUS_TOR, exit_ip="185.220.101.7")



if __name__ == "__main__":
    unittest.main(verbosity=2)
