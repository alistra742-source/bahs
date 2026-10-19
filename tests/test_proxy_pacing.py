"""What a run does when the endpoint will not open every tunnel at once.

The failure this file exists for, straight off the platform:

    2026-10-15 22:19:28,163 bahs.server: scan run 6e9b43fcf01b starting:
      456976 names x 1 platform(s) [guns.lol], concurrency 256, retries 1,
      pool 1 of 1 usable at 128 connection(s) each
    2026-10-15 22:19:30,514 bahs.sniper: check guns.lol/vlej -> error
      (ConnectTimeout: no answer within the timeout, asking guns.lol)
    2026-10-15 22:19:30,529 WARNING bahs.sniper: proxy
      http://***@global.nullproxies.com:8080 retired for this run:
      16 attempts, 16 transport failures, 0 platform refusals; 0 of 1 proxies left

128 checks went out at once, every one of them timed out opening its tunnel at
the scan's 2s ceiling, a settled sample of 16 retired the only entry in the
list, and the run ended at name 128 of 456,976 saying the list was dead. The
list was not dead -- it was being asked for more simultaneous tunnels than it
accepted, and nothing in the run could tell that apart from a corpse.

Two halves are tested here, and they are the two halves of the fix:

* **the pool** paces the width instead of writing the endpoint off, and writes
  it off only after asking for a single tunnel at a time and still getting
  nothing;
* **the scheduler** follows that width down, so the run keeps producing
  verdicts through an endpoint that answers a burst with silence.

The second half runs the real scheduler (``iter_snipes``) over an endpoint
modelled in-process, because the silence has to happen in the *connect* phase
and a loopback listener cannot produce that: measured on this machine, an HTTP
proxy on 127.0.0.1 that accepts and never answers comes back a **ReadTimeout**
with ``fault=target`` -- which the pool reads as the target being slow, not the
endpoint being saturated. The real run through a real endpoint produced
``ConnectTimeout``, so that is what the model raises, at exactly the width it
was asked at. The socket-level truth is tested where a socket can show it:
``RealConnectTimeoutTests`` gets one out of the IP stack.

The scan connect default is deliberately longer than the old 2s deadline: the
reported failures all landed at exactly that cutoff, and a proxy tunnel includes
the HTTPS handshake before the target request can proceed.
"""

import asyncio
import json
import unittest
from collections import Counter

import httpx

import server
import sniper

DEAD = "http://10.255.255.1:8080"          # blackholed: the TCP connect never lands
PROXY = "http://10.255.255.2:8080"
OTHER = "http://10.255.255.3:8080"


class RealConnectTimeoutTests(unittest.TestCase):
    """A connect phase that never lands, from the real IP stack.

    ``10.255.255.1`` is blackholed, so the TCP connect to it hangs and httpx
    gives up with the same exception a saturated endpoint produced. This is the
    classification everything else in this file keys on, checked against
    something no mock is involved in.
    """

    def test_scan_timeout_defaults_leave_time_for_a_slow_proxy_handshake(self):
        # Scan defaults must not expire before proxy handshake latency routinely
        # seen on global/rotating endpoints; per-scan overrides remain possible.
        self.assertEqual(server.SCAN_CONNECT_TIMEOUT, 10.0)
        self.assertEqual(server.SCAN_READ_TIMEOUT, 8.0)
        scan_pool = server._pool_kwargs(True, 32, [PROXY], "list")
        self.assertEqual(scan_pool["timeout"].connect, 10.0)
        self.assertEqual(scan_pool["timeout"].read, 8.0)

    def test_a_blackholed_endpoint_is_a_proxy_fault_and_a_timeout(self):
        async def go():
            client = httpx.AsyncClient(
                proxy=DEAD,
                timeout=httpx.Timeout(connect=0.3, read=0.3, write=0.3, pool=0.3),
                verify=sniper.SSL_CONTEXT,
            )
            try:
                await client.get("https://guns.lol/brew")
            except Exception as exc:
                return exc
            finally:
                await client.aclose()
            return None

        exc = asyncio.run(go())
        self.assertIsNotNone(exc, "a blackholed proxy cannot have answered")
        self.assertTrue(sniper.proxy_fault(exc), type(exc).__name__)
        result = sniper.transport_error("brew", "guns.lol", exc)
        self.assertEqual(result.fault, sniper.FAULT_PROXY)
        self.assertTrue(result.timed_out, "a connect that never landed is a timeout, not a refusal")
        self.assertEqual(sniper.outcome_for(result), sniper.OUTCOME_FAILED)


class EndpointPacingTests(unittest.TestCase):
    """A burst of connect timeouts narrows the endpoint instead of retiring it."""

    def pool(self, per_proxy: int = 128, proxies=(PROXY,), **kwargs) -> sniper.ProxyPool:
        # The short rest a failure buys an endpoint is not what these tests are
        # about, and the default 60 seconds would keep the rotation off the very
        # endpoint a test is feeding.
        kwargs.setdefault("cooldown", 0.0)
        return sniper.ProxyPool(list(proxies), per_proxy=per_proxy, **kwargs)

    def probe(self, pool, outcome=sniper.OUTCOME_FAILED, timed_out=True, proxy=PROXY, **kwargs):
        """One check through this endpoint: offered, then reported.

        Every report in a real run follows its own hand-out -- the window the
        pacer judges is counted in offers -- so the test hands out exactly as
        many as it reports.
        """
        for _ in range(max(1, len(pool))):
            handed = pool.next()
            if handed is None or handed == proxy:
                pool.report(proxy, outcome, "guns.lol", timed_out=timed_out, **kwargs)
                return
            pool.abandon(handed)
        # The rotation would not offer it at all -- it has been written off, or
        # it is resting -- and requests already out still report, so the report
        # lands anyway. That is part of what these tests assert.
        pool.report(proxy, outcome, "guns.lol", timed_out=timed_out, **kwargs)

    def probes(self, pool, count, **kwargs):
        for _ in range(count):
            self.probe(pool, **kwargs)

    def test_a_saturated_endpoint_is_paced_and_not_retired(self):
        pool = self.pool()
        self.probes(pool, 4)
        self.assertEqual(pool.retired(), 0, "silence at load is not evidence the list is dead")
        self.assertEqual(pool.alive(), 1)
        self.assertEqual(pool.width(PROXY), 64, "the width is the thing that gives")
        self.assertEqual(pool.paced_endpoints(), 1)
        self.assertEqual(pool.capacity(), 64)

    def test_target_timeouts_do_not_reduce_the_proxy_tunnel_width(self):
        pool = self.pool(per_proxy=16)
        self.probes(pool, 32, outcome=sniper.OUTCOME_TIMEOUT, timed_out=True)
        self.assertEqual(pool.width(PROXY), 16)
        self.assertEqual(pool.timed_out(), 0)
        self.assertEqual(pool.failures(), 0)
        self.assertEqual(pool.retired(), 0)

    def test_the_same_failures_that_were_refusals_still_retire_it(self):
        # The counterfactual, and the old behaviour: a proxy that *refused*
        # every request (nothing to do with how many it was asked for) is still
        # written off, which is what a run of 16 ConnectTimeouts used to do.
        pool = self.pool()
        self.probes(pool, 16, timed_out=False)
        self.assertEqual(pool.retired(), 1)

    def test_the_width_is_halved_until_the_failures_stop(self):
        pool = self.pool()
        widths = []
        for _ in range(8):
            self.probes(pool, 4)
            widths.append(pool.width(PROXY))
        self.assertEqual(widths[:7], [64, 32, 16, 8, 4, 2, 1])
        self.assertEqual(widths[7], 1, "the width never reaches zero")

    def test_the_width_grows_back_after_clean_windows(self):
        pool = self.pool()
        self.probes(pool, 4)
        self.assertEqual(pool.width(PROXY), 64)
        # Answers now: an answer, however unwelcome, is a tunnel that opened.
        # One clean window is not enough -- the width that was just cut is the
        # width that was just shown to be too wide -- so 3 of them are, and then
        # it grows by one tunnel at a time. A clean window is 16 offers.
        self.probes(pool, 32, outcome=sniper.OUTCOME_TIMEOUT, timed_out=False)
        self.assertEqual(pool.width(PROXY), 64)
        self.probes(pool, 16, outcome=sniper.OUTCOME_TIMEOUT, timed_out=False)
        self.assertEqual(pool.width(PROXY), 65)
        self.probes(pool, 3 * 16, outcome=sniper.OUTCOME_OK, timed_out=False)
        self.assertEqual(pool.width(PROXY), 66)

    def test_a_couple_of_lost_tunnels_take_one_tunnel_off(self):
        # A window with a few timeouts in it is the endpoint at its ceiling:
        # one tunnel comes off rather than half the width, which is what a
        # majority timing out means.
        pool = self.pool(per_proxy=16)
        self.probes(pool, 2 * 16, outcome=sniper.OUTCOME_OK, timed_out=False)
        self.assertEqual(pool.width(PROXY), 16, "a clean endpoint is left alone")
        self.probes(pool, 14, outcome=sniper.OUTCOME_OK, timed_out=False)
        self.probes(pool, 2)
        self.assertEqual(pool.width(PROXY), 15)

    def test_four_refusals_in_a_row_cut_the_width_there_and_then(self):
        # The window exists to average out bad luck; four tunnels in a row that
        # never opened is not bad luck, and waiting for a dozen more offers to
        # confirm it spends names to learn what is already known.
        pool = self.pool()
        self.probes(pool, 4)
        self.assertEqual(pool.width(PROXY), 64)

    def test_three_refusals_are_not_enough_to_jump_to_a_conclusion(self):
        pool = self.pool()
        self.probes(pool, 3)
        self.assertEqual(pool.width(PROXY), 128, "three is not four")

    def test_a_healthy_endpoint_with_occasional_losses_keeps_its_width(self):
        # 2% of tunnels timing out is a working endpoint on a bad exit, not a
        # saturated one: over 40 windows it must not be paced into the floor.
        pool = self.pool(per_proxy=64)
        for window in range(40):
            self.probes(pool, 15, outcome=sniper.OUTCOME_OK, timed_out=False)
            self.probe(pool, sniper.OUTCOME_OK, timed_out=window % 7 == 0)
        self.assertGreater(pool.width(PROXY), 40, pool.width(PROXY))

    def test_a_refusal_counts_as_a_tunnel_that_opened(self):
        # A refused request is answered -- the tunnel worked and the platform
        # said no -- so it is a clean window for the width and the width grows
        # back. It must not be read as a *failure* of the endpoint, which is the
        # whole reason refusals have their own record.
        pool = self.pool()
        self.probes(pool, 4)
        self.assertEqual(pool.width(PROXY), 64)
        self.probes(pool, 3 * 16, outcome=sniper.OUTCOME_BLOCKED, timed_out=False)
        self.assertEqual(pool.width(PROXY), 65)
        self.assertEqual(pool.retired(), 0)

    def test_a_retry_is_not_new_demand(self):
        # A retry is a second arrival *caused by* the first attempt failing.
        # Counting it halves the width for the run's own retry rather than for
        # anything the endpoint said -- measured against an endpoint that
        # accepts 4 tunnels, counting the retries made it settle at 2.
        pool = self.pool()
        self.probes(pool, 4)
        self.assertEqual(pool.width(PROXY), 64)
        self.probes(pool, 32, retry=True)
        self.assertEqual(pool.width(PROXY), 64, "the retries said nothing new")

    def test_at_one_tunnel_a_silent_endpoint_is_written_off(self):
        # The floor is what separates "asked for too much" from "not there": a
        # single tunnel open and still nothing back is a dead endpoint, and the
        # run has to be able to say so rather than retrying it forever. The
        # sample is taken *at* the floor -- the attempts made while it was being
        # asked for 128 tunnels say nothing about whether it can carry one.
        pool = self.pool()
        self.probes(pool, 7 * 4)
        self.assertEqual(pool.width(PROXY), 1)
        self.assertEqual(pool.retired(), 0, "not yet -- it has to be tried at the floor first")
        self.probes(pool, 6)
        self.assertEqual(pool.retired(), 1)
        self.assertEqual(pool.alive(), 0)

    def test_the_stop_reason_names_the_tunnels_not_a_dead_list(self):
        pool = self.pool()
        self.probes(pool, 7 * 4)
        self.probes(pool, 16)
        reason = pool.exhausted_reason()
        self.assertIn("connections timed out", reason)
        self.assertIn("one tunnel at a time", reason)
        self.assertIn("lower the concurrency", reason)
        self.assertNotIn("the list is dead", reason)

    def test_capacity_follows_the_width(self):
        pool = self.pool(per_proxy=16, proxies=(PROXY, OTHER))
        self.assertEqual(pool.capacity(), 32)
        self.probes(pool, 4)
        self.assertEqual(pool.width(PROXY), 8)
        self.assertEqual(pool.capacity(), 16 + 8, "a cut endpoint contributes its cut width")
        self.assertEqual(pool.paced_endpoints(), 1)

    def test_a_retired_endpoint_stops_contributing_a_cut(self):
        pool = self.pool(per_proxy=16, proxies=(PROXY, OTHER))
        # Down to the floor: 16 -> 8 -> 4 -> 2 -> 1, one cut per four refusals.
        self.probes(pool, 4 * 4)
        self.assertEqual(pool.width(PROXY), 1)
        self.probes(pool, 6)
        self.assertEqual(pool.retired(), 1)
        # Its override goes with it -- otherwise capacity() subtracts a cut
        # belonging to an endpoint that no longer contributes a full width --
        # and the reports still in flight behind it do not write a new one.
        self.assertEqual(pool.paced_endpoints(), 0)
        self.assertNotIn(PROXY, pool._width)
        self.assertEqual(pool.capacity(), pool.alive() * pool.per_proxy)

    def test_the_tunnels_open_at_the_old_width_are_not_held_against_the_new_one(self):
        # The burst dispatched at the abandoned width is still in flight, and
        # every one of those checks is already doomed. Counting their failures
        # against the new width drives it down again and again on stale news.
        pool = self.pool(per_proxy=64)
        for _ in range(20):         # offered at 64, and still out
            pool.next()
        for _ in range(4):          # the first four report, and the trip fires
            pool.report(PROXY, sniper.OUTCOME_FAILED, "guns.lol", timed_out=True)
        self.assertEqual(pool.width(PROXY), 32)
        # The sixteen still out were offered at 64, and every one of them was
        # doomed when that decision was taken. When they come back they say
        # nothing about 32.
        for _ in range(16):
            pool.report(PROXY, sniper.OUTCOME_FAILED, "guns.lol", timed_out=True)
        self.assertEqual(pool.width(PROXY), 32, "stale failures do not cut it again")

    def test_rotation_moves_off_a_cut_endpoint(self):
        # With two endpoints and one of them cut, the checks go to the one that
        # will still take them rather than queueing on the one that will not.
        # The scheduler hands out a whole window before anything reports, which
        # is the moment the width has to bite.
        pool = self.pool(per_proxy=4, proxies=(PROXY, OTHER))
        self.probes(pool, 4)
        self.assertEqual(pool.width(PROXY), 2)
        self.assertEqual(pool.capacity(), 2 + 4, "the pool will open six tunnels, not eight")
        handed = [pool.next() for _ in range(pool.capacity())]
        self.assertEqual(handed.count(PROXY), 2, handed)
        self.assertEqual(handed.count(OTHER), 4, handed)

    def test_a_pool_with_nothing_to_replace_it_keeps_working(self):
        # The Tor fallback: nothing can be retired, so the width is the only
        # lever there is and it still has to work.
        pool = self.pool(per_proxy=32, retire=False)
        self.probes(pool, 5 * 4)
        self.assertEqual(pool.alive(), 1)
        self.assertEqual(pool.width(PROXY), 1)
        self.assertEqual(pool.paced_endpoints(), 1)


class RunReportTests(unittest.TestCase):
    """What the dashboard is told about a paced endpoint -- and never told.

    The pacing exists so a saturated endpoint is not retired; that is invisible
    unless the run says so, and a run that merely looks slow is exactly how this
    started. The endpoint itself is still never named: a payload a browser
    receives has no business carrying a proxy URL, which usually holds a
    password.
    """

    CREDENTIALED = "http://user:hunter2@10.255.255.9:8080"

    def paced(self) -> sniper.ProxyPool:
        pool = sniper.ProxyPool([self.CREDENTIALED], per_proxy=16, cooldown=0.0)
        for _ in range(4):
            pool.next()
            pool.report(self.CREDENTIALED, sniper.OUTCOME_FAILED, "guns.lol", timed_out=True)
        return pool

    def test_the_run_report_carries_the_pacing_without_naming_the_endpoint(self):
        pool = self.paced()
        report = server._pool_report(pool)
        self.assertEqual(report["proxies_paced"], 1)
        self.assertEqual(report["proxy_capacity"], 8)
        self.assertEqual(report["proxy_timeouts"], 4)
        self.assertEqual(report["pool_retired"], 0)
        adjustments = report["proxy_pace_adjustments"]
        self.assertEqual([(a["was"], a["now"]) for a in adjustments], [(16, 8)])
        blob = json.dumps(report)
        self.assertNotIn("10.255.255.9", blob, "an endpoint that is not offered is not reported")
        self.assertNotIn("hunter2", blob)

    def test_the_live_tick_carries_it_too(self):
        pool = self.paced()
        pool.pace = None            # the tick asks for the platform pacer
        tally = server._Tally(10, ["guns.lol"], None, pool)
        progress = tally.progress()
        self.assertEqual(progress["proxies_paced"], 1)
        self.assertEqual(progress["proxy_capacity"], 8)
        self.assertEqual(progress["proxy_timeouts"], 4)
        self.assertNotIn("10.255.255.9", json.dumps(progress))


class SaturatingEndpoint:
    """An endpoint that answers at most ``capacity`` tunnels at once.

    Past that it does nothing at all -- no refusal, no reset, no answer -- which
    is the whole problem: from one request, that is indistinguishable from a
    host that is gone. A refused tunnel is held until the caller gives up, as
    the reported run's were: every one of its 128 attempts paid the full 2s
    connect timeout.
    """

    def __init__(self, capacity: int, refusal_cost: float = 0.02) -> None:
        self.capacity = capacity
        self.refusal_cost = refusal_cost
        self.open = 0
        self.peak = 0
        self.refused = 0

    def open_tunnel(self) -> bool:
        if self.open >= self.capacity:
            self.refused += 1
            return False
        self.open += 1
        self.peak = max(self.peak, self.open)
        return True

    def close_tunnel(self) -> None:
        self.open -= 1


class SaturatedRunTests(unittest.TestCase):
    """A real run through it, over the real scheduler."""

    def setUp(self):
        self.saved = sniper.CHECKERS.get("lab")
        self.addCleanup(self.restore)
        self.endpoint = None

    def restore(self):
        if self.saved is None:
            sniper.CHECKERS.pop("lab", None)
        else:
            sniper.CHECKERS["lab"] = self.saved

    def run_it(self, names=160, capacity=8, concurrency=16, per_proxy=16, retries=1):
        self.endpoint = SaturatingEndpoint(capacity)
        endpoint = self.endpoint

        async def check(client, username):
            if not endpoint.open_tunnel():
                # Nothing took the connection, and the caller waits out the
                # connect timeout before it learns that -- 2s in the reported
                # run, 20ms here so the test finishes.
                await asyncio.sleep(endpoint.refusal_cost)
                return sniper.transport_error(
                    username,
                    "lab",
                    httpx.ConnectTimeout(
                        "",
                        request=httpx.Request("GET", "https://guns.lol/" + username),
                    ),
                )
            try:
                await asyncio.sleep(0.01)   # a check that answers
                return sniper.SnipeResult(username, "lab", "available", "free")
            finally:
                endpoint.close_tunnel()

        sniper.CHECKERS["lab"] = check
        pool = sniper.ProxyPool([PROXY], per_proxy=per_proxy)

        async def go():
            out = []
            async for result in sniper.iter_snipes(
                [f"name{index}" for index in range(names)],
                ["lab"],
                pool,
                concurrency=concurrency,
                retries=retries,
            ):
                out.append((result, pool.width(PROXY)))
            return out

        collected = asyncio.run(go())
        asyncio.run(pool.aclose())
        return [r for r, _ in collected], [w for _, w in collected], pool

    def test_the_run_keeps_producing_verdicts_and_does_not_write_the_endpoint_off(self):
        results, widths, pool = self.run_it()
        statuses = [r.status for r in results]
        errors = statuses.count("error")
        available = statuses.count("available")
        self.assertEqual(len(results), 160, "every name is accounted for, one way or another")
        # The old behaviour, exactly: the endpoint was retired at the 16th
        # settled timeout and the run ended with a wall of errors.
        self.assertEqual(pool.retired(), 0)
        self.assertEqual(pool.alive(), 1)
        self.assertEqual(pool.stop_reason, "")
        # The width came down to what the endpoint will actually carry...
        self.assertEqual(widths[-1], 8)
        self.assertLessEqual(self.endpoint.peak, 8, "never more tunnels at once than it accepts")
        # ...and finding it costs a burst of checks, not the run. Those are the
        # names whose tunnel never opened: the run paces, they are lost, and
        # everything after the width settles is a verdict.
        self.assertLessEqual(errors, 24, f"{errors}/160 lost while finding the width")
        self.assertGreaterEqual(available, 136, f"{available}/160 resolved")
        self.assertGreaterEqual(pool.timed_out(), errors, "a retried name times out twice")
        # What is left is the occasional probe: the pacer grows the width back
        # one tunnel at a time to find out whether the endpoint has recovered,
        # and a probe that is refused costs the name it was spent on. One in a
        # hundred, not one in five.
        settled = statuses[40:]
        self.assertLessEqual(
            settled.count("error"), len(settled) // 25 + 1,
            f"{settled.count('error')} errors in the last {len(settled)}",
        )

    def test_a_dead_endpoint_still_ends_the_run_honestly(self):
        # Capacity zero: it never answers a single tunnel, at any width. The run
        # must end, and say the endpoint is the problem rather than pretending
        # the names were checked.
        results, _widths, pool = self.run_it(names=200, capacity=0, retries=0)
        self.assertEqual(pool.retired(), 1)
        self.assertEqual(pool.alive(), 0)
        self.assertIn("connections timed out", pool.exhausted_reason())
        statuses = [r.status for r in results]
        self.assertEqual(statuses.count("available"), 0)
        self.assertLess(len(results), 200, "the run stops rather than filing an error per name")

    def test_a_dead_transport_gives_up_in_a_bounded_number_of_names(self):
        # The same dead endpoint at the settings a deployment actually ships
        # (SNIPE_PER_PROXY_MAX=128, SCAN_CONCURRENCY=256, SCAN_RETRIES=1), because
        # the descent cost is a function of the width it starts at and the test
        # above starts twenty times narrower. Measured on the reported run this
        # is 259 names: the pool walks 128 -> 64 -> ... -> 1, and the endpoint is
        # retired once a single tunnel still gets nothing. The bound is what
        # matters -- a run that stopped admitting it would walk a 456,976-name
        # sweep, which is indistinguishable to the user from a hung one.
        results, widths, pool = self.run_it(
            names=2000, capacity=0, concurrency=256, per_proxy=128, retries=1
        )
        # A name burned is an attempt that errored, so the bound is over
        # *attempts*, not over rows. The run also ends with one `waiting` line --
        # the scheduler saying it has nothing left to dispatch while it winds
        # down -- and counting that as a lost name would make the bound drift
        # with a scheduling detail instead of with work lost.
        statuses = Counter(r.status for r in results)
        attempts = statuses["error"]
        self.assertEqual(statuses.get("available", 0), 0)
        self.assertEqual(statuses.get("taken", 0), 0)
        self.assertEqual(statuses.get("unanswered", 0), 0,
                         "a transport timeout is never filed as a missing answer")
        self.assertEqual(sum(statuses.values()) - attempts,
                         statuses.get("waiting", 0) + statuses.get("heartbeat", 0),
                         "every row that is not a waiting/heartbeat line is a failure")
        self.assertEqual(sorted(set(widths)), [1, 2, 4, 8, 16, 32, 64, 128])
        self.assertEqual(pool.retired(), 1)
        self.assertEqual(pool.alive(), 0)
        self.assertLess(attempts, 400, f"{attempts} names burned before giving up")
        # And it says which lever to move, rather than "the list is dead".
        self.assertIn("connections timed out", pool.stop_reason)
        self.assertIn("raise the connect timeout", pool.stop_reason)

    def test_an_endpoint_that_needs_longer_to_open_is_not_called_dead(self):
        # Every tunnel timed out at the configured connect ceiling, even at one
        # tunnel at a time. A tunnel that needs more time is not a dead endpoint;
        # the pool should retry once with a longer ceiling before deciding.
        seen: list[float] = []

        async def check(client, username):
            ceiling = client.timeout.connect
            seen.append(ceiling)
            if ceiling < 4.0:
                await asyncio.sleep(0.01)
                return sniper.transport_error(
                    username, "lab",
                    httpx.ConnectTimeout(
                        "", request=httpx.Request("GET", "https://guns.lol/" + username)
                    ),
                )
            return sniper.SnipeResult(username, "lab", "available", "free")

        sniper.CHECKERS["lab"] = check
        # Width at the floor from the start, so the only lever left is the clock.
        pool = sniper.ProxyPool(
            [PROXY], per_proxy=1,
            timeout=httpx.Timeout(connect=1.0, read=2.0, write=2.0, pool=2.0),
        )

        async def go():
            out = []
            async for result in sniper.iter_snipes(
                [f"name{index}" for index in range(40)], ["lab"], pool,
                concurrency=4, retries=0,
            ):
                out.append(result.status)
            return out

        statuses = asyncio.run(go())
        asyncio.run(pool.aclose())
        self.assertEqual(pool.retired(), 0, "a slow tunnel is not a dead endpoint")
        self.assertEqual(pool.raised_ceilings(), 1, "exactly one retry was spent")
        self.assertGreaterEqual(pool.connect_ceiling(PROXY), 4.0)
        self.assertEqual(seen[0], 1.0, "the tight ceiling is tried first, not skipped")
        self.assertGreater(max(seen), 1.0, "and then a longer one is actually used")
        self.assertGreater(statuses.count("available"), 30, statuses)

    def test_a_dead_endpoint_pays_one_retry_for_the_ceiling_verdict(self):
        # The other half: when the endpoint is genuinely unreachable the retry must
        # not become an open-ended one. One escalation, then the verdict -- and the
        # stop reason has to say the ceiling was already ruled out, because telling
        # somebody to raise a timeout the run has already raised is worse than
        # saying nothing.
        _results, _widths, pool = self.run_it(
            names=2000, capacity=0, concurrency=256, per_proxy=128, retries=1
        )
        self.assertEqual(pool.raised_ceilings(), 1)
        self.assertEqual(pool.retired(), 1)
        self.assertEqual(pool.alive(), 0)
        self.assertIn("connect ceiling was already raised", pool.stop_reason)
        self.assertIn("cannot carry a request to this target", pool.stop_reason)

    def test_an_endpoint_that_can_take_the_load_is_never_touched(self):
        # The whole fix has to be invisible when the endpoint is fine: no
        # adjustments, no errors, and the width the run asked for.
        results, widths, pool = self.run_it(capacity=64)
        self.assertEqual([r.status for r in results], ["available"] * 160)
        self.assertEqual(pool.recent_endpoint_pace(), [])
        self.assertEqual(set(widths), {16})
        self.assertEqual(pool.paced_endpoints(), 0)


if __name__ == "__main__":
    unittest.main()
