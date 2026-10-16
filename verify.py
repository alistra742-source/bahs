"""Verify that a platform's check can actually produce a verdict.

A sniper is only as good as the signal under each checker, and a checker that has
silently rotted looks exactly like a good one on a run: every name comes back
"available" and the dashboard calls it a sweep. The failure that matters is not
loud, it is a false *free* -- a name the platform will not let you register,
reported as claimable, found after the run has spent your proxies on it.

So each checker is asked two questions it cannot answer by guessing, using
handles whose truth is known independently of the checker:

* a **held** control -- a handle that certainly exists on that platform. The
  checker must say taken/banned. If it says free, it is reporting real accounts
  as claimable, and nothing else it says can be trusted.
* a **free** control -- a random, well-formed handle that has never existed. The
  checker must say available. If it says taken, the checker is over-reporting
  held names, which is quieter but still wrong.

Both controls are read through the same code path a run uses
(``sniper.CHECKERS``), because the question is what the run would see, not what
the endpoint does when asked politely by hand.

    python verify.py                      # every platform, direct
    python verify.py --platform github --platform minecraft
    python verify.py --proxy http://user:pass@host:8080
    python verify.py --json

Direct from a datacenter host, most platforms answer 403/429 and the tool says
so: that is a real answer about *this host*, not a verdict about the name, and
the fix is a proxy, not a rewrite of the checker. Run it through the proxy list
you actually snipe with.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys

import httpx

import sniper

# A handle that certainly exists on each platform, so the checker has something
# it must recognise. Branded/own accounts on purpose: they are the handles least
# likely to be renamed or deleted out from under the test.
#
# These are a starting point, not gospel. A platform that renamed its own
# account, or a control that has since been deleted, turns into a *failed* line
# in the report rather than a silent pass -- and ``--held github=torvalds``
# replaces any entry without editing this file.
CONTROLS: dict[str, str] = {
    "discord": "discord",
    "instagram": "instagram",
    "tiktok": "tiktok",
    "x": "x",
    "youtube": "youtube",
    "facebook": "facebook",
    "reddit": "reddit",
    "telegram": "telegram",
    "github": "github",
    "roblox": "roblox",
    "minecraft": "notch",
    "steam": "gaben",
    "kick": "kick",
    "bluesky": "bsky.app",
    "huggingface": "huggingface",
    # guns.lol has no account anyone can vouch for from here, so it has no
    # default control: it is reported as unverified rather than guessed at.
    # Pass one with ``--held guns.lol=<a handle you know is taken>``.
}

# What "held" looks like, and what "free" looks like. Read from the checkers'
# own vocabulary so this file cannot drift into asserting a status no checker
# emits.
HELD = ("taken", "banned")
FREE = ("available",)

# A control that could not be answered is not a verdict about the name, and the
# two reasons a run distinguishes are kept apart here too.
UNANSWERED = ("blocked", "unanswered")
FAILED = ("error",)

ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def free_probe(platform: str, seed: int | None = None) -> str:
    """A well-formed handle that has never existed on ``platform``.

    Random per call, so two runs do not agree by accident, and fitted to the
    platform's own namespace first -- a probe the platform would reject on
    shape comes back ``invalid``, which tests nothing about the checker. The
    lengths are tried longest first because the shortest name-spaces are the
    strictest: X allows 15 characters and Roblox and Minecraft 16-20, so one
    fixed length would silently skip a quarter of the platforms.
    """
    rng = random.Random(seed)
    for length in (20, 18, 16, 15, 14, 12, 10, 8, 6, 5, 4, 3):
        for _ in range(20):
            name = "v" + "".join(rng.choice(ALPHABET) for _ in range(length - 1))
            if sniper.NAME_RE.match(name) and sniper.name_fits(platform, name):
                return name
    raise RuntimeError(f"no random probe fits the namespace of {platform}")


def judge(platform: str, held: sniper.SnipeResult | None,
          free: sniper.SnipeResult | None) -> tuple[str, str]:
    """``(verdict, note)`` for one platform's control pair.

    The verdict is about the *checker*, never about the names:

    ``ok``            both controls answered as the platform's truth requires.
    ``false-free``    the held control came back free. The worst case: the
                      checker would offer a name that is not claimable.
    ``false-taken``   the free control came back held. A name nobody holds is
                      reported as spent.
    ``blocked``       the platform refused this host on one of the controls.
    ``failed``        the transport failed on one of the controls.
    ``unreadable``    an answer arrived that was neither.
    ``unverified``    the checker answered, but no held control was supplied, so
                      nothing here proves it can recognise a held name.
    """
    for result in (held, free):
        if result is None:
            continue
        if result.status in FAILED:
            return "failed", f"{result.status} on {result.username}: {result.detail}"
        if result.status in UNANSWERED:
            return "blocked", f"{result.status} on {result.username}: {result.detail}"
        if result.status not in HELD + FREE + ("invalid",):
            return "unreadable", f"{result.username} -> {result.status}: {result.detail}"
    if free is not None and free.status == "invalid":
        return "unreadable", "the free probe was rejected on shape, so it proves nothing"
    if held is None:
        if free is not None and free.status in FREE:
            return "unverified", "the free control resolved; no held control was supplied"
        return "false-taken", f"a name nobody holds read as {free.status}"
    if held.status in FREE:
        return "false-free", (
            f"a handle that exists read as {held.status} ({held.detail}) -- "
            "the checker would offer a name that cannot be registered"
        )
    if free is not None and free.status in HELD:
        return "false-taken", (
            f"a random name nobody holds read as {free.status} ({free.detail}) -- "
            "the checker will report free names as spent"
        )
    return "ok", f"held -> {held.status} ({held.detail}); free -> available"


async def check_one(platform: str, client: httpx.AsyncClient,
                    held: str | None, seed: int) -> tuple[sniper.SnipeResult | None,
                                                         sniper.SnipeResult]:
    """Both controls, through the checker a run would use."""
    free_name = free_probe(platform, seed)
    checker = sniper.CHECKERS[platform]
    held_result = await checker(client, held) if held else None
    free_result = await checker(client, free_name)
    return held_result, free_result


async def verify(platforms: list[str], proxy: str | None, held_overrides: dict[str, str],
                 timeout: float, seed: int) -> list[dict]:
    """Every platform's control pair, in order. One at a time: two requests per
    platform against sixteen platforms is not a load problem, and sequential
    keeps the report readable next to the traffic it describes."""
    rows: list[dict] = []
    async with httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
        follow_redirects=True,
        verify=sniper.SSL_CONTEXT,
    ) as client:
        for platform in platforms:
            if platform not in sniper.CHECKERS:
                rows.append({"platform": platform, "verdict": "unknown",
                             "note": "not a platform this build checks", "held": None,
                             "free": None})
                continue
            held = held_overrides.get(platform) or CONTROLS.get(platform)
            try:
                held_result, free_result = await check_one(platform, client, held, seed)
            except Exception as exc:  # a checker raising is itself the finding
                rows.append({"platform": platform, "verdict": "failed",
                             "note": f"{type(exc).__name__}: {exc}",
                             "held": held, "free": None})
                continue
            verdict, note = judge(platform, held_result, free_result)
            rows.append({
                "platform": platform, "verdict": verdict, "note": note,
                "held": held, "free": free_result.username,
                "held_status": held_result.status if held_result else None,
                "free_status": free_result.status,
                "free_detail": free_result.detail,
            })
    return rows


# How each verdict reads in the report, and whether it means the checker is
# usable. Kept as one table so the summary line and the table cannot disagree.
VERDICT_LABEL = {
    "ok": "OK",
    "false-free": "FALSE FREE",
    "false-taken": "FALSE TAKEN",
    "blocked": "BLOCKED",
    "failed": "FAILED",
    "unreadable": "UNREADABLE",
    "unverified": "UNVERIFIED",
    "unknown": "UNKNOWN",
}
USABLE = ("ok", "unverified")


def render(rows: list[dict], proxy: str | None) -> str:
    width = max([len(r["platform"]) for r in rows] + [8])
    lines = [
        f"{'platform'.ljust(width)}  {'verdict'.ljust(11)}  held -> free",
        f"{'-' * width}  {'-' * 11}  {'-' * 40}",
    ]
    for row in rows:
        if row["held"] is None:
            controls = f"(no control) -> {row['free_status'] or '-'}"
        else:
            controls = f"{row['held_status'] or '-'} -> {row['free_status'] or '-'}"
        lines.append(f"{row['platform'].ljust(width)}  "
                     f"{VERDICT_LABEL.get(row['verdict'], row['verdict']).ljust(11)}  {controls}")
    for row in rows:
        if row["verdict"] not in USABLE:
            lines.append(f"  {row['platform']}: {row['note']}")
    good = sum(1 for r in rows if r["verdict"] in USABLE)
    lines.append("")
    lines.append(f"{good} of {len(rows)} platform checks are producing verdicts")
    lines.append("direct (no proxy)" if proxy is None
                 else f"through {sniper.mask_proxy(proxy)}")
    if proxy is None:
        lines.append("A BLOCKED line here is about this host, not the checker: "
                     "re-run with --proxy against the list you actually snipe with.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", action="append", default=[],
                        help="check only this platform (repeatable); default all")
    parser.add_argument("--proxy", default=None,
                        help="run the controls through this proxy, as a run would")
    parser.add_argument("--held", action="append", default=[],
                        help="override a held control, as platform=handle (repeatable)")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="per-request ceiling in seconds (default 15)")
    parser.add_argument("--seed", type=int, default=None,
                        help="fix the random free probes, for a repeatable run")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    overrides: dict[str, str] = {}
    for pair in args.held:
        if "=" not in pair:
            parser.error(f"--held wants platform=handle, got {pair!r}")
        platform, handle = pair.split("=", 1)
        overrides[platform.strip().lower()] = handle.strip()

    platforms = [p.strip().lower() for p in args.platform] or list(sniper.CHECKERS)
    rows = asyncio.run(verify(platforms, args.proxy, overrides, args.timeout,
                              args.seed if args.seed is not None else random.randrange(1 << 30)))

    if args.json:
        print(json.dumps({"proxy": args.proxy is not None, "platforms": rows}, indent=2))
    else:
        print(render(rows, args.proxy))
    # A non-zero exit is what makes this usable as a check rather than a report:
    # a checker that cannot see a held name is a broken build, not a warning.
    return 0 if all(r["verdict"] in USABLE for r in rows) else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
