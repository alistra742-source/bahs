import unittest

import sniper

PROXY = "http://10.255.255.1:8080"


class ProxyRetirementTests(unittest.TestCase):
    """A proxy that fails every transport is retired, even mid-flight.

    The pool used to decide retirement only on a *settled* sample -- nothing it
    was handed still in flight. Under the scheduler's sliding window a fresh
    check is dispatched the moment an old one settles, so with a wide run on a
    short list the in-flight count never reaches zero and a proxy failing every
    request was never retired: it stayed "alive" and the run filed an error for
    every remaining name instead of stopping and saying the list was dead.
    """

    def test_dead_proxy_is_retired_with_requests_still_in_flight(self):
        pool = sniper.ProxyPool([PROXY], fail_limit=6)
        for _ in range(20):
            pool.next()  # handed out and not yet reported: still in flight
        for _ in range(16):
            pool.report(PROXY, sniper.OUTCOME_FAILED, "github")
        self.assertEqual(pool.retired(), 1)
        self.assertEqual(pool.alive(), 0)

    def test_dead_proxy_still_retired_at_once_when_settled(self):
        pool = sniper.ProxyPool([PROXY], fail_limit=6)
        for _ in range(6):
            pool.next()
            pool.report(PROXY, sniper.OUTCOME_FAILED, "github")
        self.assertEqual(pool.retired(), 1)

    def test_fast_failure_race_alone_does_not_retire(self):
        # Six fast failures while the rest of a wide window is still out is the
        # race the sample length exists to rule out.
        pool = sniper.ProxyPool([PROXY], fail_limit=6)
        for _ in range(64):
            pool.next()
        for _ in range(6):
            pool.report(PROXY, sniper.OUTCOME_FAILED, "github")
        self.assertEqual(pool.retired(), 0)

    def test_working_proxy_is_kept(self):
        # Half the answers land: a failure rate well under the threshold keeps
        # a working endpoint in the rotation.
        pool = sniper.ProxyPool([PROXY], fail_limit=6)
        for _ in range(40):
            pool.next()
        for index in range(20):
            outcome = sniper.OUTCOME_OK if index % 2 else sniper.OUTCOME_FAILED
            pool.report(PROXY, outcome, "github")
        self.assertEqual(pool.retired(), 0)
        self.assertEqual(pool.alive(), 1)

    def test_timeouts_are_not_strikes(self):
        # A read timeout opened the tunnel and the target answered late: it is
        # not the proxy's failure and must never retire it.
        pool = sniper.ProxyPool([PROXY], fail_limit=6)
        for _ in range(40):
            pool.next()
        for _ in range(30):
            pool.report(PROXY, sniper.OUTCOME_TIMEOUT, "github")
        self.assertEqual(pool.retired(), 0)
        self.assertEqual(pool.attempts(), 0)

    def test_proxy_tunnel_handshake_failures_are_classified_as_proxy_faults(self):
        import asyncio
        import socket
        import struct
        import httpx

        async def run_cases():
            results = {}

            # Case 1: HTTP proxy accepts TCP then stalls before replying to CONNECT
            async def slow_connect_handler(r, w):
                await asyncio.sleep(2.0)
                w.close()

            s1 = await asyncio.start_server(slow_connect_handler, "127.0.0.1", 0)
            p1 = s1.sockets[0].getsockname()[1]

            # Case 2: HTTP proxy immediately drops connection on CONNECT
            async def drop_connect_handler(r, w):
                await r.read(128)
                w.close()

            s2 = await asyncio.start_server(drop_connect_handler, "127.0.0.1", 0)
            p2 = s2.sockets[0].getsockname()[1]

            # Case 3: SOCKS5 proxy accepts greeting then stalls on CONNECT
            async def socks_stall_handler(r, w):
                await r.read(64)
                w.write(b"\x05\x00")
                await w.drain()
                await asyncio.sleep(2.0)
                w.close()

            s3 = await asyncio.start_server(socks_stall_handler, "127.0.0.1", 0)
            p3 = s3.sockets[0].getsockname()[1]

            # Case 4: HTTP proxy forwards HTTP, then target stalls on response
            async def target_stall_handler(r, w):
                await r.read(256)
                await asyncio.sleep(2.0)
                w.close()

            s4 = await asyncio.start_server(target_stall_handler, "127.0.0.1", 0)
            p4 = s4.sockets[0].getsockname()[1]

            # Case 5: CONNECT succeeded, but the remote TLS handshake never answers.
            async def target_tls_stall_handler(r, w):
                await r.readline()
                while await r.readline() not in (b"\r\n", b"\n", b""):
                    pass
                w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await w.drain()
                await asyncio.sleep(2.0)
                w.close()

            s5 = await asyncio.start_server(target_tls_stall_handler, "127.0.0.1", 0)
            p5 = s5.sockets[0].getsockname()[1]

            t = httpx.Timeout(0.15)
            for name, url, target_url in [
                ("http_connect_timeout", f"http://127.0.0.1:{p1}", "https://example.com/"),
                ("http_connect_drop", f"http://127.0.0.1:{p2}", "https://example.com/"),
                ("socks5_connect_timeout", f"socks5://127.0.0.1:{p3}", "https://example.com/"),
                ("target_stall_after_connect", f"http://127.0.0.1:{p4}", "http://example.com/"),
                ("target_tls_stall_after_connect", f"http://127.0.0.1:{p5}", "https://example.com/"),
            ]:
                async with httpx.AsyncClient(proxy=url, timeout=t) as c:
                    try:
                        await c.get(target_url)
                    except Exception as exc:
                        results[name] = sniper.proxy_fault(exc)

            for s in (s1, s2, s3, s4, s5):
                s.close()
            await asyncio.gather(*(s.wait_closed() for s in (s1, s2, s3, s4, s5)))
            return results

        outcomes = asyncio.run(run_cases())
        self.assertTrue(outcomes["http_connect_timeout"])
        self.assertTrue(outcomes["http_connect_drop"])
        self.assertTrue(outcomes["socks5_connect_timeout"])
        # Target stalling after the tunnel was established is not a proxy fault,
        # including when the target TLS handshake consumes the connect timeout.
        self.assertFalse(outcomes["target_stall_after_connect"])
        self.assertFalse(outcomes["target_tls_stall_after_connect"])

    def test_dead_pool_stops_without_spraying_inflight_errors(self):
        import asyncio
        import httpx

        async def fake_dead_checker(client, username):
            await asyncio.sleep(0.01)
            return sniper.transport_error(
                username, "discord", httpx.ConnectError("connection refused")
            )

        saved = sniper.CHECKERS["discord"]
        sniper.CHECKERS["discord"] = fake_dead_checker
        try:
            async def run_sweep():
                pool = sniper.ProxyPool([PROXY], fail_limit=6, per_proxy=32)
                pool.begin_run()
                names = [f"user{i:03d}" for i in range(100)]
                results = [
                    r
                    async for r in sniper.iter_snipes(
                        names, ["discord"], pool, concurrency=32, retries=2
                    )
                ]
                await pool.aclose()
                return results, pool

            results, pool = asyncio.run(run_sweep())
            errors = [r for r in results if r.status == "error"]
            # Once the proxy reaches its retirement threshold (16 failures),
            # remaining in-flight checks are abandoned as misses rather than
            # emitted as error rows.
            self.assertEqual(pool.retired(), 1)
            self.assertLessEqual(len(errors), 16)
            self.assertTrue(pool.stop_reason)
        finally:
            sniper.CHECKERS["discord"] = saved

    def test_retired_proxy_hands_off_name_to_healthy_proxy(self):
        import asyncio
        import httpx

        good_proxy = "http://10.255.255.2:8080"
        pool = sniper.ProxyPool([PROXY, good_proxy], cooldown=0.0, fail_limit=6, per_proxy=16)
        pool.begin_run()

        async def fake_checker(client, username):
            await asyncio.sleep(0.005)
            if client is pool.client(PROXY):
                return sniper.transport_error(
                    username, "github", httpx.ConnectError("refused")
                )
            return sniper.SnipeResult(username, "github", "taken", username)

        saved = sniper.CHECKERS["github"]
        sniper.CHECKERS["github"] = fake_checker
        try:
            async def run_sweep():
                names = [f"user{i:03d}" for i in range(40)]
                results = [
                    r
                    async for r in sniper.iter_snipes(
                        names, ["github"], pool, concurrency=16, retries=0
                    )
                ]
                await pool.aclose()
                return results

            results = asyncio.run(run_sweep())
            self.assertEqual(pool.retired(), 1)
            self.assertEqual(pool.alive(), 1)
            # Even with retries=0, names that hit the retiring proxy are
            # re-dispatched to the surviving healthy proxy.
            taken = [r for r in results if r.status == "taken"]
            self.assertGreaterEqual(len(taken), 25)
        finally:
            sniper.CHECKERS["github"] = saved

    def test_guns_verdict_and_redirect_handling(self):
        import asyncio
        import httpx

        # Case-insensitive claim marker
        res = sniper._guns_verdict(
            "AbCdEf",
            b'<html><head><title>guns.lol</title></head><a href="/register?claim=abcdef">claim</a></html>',
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.status, "available")

        # Redirect away from /{username} (e.g. /pricing -> /premium) is invalid
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/pricing":
                return httpx.Response(307, headers={"Location": "https://guns.lol/premium"})
            return httpx.Response(200, text="<html><head><title>guns.lol — Premium</title></head></html>")

        async def run_guns():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
                return await sniper.check_guns(client, "pricing")

        redirected = asyncio.run(run_guns())
        self.assertEqual(redirected.status, "invalid")


if __name__ == "__main__":
    unittest.main()
