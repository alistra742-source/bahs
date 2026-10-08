# bahs

Username availability across ten platforms, over your own proxy list.

Paste or upload a proxy list, pick the name spaces you want swept, and it checks
**every** name in them — nothing is sampled. Results stream to the dashboard as
they land, free names can go straight to a Discord webhook, and a platform that
rate-limits the whole pool sits out while the rest of the run continues.

Nothing is scraped. Nothing is validated ahead of time. The list you paste is
exactly what a run rotates over.

## Run it

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`. On Railway, `PORT` is injected; point `STORE_PATH`
at a volume so the proxy list and settings survive a deploy.

## Platforms

| platform | how the check works | latency from a datacenter IP |
|---|---|---|
| **roblox** | its own signup validator — a ~50 byte JSON body whose `code` *is* the verdict | ~130 ms |
| **minecraft** | `minecraftservices` profile lookup; 200 carries the account, 404 means free | ~290 ms |
| **github** | `api.github.com/users/{name}` — 200 taken, 404 free | ~130 ms |
| **telegram** | `t.me/{name}`; a live handle renders `tgme_page_title`, a free one the generic page | ~450 ms |
| **discord** | `unique-username/username-attempt-unauthed` | ~140 ms |
| **instagram** | `web_profile_info` with the web app id | refused from most datacenter IPs |
| **tiktok** | oEmbed: 200 + `author_name` taken, 400 free | ~300 ms |
| **x** | `x.com/{handle}` — status only, never the body | ~500 ms |
| **youtube** | `youtube.com/@{handle}` — status only | ~120 ms |
| **guns.lol** | profile page, first 16 KB | ~860 ms |

**GitHub needs a token.** Unauthenticated its API allows 60 requests an hour per
IP — not a check rate, a limit you hit in a minute and then sit out. Set
`GITHUB_TOKEN` and the ceiling becomes 5,000.

**Instagram is the weak one.** It refuses datacenter IPs with a blanket 429
before your proxy list is involved, so it depends on the list being
residential-ish. It reports that honestly as `blocked` rather than guessing.

## Name spaces

Pick any combination of these. `l` letters a–z, `n` digits, `c` letters+digits,
`og` dictionary words — and the length.

| | 3 | 4 | 5 |
|---|---|---|---|
| **l** | 17,576 | 456,976 | 11,881,376 |
| **n** | 1,000 | 10,000 | 100,000 |
| **c** | 46,656 | 1,679,616 | 60,466,176 |
| **og** | 431 | 1,418 | 2,157 |

Overlapping selections are dropped before the count: `og` words are letters, so
`3og` inside `3l` adds nothing, and `l`/`n` are both inside `c`. Selecting `3l`
and `3og` checks 17,576 names, not 18,007 — "every combination" never means the
same name twice.

Lower-case only, deliberately: a handle is case-insensitive for uniqueness
everywhere here, and TikTok's oEmbed will not resolve a mixed-case one at all.

### The ceiling, and why it moved

`MAX_ENUMERATION` used to be 1,000,000, and it was a *memory* limit — the names
were built as a list, and "all of length 4 alphanumeric" is ~200 MB of Python
strings while 14,776,336 is ~843 MB.

Enumeration is lazy now. `itertools` walks the space while the run consumes it,
so **memory no longer scales with the size of the space** — walking 400,000
names of a 60-million space holds RSS flat (measured: 29 MB → 29 MB).

So the ceiling is a sanity bound rather than a resource one, and it defaults to
100,000,000: every bucket above is allowed, including `5c`. What a big selection
costs is **wall clock**, which is why the dashboard shows an estimate from your
last measured rate instead of letting you start a three-day run blind. Nothing
is ever silently sampled — a space over the cap is refused with its real size.

## Speed, measured

Throughput tracks **connections ÷ platform latency**, not client parallelism.
Measured against a local origin through real HTTP proxies:

| origin latency | pool | connections | checks/s |
|---|---|---|---|
| 150 ms | 1 endpoint | 64 | 75 |
| 150 ms | 8 endpoints | 64 | **364** |
| 800 ms | 8 endpoints | 256 | 254 |
| 800 ms | 1 endpoint | 64 | 77 |

The client's own ceiling is around 2,000 dispatches/s, so it is never the wall.

That ceiling is only the ceiling if the scheduler gets out of its own way. The
dispatch loop asks the pool for `usable()` **once per check**, and `usable()` used
to walk every proxy in the list. Measured on the loop alone, 60,000 checks through
a 400-proxy pool: **35,924/s** counting each call, **86,328/s** once the count is
O(1) — a 2.4x change in dispatch cost that scaled with how good your list is.
The wall-clock gain in a real run is smaller, because with real exit IPs the
network dominates; it is the client-side floor that moved.
Measured end to end through the dashboard, 1,000 Roblox checks on eight proxy
endpoints ran at 323/s and finished in three seconds.
What moves the number is **more concurrent exit IPs**. With one rotating
endpoint, per-proxy connections are the whole ceiling: measured 3.9/s at 1
connection, 13.0/s at 6, 53.1/s at 32. Per-proxy connections are sized from your
pool and the concurrency you ask for, so a small list still gets the connections
it can use.

**A headless browser would make this slower, not faster** — a page load adds
hundreds of milliseconds and hundreds of MB per worker to do what one HTTP
request already does.

## Rate limits

A `429` is the platform talking, not your proxies, and it is handled as such:

- The proxy is **rested** briefly and counted in its own `blocked` column. It is
  never retired for it.
- When a platform has refused **every proxy still alive** in the pool, it is
  **paused** — with the `retry_after` the platform gave, capped at
  `SNIPE_PLATFORM_PAUSE_MAX`. Jobs for it are skipped instead of hammered.
- The **rest of the run continues** on the platforms that are still answering.
  A discord 429 no longer costs you the tiktok sweep.
- If every platform ends up paused, the run stops and says which and why.

A run only reports "proxies exhausted" when the list genuinely is — every proxy
retired.

## A flaky proxy no longer ends the run

The pool used to treat a *cooldown* as "unusable": one transport error put a
proxy to sleep for `SNIPE_PROXY_COOLDOWN` seconds, the scheduler stopped
dispatching, and with a single proxy the run then slept ten seconds at a time
before abandoning the sweep. Measured with one host answering 70% of requests:
302 of 3,000 names checked in 36 s, 30 s of it asleep, then "the list is dead".
A proxy in a cooldown is not a pool that cannot be used, so a cache of live
proxies is now what "usable" means; only retirement takes one out.

**Retirement is a rate, not a streak.** A streak of failures was wiped by any
answer, so a host that answered one request in three kept its record clean
forever and the run limped through the name list one error at a time. A proxy is
now written off when it has failed every attempt up to `SNIPE_PROXY_FAIL_LIMIT`
(it is simply gone), or `SNIPE_PROXY_FAIL_RATE` of at least sixteen attempts.
A host failing a third of its requests is kept — it is still producing answers,
and ending a sweep is worse than reporting its error rate. When every proxy in
the list is retired the run stops and says so.

The `done` summary carries `proxy_attempts` and `proxy_failures`, and the
dashboard turns them into one line: a high transport-failure share is the list,
not the names.

**Errors and unanswered checks are different things.** A transport failure is
the tunnel's fault. A challenge page, a status we do not read, or a 200 missing
the field we look at is the *platform's* answer, reported separately as
`unanswered` — it never rests, strikes or retires a proxy. Charging those to the
list is what made a run against a challenged site report "your list is dead"
about a list that was working.

## Alerts

Set a Discord webhook on the **Alerts** tab (or `ALERT_WEBHOOK`) and free names
are posted as they are found, digested into batches of ten embeds. Default
template: `{platform} username available: `{username}``. Placeholders are
`{username}`, `{platform}`, `{detail}`.

The webhook is posted to **directly, never through your proxy list** — a webhook
URL authenticates the post *and* names the channel, so routing it through a
pasted proxy would hand whoever runs that proxy the ability to post there.

A webhook URL is a capability. Keep it out of screenshots, and use **Send test**
to confirm it lands rather than assuming.

## Claiming

`GET /claim?platform=&username=` returns the platform's own registration route.
It does **not** create an account, and no part of this does: automating signups
is what gets an IP range banned, and a name is only yours once *you* have
registered it. Confirm the name is still free, then claim it yourself.

## API

| | |
|---|---|
| `GET /` | the dashboard |
| `GET /info`, `GET /health` | platforms, generation, counters |
| `GET /menu` | every bucket with its real size |
| `GET /proxies` `POST` `DELETE` `POST /proxies/clear` `POST /proxies/upload` | the list |
| `POST /scan` | `{buckets:[{kind,length}], platforms, concurrency, stream}` |
| `POST /snipe` | the same for explicit `usernames` |
| `GET /snipe?username=` | one name |
| `GET /generate` | `letters\|alnum\|numbers\|words` by length |
| `POST /scan/stop` `POST /snipe/stop` `POST /runs/stop` `GET /runs` | the stop path |
| `GET /claim` | where to register a name |
| `GET /settings` `POST /settings` `POST /settings/test-webhook` | alerts |

`stream: true` returns NDJSON: `start`, then a `result` line per verdict
(`available`, `taken`, `invalid`, `blocked`, `error`, `unanswered`), a
`progress` line as it goes, a `waiting` line when every live proxy is cooling
down, and a `done` summary.

## Environment

| variable | default | |
|---|---|---|
| `PORT` | `8080` | |
| `STORE_PATH` | `data/proxies.json` | point at a volume |
| `SETTINGS_PATH` | `data/settings.json` | alert settings |
| `GITHUB_TOKEN` | — | raises GitHub from 60/hr to 5,000 |
| `ALERT_WEBHOOK` | — | Discord webhook |
| `ALERT_TEMPLATE` | `{platform} username available: `{username}`` | |
| `ALERT_PING` | — | `@here`, `<@&roleid>` — first message only |
| `ALERT_BATCH` | `10` | embeds per message |
| `ALERT_MIN_INTERVAL` | `1.2` | seconds between posts |
| `ALERT_MAX_MESSAGES` | `20` | posts per run |
| `MAX_ENUMERATION` | `100000000` | names per run |
| `MAX_PROXIES` | `100000` | |
| `SCAN_CONCURRENCY` | `256` | checks in flight |
| `SCAN_BUFFER_MAX` | `50000` | largest buffered (non-stream) run |
| `SCAN_TARGET_RATE` | `100` | reported against on the dashboard |
| `SNIPE_PER_PROXY` | `4` | floor for connections per proxy |
| `SNIPE_PER_PROXY_MAX` | `64` | ceiling for one proxy |
| `SNIPE_PLATFORM_PAUSE` | `90` | seconds a blocked platform sits out |
| `SNIPE_PLATFORM_PAUSE_MAX` | `900` | cap when the platform names a `retry_after` |
| `SNIPE_PROXY_BLOCK_LIMIT` | `40` | blocks before a proxy is retired |
| `SNIPE_PROXY_FAIL_LIMIT` | `6` | attempts before a proxy that failed every one is retired |
| `SNIPE_PROXY_FAIL_RATE` | `0.75` | share of failures that writes a proxy off |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` | `3` / `8` | seconds |
| `SCAN_CONNECT_TIMEOUT` / `SCAN_READ_TIMEOUT` | `2` / `6` | tighter in a scan |
| `MAX_CONCURRENT_RUNS` | `8` | 429 past it |
| `OG_WORDS_FILE` / `OG_WORDS` | — | extra wordlists for `og` |
| `LOG_LEVEL` | `info` | |

## The dashboard on a phone

The phone view is verified in a real browser, not assumed: Chromium at a 390 px
viewport and at 1440 px, with the document width compared to the viewport and
every element checked for crossing it.

Two things that were wrong and are now measured rather than guessed:

- **The page was 550 px wide in a 390 px viewport.** `table{min-width:520px}`
  is what makes a wide table scroll sideways, but the table's intrinsic width
  travelled up through `.card`, `.grid2` and `main` — every one of them a grid
  item with the default `min-width:auto` — and the whole page grew instead of
  the table. The wrappers now carry `min-width:0`, so the tables scroll **inside**
  their own box and the document is exactly the viewport width (390 = 390).
- **The entire `@media(max-width:900px)` block sat above the rules it overrides.**
  Same specificity, decided by source order, and the desktop rules came later —
  so the bucket grid, the table font and the button padding quietly stayed at
  desktop metrics on a phone. That is why the sizes in the name-space grid kept
  getting clipped (`1,679,6` instead of `1,679,616`) no matter how many times the
  numbers were shrunk. The phone block now lives at the end of the sheet, with a
  comment saying why it has to.

## Layout

```
server.py      HTTP surface, the run loop's reporting, alerts wiring
sniper.py      the proxy pool, the scheduler, and the per-platform checkers
generator.py   buckets and lazy enumeration
proxies.py     parsing and storing the proxy list
alerts.py      the Discord webhook
static/        the dashboard (one file, no build step)
wordlists/     og.txt
```
