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


if __name__ == "__main__":
    unittest.main()
