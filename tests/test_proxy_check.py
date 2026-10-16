"""The proxy check: what each failure is called, and what the page is told.

Two layers are tested, and they are different questions. The classification
tests ask what one proxy's outcome *means*, which is only assertable with an
exception raised on demand -- ``httpx.MockTransport`` is what makes that
possible, and none of those tests needs the network. The endpoint tests ask what
the HTTP surface does with that answer: that a stream reports every proxy, that a
check refuses rather than blaming the list when the target itself is unreachable,
and that a paste can be tested without being stored.
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import accounts
import proxycheck
import server

ORIGIN = "http://testserver"
PROXY = "http://198.51.100.7:8080"


class _FakeSocksError(Exception):
    """socksio's handshake failure, which is not an httpx error at all.

    The proxy library raises its own types and ``sniper.proxy_fault`` tells them
    apart by module name, so the module is what this class fakes.
    """


_FakeSocksError.__module__ = "socks"


def factory_for(handler):
    """A ``client_factory`` that never touches the network, and records the proxy."""

    seen: list[str] = []

    def make(proxy: str, timeout: float) -> httpx.AsyncClient:
        seen.append(proxy)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    return make, seen


def answering(payload: str = '{"ip": "203.0.113.9"}', status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=payload)
    return handler


def raising(exc: BaseException):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


class ClassificationTests(unittest.IsolatedAsyncioTestCase):
    """One proxy, one answer, one name for it."""

    async def classify(self, handler) -> proxycheck.Probe:
        factory, seen = factory_for(handler)
        result = await proxycheck.probe(PROXY, client_factory=factory)
        self.assertEqual(seen, [PROXY], "the proxy under test must be the one used")
        return result

    async def test_an_answer_is_alive_and_names_the_exit(self):
        result = await self.classify(answering())
        self.assertTrue(result.ok)
        self.assertEqual(result.status, proxycheck.STATUS_ALIVE)
        self.assertEqual(result.exit_ip, "203.0.113.9")
        self.assertEqual(result.http, 200)
        self.assertFalse(result.exit_fault)
        self.assertIsInstance(result.ms, int)

    async def test_a_bare_address_body_still_names_the_exit(self):
        result = await self.classify(answering("203.0.113.7\n"))
        self.assertEqual(result.exit_ip, "203.0.113.7")

    async def test_an_origin_body_is_read_too(self):
        result = await self.classify(answering('{"origin": "198.51.100.4, 10.0.0.1"}'))
        self.assertEqual(result.exit_ip, "198.51.100.4")

    async def test_a_body_that_is_not_an_address_leaves_the_exit_blank(self):
        result = await self.classify(answering("<html>hello</html>"))
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_ip, "")

    async def test_a_target_that_refuses_the_exit_is_not_the_proxys_failure(self):
        # 403/429 from the far side: the tunnel carried the request, so the exit
        # works and the target blocked that address. Deleting a working list for
        # the target's opinion is the mistake this status exists to prevent.
        result = await self.classify(answering("denied", status=403))
        self.assertEqual(result.status, proxycheck.STATUS_BLOCKED)
        self.assertFalse(result.ok)
        self.assertFalse(result.exit_fault)
        self.assertIn("403", result.detail)

    async def test_407_is_credentials_and_is_the_one_repair_the_user_can_type(self):
        for exc in (
            httpx.ProxyError("407 Proxy Authentication Required"),
            httpx.ProxyError("Tunnel connection failed: 407 Proxy Authentication Required"),
        ):
            result = await self.classify(raising(exc))
            self.assertEqual(result.status, proxycheck.STATUS_AUTH, str(exc))
            self.assertTrue(result.exit_fault)

    async def test_407_as_a_response_status_is_credentials_too(self):
        result = await self.classify(answering("auth", status=407))
        self.assertEqual(result.status, proxycheck.STATUS_AUTH)

    async def test_a_silent_exit_is_no_answer(self):
        result = await self.classify(raising(httpx.ConnectTimeout("")))
        self.assertEqual(result.status, proxycheck.STATUS_NO_ANSWER)
        self.assertTrue(result.exit_fault)
        # The empty message is replaced rather than shown as nothing at all.
        self.assertIn("no answer within the timeout", result.detail)

    async def test_an_exit_that_refuses_the_socket_is_refused(self):
        for exc in (httpx.ConnectError("Connection refused"),
                    httpx.ProxyError("Tunnel connection failed: 403 Forbidden")):
            result = await self.classify(raising(exc))
            self.assertEqual(result.status, proxycheck.STATUS_REFUSED, str(exc))
            self.assertTrue(result.exit_fault)

    async def test_a_socks_handshake_failure_blames_the_proxy(self):
        result = await self.classify(raising(_FakeSocksError("no acceptable auth methods")))
        self.assertEqual(result.status, proxycheck.STATUS_REFUSED)
        self.assertTrue(result.exit_fault)

    async def test_a_late_answer_is_the_targets_fault_not_the_exits(self):
        # The tunnel opened: bytes went out and the answer was late. A run makes
        # the same distinction (sniper.proxy_fault) and so must the check, or a
        # slow target retires a working list.
        result = await self.classify(raising(httpx.ReadTimeout("")))
        self.assertEqual(result.status, proxycheck.STATUS_SLOW)
        self.assertFalse(result.exit_fault)
        self.assertFalse(result.ok)

    async def test_an_unexpected_error_is_not_dressed_up_as_a_verdict(self):
        result = await self.classify(raising(ValueError("something else entirely")))
        self.assertEqual(result.status, proxycheck.STATUS_SLOW)
        self.assertFalse(result.exit_fault)
        self.assertTrue(result.detail)

    async def test_a_failure_never_carries_credentials_into_the_detail(self):
        factory, _ = factory_for(raising(httpx.ConnectError("Connection refused")))
        result = await proxycheck.probe("http://user:sup3rsecret@198.51.100.7:8080",
                                        client_factory=factory)
        self.assertNotIn("sup3rsecret", result.detail)

    def test_the_label_table_covers_every_status(self):
        for status in (proxycheck.STATUS_ALIVE, proxycheck.STATUS_AUTH, proxycheck.STATUS_REFUSED,
                       proxycheck.STATUS_NO_ANSWER, proxycheck.STATUS_BLOCKED,
                       proxycheck.STATUS_SLOW, proxycheck.STATUS_UNKNOWN):
            self.assertIn(status, proxycheck.STATUS_LABEL)


class SweepTests(unittest.IsolatedAsyncioTestCase):
    """The whole list, in parallel, one line per proxy."""

    async def test_every_proxy_is_reported_exactly_once(self):
        proxies = [f"http://198.51.100.{n}:8080" for n in range(10)]

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0)
            return httpx.Response(200, text='{"ip": "203.0.113.9"}')

        factory, seen = factory_for(handler)
        results = [r async for r in proxycheck.check_many(proxies, concurrency=3, client_factory=factory)]
        self.assertEqual(sorted(r.proxy for r in results), sorted(proxies))
        self.assertEqual(sorted(seen), sorted(proxies))
        self.assertEqual(len(results), 10)

    async def test_the_sweep_stops_when_the_stop_event_is_set(self):
        proxies = [f"http://198.51.100.{n}:8080" for n in range(20)]
        stop = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.01)
            return httpx.Response(200, text="{}")

        factory, _ = factory_for(handler)
        stream = proxycheck.check_many(proxies, concurrency=2, stop=stop, client_factory=factory)
        first = await stream.__anext__()
        stop.set()
        rest = [r async for r in stream]
        self.assertLess(len(rest) + 1, len(proxies), "a stopped sweep must not wait for the tail")

    async def test_a_worker_that_blows_up_does_not_hang_the_sweep(self):
        # The reader waits on one line per proxy. A probe raising instead of
        # answering used to leave that line unpublished and the whole report
        # waiting forever, which is the one failure mode a stream cannot recover
        # from -- so the worker publishes it as an unreadable answer.
        async def broken(proxy, **kwargs):
            raise RuntimeError("boom")

        with patch.object(proxycheck, "probe", broken):
            results = [r async for r in proxycheck.check_many(["http://a:1", "http://b:2"])]
        self.assertEqual(len(results), 2)
        self.assertEqual({r.status for r in results}, {proxycheck.STATUS_UNKNOWN})
        self.assertIn("RuntimeError: boom", results[0].detail)

    async def test_an_empty_list_yields_nothing(self):
        factory, _ = factory_for(answering())
        self.assertEqual([r async for r in proxycheck.check_many([], client_factory=factory)], [])

    def test_summary_separates_dead_exits_from_a_blocking_target(self):
        results = [
            proxycheck.Probe("http://a:1", proxycheck.STATUS_ALIVE, 12, http=200, exit_ip="203.0.113.1"),
            proxycheck.Probe("http://b:1", proxycheck.STATUS_NO_ANSWER, 6000, detail="ConnectTimeout"),
            proxycheck.Probe("http://c:1", proxycheck.STATUS_BLOCKED, 20, http=403),
        ]
        summary = proxycheck.summarise(results, 6.0)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["alive"], 1)
        # Only the silent exit is dead; the blocked one is the target's opinion.
        self.assertEqual(summary["dead"], 1)
        self.assertEqual(summary["counts"][proxycheck.STATUS_BLOCKED], 1)
        self.assertEqual(summary["labels"][proxycheck.STATUS_BLOCKED], "target blocked this exit")
        self.assertEqual(summary["elapsed_s"], 6.0)
        self.assertIn("1 of 3 proxies answered", proxycheck.describe(results))

    def test_a_probe_serialises_with_the_label_the_page_shows(self):
        result = proxycheck.Probe(PROXY, proxycheck.STATUS_AUTH, 41, http=407, detail="HTTP 407")
        row = result.as_dict()
        self.assertEqual(row["label"], "credentials rejected")
        self.assertFalse(row["ok"])
        self.assertEqual(row["proxy"], PROXY)


class TargetTests(unittest.IsolatedAsyncioTestCase):
    """The target is asked first, with no proxy in the way."""

    async def reachable(self, handler) -> tuple[bool, str]:
        # The real class is captured first: patching the attribute on the httpx
        # module that proxycheck imports would otherwise make the replacement
        # call itself.
        real = httpx.AsyncClient
        with patch.object(proxycheck.httpx, "AsyncClient",
                          lambda **kwargs: real(transport=httpx.MockTransport(handler))):
            return await proxycheck.target_reachable("https://target.test/")

    async def test_a_working_target_says_so(self):
        ok, note = await self.reachable(answering())
        self.assertTrue(ok, note)
        self.assertIn("HTTP 200", note)

    async def test_a_failing_target_names_itself_rather_than_the_list(self):
        ok, note = await self.reachable(raising(httpx.ConnectError("Name or service not known")))
        self.assertFalse(ok)
        self.assertIn("ConnectError", note)

    async def test_a_non_2xx_target_counts_as_unreachable(self):
        ok, note = await self.reachable(answering("go away", status=503))
        self.assertFalse(ok)
        self.assertIn("503", note)


async def alive(proxy: str, **kwargs) -> proxycheck.Probe:
    """A probe that always answers -- the shape ``check_many`` awaits."""
    return proxycheck.Probe(proxy, proxycheck.STATUS_ALIVE, 11, http=200, exit_ip="203.0.113.5")


class EndpointTests(unittest.TestCase):
    """The endpoints, through the same middleware and session a browser uses."""

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
        server._runs.clear()
        server._checks_in_flight.clear()
        self.client = TestClient(server.app, client=("127.0.0.1", 50000))
        self.headers = {"Origin": ORIGIN}
        self.session = self.signup("checkuser", self.client)

    def tearDown(self):
        server._checks_in_flight.clear()
        server._runs.clear()
        server.proxy_list = self.saved_list
        self.client.close()
        accounts.store = self.saved_store
        self.env.stop()
        self.tmp.cleanup()

    # --- session helpers, the same flow a browser runs ---
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

    def store(self, proxies):
        r = self.client.post("/proxies", headers=self.csrf(),
                             json={"text": "\n".join(proxies), "mode": "replace"})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def lines(self, response) -> list[dict]:
        return [json.loads(line) for line in response.iter_lines() if line.strip()]

    def check(self, body=None, target=(True, "HTTP 200 in 9 ms"), probe=alive):
        async def reachable(*args, **kwargs):
            return target

        with patch.object(proxycheck, "probe", probe), \
             patch.object(proxycheck, "target_reachable", reachable):
            return self.client.post("/proxies/check", headers=self.csrf(), json=body or {})

    # --- the surface ---
    def test_signed_out_requests_are_refused(self):
        # A fresh client: setUp signs one in, and a signed-in request without the
        # token is a different refusal (403) from no session at all (401).
        anon = TestClient(server.app, client=("127.0.0.1", 50002))
        try:
            for path in ("/proxies/check", "/proxies/prune"):
                self.assertEqual(anon.post(path, headers=self.headers, json={}).status_code, 401, path)
        finally:
            anon.close()

    def test_a_check_without_a_token_is_refused_even_when_signed_in(self):
        self.store([PROXY])
        r = self.client.post("/proxies/check", headers=self.headers, json={})
        self.assertEqual(r.status_code, 403)

    def test_an_account_without_access_cannot_check(self):
        server.proxy_list.replace([PROXY])
        second = TestClient(server.app, client=("127.0.0.1", 50001))
        try:
            session = self.signup("nobody", second)
            self.assertFalse(session["user"]["access"])
            r = second.post("/proxies/check", headers=self.csrf(session), json={})
            self.assertEqual(r.status_code, 402, r.text)
            self.assertEqual(r.json()["detail"], "Trial ended. Choose a plan to continue.")
        finally:
            second.close()

    def test_a_stored_list_is_reported_line_by_line(self):
        self.store([PROXY, "socks5://198.51.100.8:1080"])
        events = self.lines(self.check())
        self.assertEqual(events[0]["type"], "start")
        self.assertEqual(events[0]["total"], 2)
        self.assertEqual(events[0]["source"], "stored")
        self.assertEqual(events[0]["target"], server.PROXY_CHECK_URL)
        results = [e for e in events if e["type"] == "result"]
        self.assertEqual(len(results), 2)
        self.assertEqual({r["label"] for r in results}, {"answered"})
        self.assertEqual({r["exit_ip"] for r in results}, {"203.0.113.5"})
        self.assertEqual([r["checked"] for r in results], [1, 2])
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual((done["total"], done["alive"], done["dead"]), (2, 2, 0))
        self.assertEqual(server._checks_in_flight, {}, "the check must release its slot")

    def test_a_paste_can_be_checked_without_being_stored(self):
        self.store([PROXY])
        # "not a proxy" splits into three rejected tokens, which is the parser's
        # own count and the number the page shows.
        events = self.lines(self.check({"text": "203.0.113.44:9000\nnot a proxy"}))
        self.assertEqual(events[0]["source"], "paste")
        self.assertEqual(events[0]["total"], 1)
        self.assertEqual(events[0]["rejected"], 3)
        self.assertEqual(self.client.get("/proxies").json()["proxies"], [PROXY])

    def test_the_cap_reports_what_it_left_out(self):
        self.store([f"198.51.100.{n}:8080" for n in range(1, 6)])
        events = self.lines(self.check({"limit": 2}))
        self.assertEqual(events[0]["total"], 2)
        self.assertEqual(events[0]["skipped"], 3)

    def test_an_unreachable_target_refuses_instead_of_blaming_the_list(self):
        self.store([PROXY])
        r = self.check(target=(False, "ConnectError: Name or service not known"))
        self.assertEqual(r.status_code, 503)
        self.assertIn("not answering from this host", r.json()["detail"])
        self.assertIn("nothing is wrong with them", r.json()["detail"])
        self.assertEqual(server._checks_in_flight, {})

    def test_an_empty_stored_list_says_so(self):
        r = self.check()
        self.assertEqual(r.status_code, 503)
        self.assertIn("no proxies saved yet", r.json()["detail"])

    def test_a_second_check_is_refused_while_one_is_running(self):
        self.store([PROXY])
        server._checks_in_flight[self.session["user"]["id"]] = time.time()
        r = self.check()
        self.assertEqual(r.status_code, 409)
        self.assertIn("already running", r.json()["detail"])

    def test_the_summary_counts_each_kind_of_failure(self):
        self.store(["http://a:1", "http://b:2", "http://c:3"])
        kinds = {"http://a:1": proxycheck.STATUS_ALIVE,
                 "http://b:2": proxycheck.STATUS_AUTH,
                 "http://c:3": proxycheck.STATUS_BLOCKED}

        async def probe(proxy, **kwargs):
            status = kinds[proxy]
            return proxycheck.Probe(proxy, status, 30,
                                    http=200 if status == proxycheck.STATUS_ALIVE else 407,
                                    detail="detail")

        events = self.lines(self.check(probe=probe))
        done = events[-1]
        self.assertEqual(done["alive"], 1)
        self.assertEqual(done["dead"], 1, "only the exit's own failure counts as dead")
        self.assertEqual(done["counts"][proxycheck.STATUS_BLOCKED], 1)
        self.assertEqual(done["labels"][proxycheck.STATUS_AUTH], "credentials rejected")

    def test_prune_removes_only_what_is_in_the_list(self):
        keep, drop = "http://keep:1", "http://drop:2"
        self.store([keep, drop])
        r = self.client.post("/proxies/prune", headers=self.csrf(),
                             json={"proxies": [drop, "http://never-was-here:3", drop]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["count"], 1)
        self.assertEqual(r.json()["removed"], [drop])
        self.assertEqual(self.client.get("/proxies").json()["proxies"], [keep])

    def test_prune_without_a_token_is_refused(self):
        self.store([PROXY])
        self.assertEqual(self.client.post("/proxies/prune", headers=self.headers,
                                          json={"proxies": [PROXY]}).status_code, 403)
        self.assertEqual(self.client.get("/proxies").json()["proxies"], [PROXY])

    def test_the_endpoints_are_listed_where_the_docs_page_reads_them(self):
        endpoints = self.client.get("/info").json()["endpoints"]
        self.assertIn("POST /proxies/check", endpoints)
        self.assertIn("POST /proxies/prune", endpoints)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
