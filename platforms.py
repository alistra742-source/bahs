"""Platform reachability probes.

Three targets matter for this build: Discord, guns.lol and Instagram. A proxy
only counts as *platform-suitable* if a real request through it reaches the
target without a block page, so each probe checks status **and** body markers,
not status alone -- Cloudflare and the platforms themselves answer blocked
proxies with 200s that render a challenge page.
"""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Platform:
    name: str
    url: str
    accept_status: tuple[int, ...]
    # Any of these substrings in the body means a block/challenge page, even
    # when the status code looked healthy.
    block_markers: tuple[str, ...]
    # If set, the body must contain at least one of these to count as a real
    # reach (defends against 200 responses that are an empty CDN error).
    require_markers: tuple[str, ...] = ()
    # Host header sent with the request; some probes need the bare host.
    method: str = "GET"


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        name="discord",
        # The public gateway endpoint answers any unauthenticated client with a
        # gateway URL. It is the lightest "is Discord reachable and not
        # challenging me" request that exists.
        url="https://discord.com/api/v9/gateway",
        accept_status=(200,),
        block_markers=(
            "just a moment",
            "attention required",
            "cf-chl",
            "access denied",
            "you have been blocked",
        ),
        require_markers=("gateway",),
    ),
    Platform(
        name="guns.lol",
        url="https://guns.lol/",
        accept_status=(200, 301, 302),
        block_markers=(
            "just a moment",
            "attention required",
            "cf-chl",
            "access denied",
            "you have been blocked",
            "error 1020",
        ),
    ),
    Platform(
        name="instagram",
        url="https://www.instagram.com/",
        accept_status=(200,),
        block_markers=(
            "just a moment",
            "attention required",
            "cf-chl",
            "challenge_required",
            "please wait a few minutes",
            "we restrict certain activity",
        ),
    ),
)

# Cap on how much body we read per probe. The markers live in the first KBs.
MAX_BODY_BYTES = 64 * 1024


@dataclass
class PlatformResult:
    name: str
    ok: bool
    status: int | None
    latency_ms: float | None
    reason: str = ""


async def probe(
    client: httpx.AsyncClient, platform: Platform, timeout: httpx.Timeout
) -> PlatformResult:
    """Probe one platform through an already proxy-configured client."""
    import time

    started = time.perf_counter()
    try:
        resp = await client.request(
            platform.method,
            platform.url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
    except httpx.TimeoutException:
        return PlatformResult(platform.name, False, None, None, "timeout")
    except httpx.HTTPError as exc:
        return PlatformResult(platform.name, False, None, None, f"transport:{type(exc).__name__}")

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    body = resp.content[:MAX_BODY_BYTES].decode(errors="ignore").lower()

    if resp.status_code not in platform.accept_status:
        return PlatformResult(platform.name, False, resp.status_code, elapsed_ms, f"status:{resp.status_code}")

    for marker in platform.block_markers:
        if marker in body:
            return PlatformResult(platform.name, False, resp.status_code, elapsed_ms, f"blocked:{marker}")

    if platform.require_markers and not any(m in body for m in platform.require_markers):
        return PlatformResult(platform.name, False, resp.status_code, elapsed_ms, "unexpected-body")

    return PlatformResult(platform.name, True, resp.status_code, elapsed_ms, "ok")


async def probe_all(
    client: httpx.AsyncClient, timeout: httpx.Timeout
) -> dict[str, PlatformResult]:
    """Probe every platform through one client, concurrently."""
    import asyncio

    results = await asyncio.gather(
        *(probe(client, p, timeout) for p in PLATFORMS), return_exceptions=True
    )
    out: dict[str, PlatformResult] = {}
    for platform, result in zip(PLATFORMS, results):
        if isinstance(result, BaseException):
            out[platform.name] = PlatformResult(platform.name, False, None, None, "probe-error")
        else:
            out[platform.name] = result
    return out
