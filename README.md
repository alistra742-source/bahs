# proxy-scraper

Scrapes public proxy lists, validates every candidate, and serves a ranked,
platform-tested list over HTTP. Built to run on Railway.

What it does each cycle:

1. **Scrape** — 20 public list sources (TheSpeedX, monosans, clarketm, hookzof,
   roosterkid, mmpx12, ProxyScrape, proxy-list.download, prxchk, …), HTTP/S,
   SOCKS4 and SOCKS5, fetched concurrently.
2. **Validate** — every candidate is checked through its own proxy for
   liveness and round-trip latency.
3. **Classify** — a single judge request reveals the exit IP the target saw
   and the request headers it received, which yields the anonymity level:
   `elite` (IP hidden, no forwarding headers), `anonymous` (IP hidden, headers
   present) or `transparent` (your real IP leaked).
4. **Platform-test** — Discord, guns.lol and Instagram are probed through the
   proxy. Status **and** body markers are checked, so a Cloudflare "Just a
   moment…" page returned with a 200 does not pass.
5. **Score + persist** — each proxy gets a 0–100 score from anonymity, latency
   and platform pass rate, ranked, pruned, and written to disk.

On top of that list, **`POST /snipe` checks whether a username is free** on
Discord, guns.lol and Instagram, driving every request through the validated
proxies — the platforms block a datacenter IP outright, which is the reason the
proxy list exists in the first place.

`GET /generate` builds candidate names (**N letters, N alphanumerics, N
numbers, or N-length "OG" dictionary words** from a configurable list), and
`POST /scan` generates and checks them in bulk at a target rate of 100 checks a
second or better. `GET /claim` says where a free name is registered.

## Deploy on Railway

The repository root **is** the service. In Railway:

1. **New Project → Deploy from GitHub repo** (branch `main`; root directory is the repo root).
2. Railway reads `railway.json` and builds from the `Dockerfile`.
3. **Settings → Networking → Generate Domain**.
5. *(Recommended)* **Settings → Volumes → Add Volume**, mount at `/data`, and
   set `STORE_PATH=/data/proxies.json` so the validated list survives deploys.

`PORT` is injected by Railway; the container binds `0.0.0.0:$PORT`.

### Variables

| Variable | Default | Notes |
| --- | --- | --- |
| `PORT` | `8080` | injected by Railway |
| `REFRESH_INTERVAL` | `1800` | seconds between refresh cycles |
| `AUTO_START` | `1` | start the refresh loop at boot; `0` waits for the Start button / `POST /start` |
| `REFRESH_ON_START` | `1` | run a cycle immediately at boot |
| `MAX_CANDIDATES` | `20000` | proxies validated per cycle |
| `MAX_CONCURRENCY` | `800` | simultaneous proxy checks — the main speed lever (~3200 sockets at peak) |
| `MAX_FAILURES` | `2` | consecutive failures before a proxy is dropped (a never-alive proxy goes on its first failure) |
| `STORE_SAVE_EVERY` | `200` | persist the store every N validated proxies, so a long cycle is durable mid-run |
| `PROGRESS_EVERY` | `500` | log a progress line every N validated proxies |
| `FAIL_COOLDOWN` | `21600` | a failed proxy is skipped for this many seconds even if a scrape offers it again; `0` disables |
| `MAX_DEAD_REMEMBERED` | `200000` | ceiling on the skip list, oldest forgotten first |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` | `2.5` / `5` | per-request seconds — a dead proxy costs its connect timeout, so these dominate cycle time |
| `JUDGE_TIMEOUT` | `7` | hard ceiling per proxy check |
| `MAX_LATENCY_MS` | `4000` | latency beyond this scores zero |
| `W_ANONYMITY` / `W_LATENCY` / `W_PLATFORM` | `0.35` / `0.25` / `0.40` | score weights |
| `STORE_PATH` | `data/proxies.json` | point at a volume mount for durability |
| `STALE_AFTER` | `3 × REFRESH_INTERVAL` | drop proxies not validated within this window |
| `JUDGE_URL` | `https://httpbin.org/get` | must return `origin` + `headers` |
| `JUDGE_FALLBACKS` | `httpbingo.org/get`, `eu.httpbin.org/get`, `postman-echo.com/get` | the rest of the judge pool, tried in rotation |
| `JUDGE_PER_ENDPOINT` | `60` | simultaneous judge requests allowed against any one endpoint |
| `SNIPE_CONCURRENCY` | `32` | simultaneous name checks in one snipe request |
| `SNIPE_RETRIES` | `2` | how many times a blocked or proxy-failed name is retried on another proxy |
| `SNIPE_MAX_NAMES` | `200` | ceiling on names per snipe request |
| `SNIPE_POOL` | `400` | how many validated proxies the sniper rotates over |
| `SNIPE_PROXY_COOLDOWN` | `60` | seconds a proxy that blocked or errored is rested; `0` disables |
| `SNIPE_PER_PROXY` | `4` | simultaneous requests through any one proxy |
| `SNIPE_CONNECT_TIMEOUT` / `SNIPE_READ_TIMEOUT` | `3` / `8` | sniper timeouts, tighter than the validator's so a bad proxy is abandoned quickly |
| `SNIPE_POOL_TTL` | `300` | seconds a warm proxy pool (and its connections) is reused across requests |
| `SNIPE_PLATFORM_WEIGHT` | `4` | how often a proxy that passed a platform's own probe is repeated in that platform's rotation (`1` = plain round-robin) |
| `OG_WORDS_FILE` | – | extra/replacement word list for the `words` pattern (one word per line or space separated) |
| `OG_WORDS` | – | extra inline words, comma separated |
| `SCAN_CONCURRENCY` | `256` | simultaneous checks in one scan — the throughput lever |
| `SCAN_MAX_NAMES` | `20000` | names accepted in one scan (a longer list is truncated and reported) |
| `SCAN_RETRIES` | `0` | retries per name in a scan; at this width a retry costs more than it buys |
| `SCAN_CONNECT_TIMEOUT` / `SCAN_READ_TIMEOUT` | `1.5` / `4` | scan timeouts, tighter than the sniper's so a dead proxy is dropped fast |
| `SCAN_PER_PROXY` | `8` | simultaneous requests through one proxy during a scan |
| `SCAN_TARGET_RATE` | `100` | the rate a scan is measured against |

## Dashboard

`GET /` serves a single-page dashboard over the same API:

- **Start / Stop** — start and stop the background refresh loop. State is shown
  live; `Stop` cancels an in-flight cycle. `Refresh now` queues one cycle
  without touching the loop state.
- **Validated proxies** — the ranked list with filters (platform, anonymity,
  protocol, min score, limit), per-row platform dots, latency, score bar,
  copy and remove, plus `Purge dead` / `Purge all` and `Export .txt`.
- **Stats** — tracked, alive, per-platform, per-anonymity, cycles, plus two
  cumulative counters that survive restarts: `total seen` (distinct proxies ever
  tracked) and `checks run` (every validation performed).
- **Copy the working list** — one button per target (`all`, `discord`,
  `guns.lol`, `instagram`, `passes all 3`) copies the matching proxies as plain
  `ip:port` lines, honouring the anonymity/protocol/limit filters on screen.
- **Sniper · pattern scan** — pick the platforms, then either paste names (one
  per line, `@` optional) and press `Check names`, or build patterns —
  `OG words` / `letters` / `alphanumeric` / `numbers` with a length and a count
  each — and press `Scan at max rate`. `Generate` fills the box so the list can
  be inspected or edited first. Verdicts stream in one line at a time with the
  status, the reason, the proxy used and the latency, while the bar under the
  box shows `checked / total` and the completed-checks-per-second rate.
  `Copy available` takes the names that came back free, and each free row has a
  `claim` button that copies the name and opens its registration page.
- No login: the API is open, so the dashboard needs no key.

### How the sniper reads each platform

Every rule below was read off the live site rather than assumed:

| Platform | Request | Free | Taken | Never available |
| --- | --- | --- | --- | --- |
| Discord | `POST /api/v9/unique-username/username-attempt-unauthed` | `200 {"taken":false}` | `200 {"taken":true}` | `400` is a **validation** refusal (reserved word, bad length), reported as `invalid`, never as free |
| guns.lol | `GET /{name}` | the site's default page, no profile block | page carries `profile-page-json-ld` with `"identifier":"<name>"` | — |
| Instagram | `GET /api/v1/users/web_profile_info/?username=` with `x-ig-app-id` | `404` | `200` with a user object | `401`/`429`/`302` are blocks and are reported as `blocked`, not guessed at |

`blocked` and `error` are the only statuses that say nothing about the name, so
they are the only ones retried (on another proxy, up to `SNIPE_RETRIES`). A name
that the strictest of the three platforms could not accept is rejected before a
request is spent on it.

### Why a snipe batch is fast

1. **A warm client per proxy.** One `httpx` client per proxy carries the whole
   batch, so the proxy CONNECT tunnel and the TLS session to each target are
   paid once instead of once per name. Measured against the real targets through
   a proxy: **1.66× faster and 12 connections down to 10** for a 12-check batch,
   and the gap widens with proxy latency, since that is what the saving is.
2. **The pool is shared and remembered.** The pool is built once and reused
   across requests (`SNIPE_POOL_TTL`), and a proxy that blocks or errors is
   rested for `SNIPE_PROXY_COOLDOWN`. A retry therefore lands on a different,
   healthy host instead of the one that just refused it.
3. **Proxies are matched to the platform.** A Discord check prefers a proxy
   that already passed the Discord probe, so fewer requests come back blocked.
4. **Only the bytes that answer the question.** The guns.lol page is 22–40 KB
   of Next.js payload; both signals it turns on land inside the first 9 KB, so
   the response is read to the verdict and the remainder drained in the
   background to keep the connection warm.
5. **Results stream.** `"stream": true` returns NDJSON, one verdict per line,
   so the first answer is on screen in the first second instead of after the
   whole batch.
6. **A sliding window, not fixed chunks.** The moment one check finishes the
   next starts, so in-flight work stays at `SCAN_CONCURRENCY` from the first
   verdict to the last. Draining a chunk before starting the next one leaves the
   tail of every chunk idle, which is throughput a scan cannot spare.

### Throughput

Measured with the real scheduler and real HTTP clients against a target that
holds each request open, so the numbers are the engine's, not a stub's:

| concurrency | per-check latency | checks a second |
| --- | --- | --- |
| 32 | 250 ms | 83 |
| 256 | 250 ms | **538** |
| 256 | 1000 ms | **170** |

Peak in-flight tracked the ceiling exactly (256 of 256), which is the sliding
window doing its job. With a real proxy pool the rate is whatever the pool
allows — every check still costs one proxy round trip, and free proxies are
slow and often dead — but the scheduler is not the bottleneck; `SCAN_CONCURRENCY`
is the dial. `POST /scan` reports `per_second`, `target_rate` and `target_met`
so the live number is never a guess.

### Why a cycle is fast

Four things keep the wall clock down, in order of impact:

1. **Dead proxies are not re-checked.** A proxy that has never once answered is
   dropped on its first failure and remembered for `FAIL_COOLDOWN`. Free lists
   hand back the same dead addresses on every scrape, so without this every
   cycle re-checked the same ~20k corpses; with it, a repeat cycle validates
   only the alive set plus genuinely new hosts (measured: 10 offered → 1
   checked on a repeat scrape).
2. **One round trip per alive proxy.** The judge and the three platform probes
   start together; a proxy the judge rejects has its probes cancelled rather
   than awaited.
3. **The judge is a pool, not one host.** A single httpbin instance loses about
   40% of requests once ~400 are in flight against it (measured), and a failed
   judge request is indistinguishable from a dead proxy — it reads as *nothing
   is alive*. Checks rotate across `JUDGE_URLS`, each endpoint capped by
   `JUDGE_PER_ENDPOINT`, and a judge that answers 429/5xx hands the check to the
   next one. A proxy that will not connect is *not* retried against another
   judge: that is the proxy's fault and rotating would just pay the timeout
   again.
4. **Short connect timeouts and high concurrency** — see `CONNECT_TIMEOUT` and
   `MAX_CONCURRENCY` above. A cycle over N candidates costs roughly
   `N / MAX_CONCURRENCY` connection attempts.

## API

```
GET    /                        dashboard (HTML)
GET    /info                    service info
GET    /health                  scheduler + store status (open)
GET    /stats                   store statistics
GET    /proxies                 filtered, ranked list
GET    /best                    top proxies passing every platform
DELETE /proxies?proxy=          remove one tracked proxy
POST   /proxies/purge?scope=    drop dead (default) or all proxies
POST   /start                   start the refresh loop
POST   /stop                    stop the refresh loop
POST   /refresh                 queue a single scrape+validate cycle
POST   /snipe                   check usernames through the validated proxies
GET    /snipe?username=         the same for one name
GET    /generate                candidate names for one pattern
POST   /generate                the same for several patterns at once
POST   /scan                    generate + check in bulk, NDJSON by default
GET    /claim                   where to register a free name
```

`POST /snipe` body: `usernames` (list) or `username` (single), `platforms`
(`discord` | `guns.lol` | `instagram`, default all three), `concurrency`,
`retries`, and `stream` (NDJSON instead of one JSON document).

`GET /proxies` query parameters: `platform` (`discord` | `guns.lol` |
`instagram`), `anonymity` (`elite` | `anonymous` | `transparent`), `protocol`
(`http` | `https` | `socks4` | `socks5`), `min_score` (0–100),
`all_platforms` (true = only proxies that passed every target, applied before
`limit`), `limit` (1–1000), `format` (`json` | `txt`).

```bash
# top 50 elite proxies that pass Discord, as plain ip:port lines
curl -s "https://<domain>/proxies?platform=discord&anonymity=elite&limit=50&format=txt"

# the best of the best: pass Discord + guns.lol + Instagram
curl -s "https://<domain>/best?limit=25"

# start / stop the background loop
curl -s -X POST "https://<domain>/start"
curl -s -X POST "https://<domain>/stop"

# is this name free on all three, through the validated proxies?
curl -s -X POST "https://<domain>/snipe" -H 'Content-Type: application/json' \
  -d '{"username":"zzq7x9k2v4m8n3"}'

# a batch, streamed as NDJSON, one verdict per line
curl -s -N -X POST "https://<domain>/snipe" -H 'Content-Type: application/json' \
  -d '{"usernames":["newname","oldname"],"platforms":["discord"],"stream":true}'

# force a refresh off-schedule
curl -s -X POST "https://<domain>/refresh"

# 500 four-letter OG dictionary words
curl -s "https://<domain>/generate?pattern=words&length=4&limit=500"

# a reproducible sample of 200 five-character alphanumerics
curl -s "https://<domain>/generate?pattern=alnum&length=5&limit=200&seed=7"

# generate and scan 2000 five-letter words on all three platforms, live rate
curl -s -N -X POST "https://<domain>/scan" -H 'Content-Type: application/json' \
  -d '{"patterns":[{"pattern":"words","length":5,"limit":2000}],"concurrency":256}'

# where a free name gets registered
curl -s "https://<domain>/claim?platform=discord&username=freedude"

# manage the validated set
curl -s -X DELETE "https://<domain>/proxies?proxy=http%3A%2F%2F1.2.3.4%3A8080"
curl -s -X POST "https://<domain>/proxies/purge?scope=dead"
```

### Patterns

| `pattern` | characters | size for length n |
| --- | --- | --- |
| `letters` | `a-z` `A-Z` | `52**n` |
| `alnum` | `a-z` `A-Z` `0-9` | `62**n` |
| `numbers` | `0-9` | `10**n` |
| `words` | the configured dictionary, filtered to exactly n letters | – |

`mode=random` (the default) samples, `mode=sequential` walks the space from the
start, and `seed` makes a sample reproducible — that is what lets a scan be
rerun later without re-asking about names that were already answered. A space
smaller than the limit is enumerated instead of sampled, so `numbers` length 2
returns all hundred rather than a random hundred.

### Claiming

A username can only be taken by an account, and an account is created by a
person, so `GET /claim` is a handoff rather than a factory: it returns the
platform's registration URL, the path to change the name on an existing
account, and a reminder that the name should be re-checked first. Nothing in
this service creates accounts on your behalf.

### Scoring

```
score = 100 × (w_a·anonymity + w_l·latency + w_p·platforms) / (w_a + w_l + w_p)

anonymity : elite 1.0 · anonymous 0.6 · transparent 0.1 · unknown 0.0
latency   : max(0, 1 − latency_ms / MAX_LATENCY_MS)
platforms : passed_platforms / probed_platforms   (3 targets)
```

## Local run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8080
```

## Notes

- Proxies are validated **against the real targets**, so a proxy marked
  `platforms.discord.ok = true` passed a live request to Discord. Public free
  proxies are flaky by nature; the score reflects that and the store prunes
  dead entries every cycle.
- Datacenter and free proxies are frequently rate-limited by Discord,
  guns.lol and Instagram. Expect the `platforms_passed == 3` set to be small
  and volatile; that is the honest signal, not a bug.
- The store is in-memory with JSON persistence. On Railway, mount a volume or
  the list rebuilds from scratch on each deploy.
