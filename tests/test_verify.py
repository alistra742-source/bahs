"""The control-pair verdict, without talking to anything.

``verify.py`` exists to answer one question per platform -- can this check tell a
held name from a free one -- and the answer is only worth having if a broken
checker cannot be reported as a working one. So the classification is pinned
here against synthetic results, including the case that matters most: a held
control read as free must never come out ``ok``.
"""

import asyncio
import contextlib
import io
import unittest

import sniper
import verify


def result(username, platform, status, detail="", **kwargs):
    return sniper.SnipeResult(username, platform, status, detail, **kwargs)


class FreeProbeTests(unittest.TestCase):
    def test_every_platform_gets_a_probe_its_namespace_accepts(self):
        # A probe the platform would reject on shape comes back invalid, and an
        # invalid probe tests nothing about the checker.
        for platform in sniper.CHECKERS:
            name = verify.free_probe(platform, seed=7)
            self.assertTrue(sniper.NAME_RE.match(name), (platform, name))
            self.assertTrue(sniper.name_fits(platform, name), (platform, name))

    def test_a_seed_makes_a_run_repeatable_and_another_seed_does_not(self):
        for platform in sniper.CHECKERS:
            self.assertEqual(verify.free_probe(platform, seed=1),
                             verify.free_probe(platform, seed=1), platform)
            self.assertNotEqual(verify.free_probe(platform, seed=1),
                                verify.free_probe(platform, seed=2), platform)


class JudgeTests(unittest.TestCase):
    def test_a_working_checker_is_ok(self):
        verdict, _ = verify.judge(
            "github",
            result("github", "github", "taken", "github"),
            result("vxabc", "github", "available", "no such user"),
        )
        self.assertEqual(verdict, "ok")

    def test_a_banned_held_control_still_counts_as_held(self):
        verdict, _ = verify.judge(
            "guns.lol",
            result("held", "guns.lol", "banned", "account is banned"),
            result("vxabc", "guns.lol", "available", "claimable"),
        )
        self.assertEqual(verdict, "ok")

    def test_a_held_name_read_as_free_is_the_failure_that_matters(self):
        verdict, note = verify.judge(
            "x",
            result("x", "x", "available", "404"),
            result("vxabc", "x", "available", "404"),
        )
        self.assertEqual(verdict, "false-free")
        self.assertIn("cannot be registered", note)

    def test_a_free_name_read_as_held_is_reported_too(self):
        verdict, note = verify.judge(
            "myanimelist",
            result("held", "x", "taken", "x"),
            result("vxabc", "x", "taken", "live profile"),
        )
        self.assertEqual(verdict, "false-taken")
        self.assertIn("spent", note)

    def test_no_held_control_is_never_a_pass(self):
        verdict, _ = verify.judge(
            "guns.lol", None, result("vxabc", "guns.lol", "available", "claimable")
        )
        self.assertEqual(verdict, "unverified")
        self.assertEqual(verify.USABLE[0], "ok", "only `ok` is an unqualified pass")

    def test_a_transport_failure_is_not_a_verdict_about_the_name(self):
        failed, _ = verify.judge(
            "x", result("x", "x", "error", "ConnectTimeout", fault=sniper.FAULT_PROXY), None
        )
        self.assertEqual(failed, "failed")
        blocked, _ = verify.judge(
            "x", result("x", "x", "blocked", "status 403"), None
        )
        self.assertEqual(blocked, "blocked")

    def test_an_unrecognised_status_is_unreadable_rather_than_a_pass(self):
        verdict, _ = verify.judge(
            "x", result("x", "x", "waiting", ""), result("vxabc", "x", "available", "404")
        )
        self.assertEqual(verdict, "unreadable")

    def test_an_invalid_free_probe_proves_nothing(self):
        verdict, _ = verify.judge(
            "github", None, result("bad!", "github", "invalid", "rejected")
        )
        self.assertEqual(verdict, "unreadable")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    """The whole tool against a stub registry: no network, real code path."""

    def setUp(self):
        self.original = sniper.CHECKERS
        self.registry = {}
        sniper.CHECKERS = self.registry

    def tearDown(self):
        sniper.CHECKERS = self.original

    def checker(self, answers):
        async def check(client, username):
            return answers(username)
        return check

    async def test_a_healthy_platform_and_a_broken_one_are_told_apart(self):
        self.registry["good"] = self.checker(
            lambda u: result(u, "good", "taken", "live") if u == "held"
            else result(u, "good", "available", "free")
        )
        self.registry["bad"] = self.checker(
            lambda u: result(u, "bad", "available", "free")
        )
        rows = await verify.verify(["good", "bad"], None,
                                   {"good": "held", "bad": "held"}, 5.0, seed=3)
        self.assertEqual([r["platform"] for r in rows], ["good", "bad"])
        self.assertEqual(rows[0]["verdict"], "ok")
        self.assertEqual(rows[1]["verdict"], "false-free")

    async def test_a_checker_that_raises_is_a_finding_not_a_crash(self):
        async def boom(client, username):
            raise RuntimeError("endpoint moved")
        self.registry["boom"] = boom
        rows = await verify.verify(["boom"], None, {"boom": "held"}, 5.0, seed=3)
        self.assertEqual(rows[0]["verdict"], "failed")
        self.assertIn("endpoint moved", rows[0]["note"])

    async def test_an_override_replaces_the_built_in_control(self):
        seen = []

        async def note(client, username):
            seen.append(username)
            return result(username, "p", "available", "free")
        self.registry["p"] = note
        await verify.verify(["p"], None, {"p": "someone-i-know"}, 5.0, seed=3)
        self.assertEqual(seen[0], "someone-i-know",
                         "a held control passed on the command line must be the one asked")


class ReportTests(unittest.TestCase):
    def test_a_failure_is_named_in_the_report_and_sets_the_exit_code(self):
        row = {"platform": "x", "verdict": "false-free", "held": "x", "free": "vx1",
               "held_status": "available", "free_status": "available",
               "note": "a handle that exists read as available"}
        text = verify.render([row], None)
        self.assertIn("FALSE FREE", text)
        self.assertIn("a handle that exists read as available", text)
        self.assertIn("0 of 1", text)
        self.assertIn("about this host", text)

    def test_no_credential_can_reach_the_report(self):
        proxy = "http://user:hunter2@10.0.0.1:8080"
        text = verify.render([], proxy)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("user", text)
        self.assertIn("10.0.0.1", text, "the host is kept: it is what names the exit")

    def test_main_returns_non_zero_when_a_checker_is_broken(self):
        original = sniper.CHECKERS

        async def broken(client, username):
            return result(username, "bad", "available", "free")
        sniper.CHECKERS = {"bad": broken}
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = verify.main(["--platform", "bad", "--held", "bad=someheld",
                                    "--seed", "5"])
        finally:
            sniper.CHECKERS = original
        self.assertEqual(code, 1, "a checker that cannot see a held name fails the run")
        self.assertIn("FALSE FREE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
