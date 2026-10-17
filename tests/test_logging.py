"""What a run writes to the log, and that it never writes a proxy password.

The point of the logging is that a run which is failing can be diagnosed from
the platform log alone: which exit carried each check, what the check ended as,
why it ended that way (including the exception's own message), and what the pool
decided about the list. A log that only counts failures is a number nobody can
act on, which is what a run of 128 checks with 128 errors and no reason looked
like from the outside.
"""

import asyncio
import logging
import unittest

import httpx

import server
import sniper

PROXY = "http://10.255.255.1:8080"
CREDENTIALED = "http://user:hunter2@10.255.255.1:8080"


class MaskProxyTests(unittest.TestCase):
    def test_credentials_are_replaced_and_the_endpoint_is_kept(self):
        self.assertEqual(
            sniper.mask_proxy(CREDENTIALED), "http://***@10.255.255.1:8080"
        )
        self.assertEqual(
            sniper.mask_proxy("socks5://u:p@1.2.3.4:1080"),
            "socks5://***@1.2.3.4:1080",
        )

    def test_scheme_less_and_open_proxies_are_left_alone(self):
        self.assertEqual(sniper.mask_proxy("1.2.3.4:8080"), "1.2.3.4:8080")
        self.assertEqual(sniper.mask_proxy("user:pass@1.2.3.4:8080"), "***@1.2.3.4:8080")

    def test_no_password_ever_survives_the_mask(self):
        self.assertNotIn("hunter2", sniper.mask_proxy(CREDENTIALED))


class TransportErrorTests(unittest.TestCase):
    def test_the_exception_message_rides_along_side_the_class_name(self):
        result = sniper.transport_error(
            "brew", "github", httpx.ConnectError("connection refused")
        )
        # What a row shows stays the class name...
        self.assertEqual(result.detail, "ConnectError")
        # ...and what the log shows carries the reason.
        self.assertIn("connection refused", result.cause)
        self.assertEqual(result.fault, sniper.FAULT_PROXY)

    def test_an_empty_timeout_message_still_reads_as_a_sentence(self):
        # httpcore raises ConnectTimeout('') whenever the tunnel never landed,
        # so the line a real run produced read "ConnectTimeout: " and named
        # nothing -- which is how a run of 128 failures said nothing at all.
        request = httpx.Request("POST", "https://discord.com/api/v9/x")
        result = sniper.transport_error(
            "brew", "discord", httpx.ConnectTimeout("", request=request)
        )
        self.assertEqual(result.detail, "ConnectTimeout")
        self.assertIn("ConnectTimeout: no answer within the timeout", result.cause)
        self.assertIn("asking discord.com", result.cause)
        self.assertFalse(result.cause.endswith(": "))
        self.assertEqual(result.fault, sniper.FAULT_PROXY)

    def test_a_timeout_with_no_request_still_names_something(self):
        result = sniper.transport_error("brew", "github", httpx.ReadTimeout(""))
        self.assertIn("ReadTimeout: no answer within the timeout", result.cause)

    def test_the_cause_is_never_sent_to_a_client(self):
        result = sniper.transport_error(
            "brew", "github", httpx.ConnectError("connection refused")
        )
        self.assertNotIn("cause", server._snipe_row(result))


class _CheckOnceLogging(unittest.TestCase):
    """Run one check through the pool and look at what it logged."""

    platform = "logplat"

    def setUp(self):
        self._saved = sniper.CHECKERS.get(self.platform)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            sniper.CHECKERS.pop(self.platform, None)
        else:
            sniper.CHECKERS[self.platform] = self._saved

    def run_check(self, result):
        async def checker(client, username):
            return result

        sniper.CHECKERS[self.platform] = checker

        async def go():
            pool = sniper.ProxyPool([CREDENTIALED])
            try:
                return await sniper.check_once(self.platform, "brew", pool)
            finally:
                await pool.aclose()

        return asyncio.run(go())


class CheckLineLoggingTests(_CheckOnceLogging):
    def test_a_failure_is_logged_at_info_with_its_reason_and_exit(self):
        result = sniper.SnipeResult("brew", self.platform, "blocked", "rate limited")
        with self.assertLogs("bahs.sniper", level="INFO") as captured:
            self.run_check(result)
        line = captured.output[0]
        self.assertTrue(line.startswith("INFO:bahs.sniper:"), line)
        for expected in (
            "check logplat/brew",
            "-> blocked",
            "rate limited",
            # The exit is named, and it is the masked one.
            "via http://***@10.255.255.1:8080",
        ):
            self.assertIn(expected, line)
        self.assertNotIn("hunter2", line)

    def test_a_connect_timeout_says_which_ceiling_it_hit(self):
        result = sniper.transport_error("brew", self.platform, httpx.ConnectTimeout(""))
        with self.assertLogs("bahs.sniper", level="INFO") as captured:
            self.run_check(result)
        line = captured.output[0]
        self.assertIn("ConnectTimeout: no answer within the timeout", line)
        self.assertIn("connect ceiling", line)
        self.assertIn("tunnel(s) open to this endpoint", line)

    def test_a_verdict_is_logged_but_not_at_info(self):
        result = sniper.SnipeResult("brew", self.platform, "taken")
        with self.assertLogs("bahs.sniper", level="DEBUG") as captured:
            self.run_check(result)
        lines = [r for r in captured.output if "-> taken" in r]
        self.assertEqual(len(lines), 1, captured.output)
        self.assertTrue(lines[0].startswith("DEBUG:bahs.sniper:"), lines[0])


class RetirementLoggingTests(unittest.TestCase):
    def test_a_retirement_says_which_exit_and_what_it_was_judged_on(self):
        pool = sniper.ProxyPool([CREDENTIALED], fail_limit=6)
        with self.assertLogs("bahs.sniper", level="WARNING") as captured:
            for _ in range(6):
                pool.next()
                pool.report(CREDENTIALED, sniper.OUTCOME_FAILED, "github")
        self.assertEqual(pool.retired(), 1)
        line = captured.output[0]
        self.assertTrue(line.startswith("WARNING:bahs.sniper:"), line)
        self.assertIn("proxy http://***@10.255.255.1:8080 retired", line)
        self.assertIn("6 attempts", line)
        self.assertIn("6 transport failures", line)
        self.assertIn("0 of 1 proxies left", line)
        self.assertNotIn("hunter2", line)

    def test_a_retirement_is_logged_once_however_many_reports_follow(self):
        # A wide window is still in flight when the proxy is written off, and
        # every one of those requests reports the same failure afterwards. The
        # state changes are idempotent; the log line has to be too, or one dead
        # endpoint reads as a list dying 40 times over.
        pool = sniper.ProxyPool([CREDENTIALED], fail_limit=6)
        for _ in range(40):
            pool.next()
        with self.assertLogs("bahs.sniper", level="WARNING") as captured:
            for _ in range(40):
                pool.report(CREDENTIALED, sniper.OUTCOME_FAILED, "github")
        lines = [line for line in captured.output if "retired" in line]
        self.assertEqual(len(lines), 1, captured.output)


class ServerLoggingTests(unittest.TestCase):
    def test_the_wire_is_only_clamped_when_the_level_is_not_debug(self):
        # The clamp exists because httpx logs a line per request; under debug it
        # is exactly what is being asked for, so the two must agree.
        if server._DEBUG:
            self.assertNotEqual(logging.getLogger("httpx").level, logging.WARNING)
        else:
            self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)

    def test_run_end_names_the_totals_and_the_stop_reason(self):
        pool = sniper.ProxyPool([PROXY])
        pool.begin_run()
        pool.stop_reason = "every proxy failed"
        tally = server._Tally(3, ["github"], None, pool)
        tally.add(sniper.SnipeResult("brew", "github", "error", "never arrived"))
        with self.assertLogs("bahs.server", level="INFO") as captured:
            server._log_run_end("scan", "run-1", tally, pool)
        line = captured.output[0]
        self.assertIn("scan run run-1 done", line)
        self.assertIn("1 checks", line)
        self.assertIn("1 errors", line)
        self.assertIn("stop_reason: every proxy failed", line)

    def test_run_start_does_not_echo_a_proxy_password(self):
        pool = sniper.ProxyPool([CREDENTIALED])
        pool.begin_run()
        with self.assertLogs("bahs.server", level="INFO") as captured:
            server._log_run_start("scan", "run-2", 10, ["github"], 8, 1, pool, "")
        line = captured.output[0]
        self.assertIn("scan run run-2 starting: 10 names", line)
        self.assertNotIn("hunter2", line)


if __name__ == "__main__":
    unittest.main()
