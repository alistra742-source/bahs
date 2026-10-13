# bahs

Username availability across sixteen platforms, over your own proxy list.

Paste or upload a proxy list, pick the name spaces you want swept, and it checks
**every** name in them — nothing is sampled. Results stream to the dashboard as
they land, free names can go straight to a Discord webhook, and a platform that
rate-limits the whole pool sits out while the rest of the run continues.

A run is not tied to the tab. Close the page, lose signal, put the phone down —
the sweep keeps going, and coming back reattaches to the same run, buffer and
all, until you press **Stop** or the pool genuinely dies.

Nothing is scraped. Nothing is validated ahead of time. The list you paste is
exactly what a run rotates over.

## Run it

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080`. On Railway, `PORT` is injected; point `STORE_PATH`
at a volume so the proxy list and settings survive a deploy.

The dashboard's Live card prints the commit it is running (`build 9597c9d`), from
`RAILWAY_GIT_COMMIT_SHA` or the checkout's `HEAD`, and `/health` and `/info`
carry the same string. That is how "I changed it, why do I still see the old
bug?" gets answered: a stale deploy or a cached page shows an old commit, and
the page itself is served `no-cache`, so it has to revalidate.

## Platforms

| platform | how the check works | latency from a datacenter IP |
|---|---|---|
| **discord** | its own `unique-username/username-attempt-unauthed` endpoint — a JSON `taken` flag, and a validation 400 that is never read as free | ~140 ms |
| **instagram** | `web_profile_info` with the web app id | refused from most datacenter IPs |
| **tiktok** | the profile page, asked with a mobile Safari agent, where its `user-detail` payload decides — with oEmbed only as a fallback, since a 400 from it is not a verdict | ~300 ms, ~1 s when the page confirms |
| **x** | `x.com/{handle}` — status only, never the body | ~500 ms |
| **youtube** | `youtube.com/@{handle}` — status only | ~120 ms |
| **facebook** | the page as its link-preview agent sees it: `og:title` present = held, a bare `<title>Facebook</title>` fallback = free. Head only — ~29 KB of a page that runs to 5 MB | — |
| **reddit** | `reddit.com/user/{name}/` through the same agent: the profile's own `og:` metadata = held, the site's generic front page = free | — |
| **telegram** | `t.me/{name}`; a live handle renders `tgme_page_title`, a free one the generic page | ~450 ms |
| **github** | `api.github.com/users/{name}` — 200 taken, 404 free | ~130 ms |
| **roblox** | its own signup validator — a ~50 byte JSON body whose `code` *is* the verdict | ~130 ms |
| **minecraft** | `minecraftservices` profile lookup; 200 carries the account, 404 means free | ~290 ms |
| **steam** | the vanity URL's own XML — `<steamID64>` for a live profile, `<error>` for a free one | 6.3 KB vs 144 bytes |
| **kick** | `api/v2/channels/{name}`, the same JSON its channel page loads — 200 the channel, 404 `Channel not found.` | — |
| **bluesky** | `resolveHandle` on `{name}.bsky.social` — a DID for a handle that exists, `InvalidRequest` (63 bytes) for one that does not | 42 vs 63 bytes |
| **guns.lol** | profile page, first 64 KB: Schema.org block or account payload taken, ban heading `banned`, claim offer free | ~860 ms |
| **huggingface** | `api/users/{name}/overview` — 200 the account, 404 nothing | 15 KB |

Sixteen name spaces, picked as the ones a username is worth having on rather than
every endpoint that can answer a query. The longer tail that used to be here —
myanimelist, vk, disqus, wattpad, duolingo, dribbble, soundcloud, chess.com,
dockerhub, codeberg, mastodon, dev.to, lichess, gitlab, itch.io, gumroad — was a
list nobody was sniping on, and each one was another thing to keep working. What
a platform *cannot* have is decided locally and for free (`NAMESPACES`): a name
that cannot exist is reported `invalid` without spending a request on it.

Every platform was verified the same way: known-held handles must read taken, and
**names that cannot exist must read free** — on Facebook, six pages and sixteen
impossible names; on Reddit, four handles and four impossible names. **Tumblr was
dropped** for failing exactly that: it answers 403 to every exit in a datacenter
range, so it cannot be checked from a list like this one.

**TikTok does not have digit-only handles, and saying otherwise costs you a
registration.** `1234567` passes the charset rule and the profile page answers
"no such user", because there is no account there — so the honest-looking verdict
is `available` and TikTok's own signup then refuses with *"Couldn't update user.
Try another one."* The rule is local and free: an all-digit handle is `invalid`
on TikTok and never reaches a request.

**A platform decides whether to answer based on the client, not only the IP.**
Facebook and Reddit both answer a *link-preview* agent and refuse nearly
everything else: a browser agent gets Facebook's `400 Error` page, and Reddit
gives every non-crawler a JavaScript shell that says nothing about any name. The
one agent that answers both is `PREVIEW_UA` in `sniper.py`. This is also why no
check is pinned to a version-numbered browser string: an agent a platform has
quietly stopped answering is worse than one it refuses, because an answer still
arrives and still looks like an answer.

**And on those two, a free verdict is control-read before it is believed.** A
platform that would rather not answer serves the *same* page for a name that is
held as for one that is not — measured on Facebook with two link-preview agents
it had begun refusing, where `zuck`, `nasa` and `microsoft` all came back as the
login wall a free name gets. That reads as `available` for **every name in the
run**. So `facebook` and `reddit` are asked about a name that is certainly held
before any free verdict from them stands (`SignalGuard` in `sniper.py`, tuned by
`SNIPE_SIGNAL_GUARD_TTL` and `SNIPE_SIGNAL_GUARD_AFTER`):

| the control name reads | what is reported |
|---|---|
| held | the free verdict stands, and the run says the control held |
| free too | **blocked** — the platform refusing to answer — until a later control read says otherwise |
| cannot be read | **unanswered** — retried on another exit, never reported as available |

##  Name spaces

Pick any combination of these. `l` letters a–z, `n` digits, `c` letters+digits,
`og` dictionary words — and the length.

| | 2 | 3 | 4 | 5 |
|---|---|---|---|---|
| **l** | 676 | 17,576 | 456,976 | 11,881,376 |
| **n** | 100 | 1,000 | 10,000 | 100,000 |
| **c** | 1,296 | 46,656 | 1,679,616 | 60,466,176 |
| **og** | 82 | 431 | 1,418 | 2,157 |

The picker is a grid: a length down the side, a kind across the top, and any
number of cells selected at once — `2c` and `3c` and `4c` in one go if that is
what you want. The size shown on each cell is computed, not guessed, and the sum
of your selection is printed under the grid with an estimate from your last
measured rate, so a three-hour sweep says so before it starts.

Overlapping selections are dropped before the count: `og` words are letters, so
`3og` inside `3l` adds nothing, and `l`/`n` are both inside `c`. Selecting `3l`
and `3og` checks 17,576 names, not 18,007 — "every combination" never means the
same name twice. The dashboard says which selections it dropped, rather than
quietly checking fewer names than the count implies.

Lower-case only, deliberately: a handle is case-insensitive for uniqueness on
every target here, so generating both cases would ask the same question twice.
Names you paste in are not rewritten — every checker resolves case the way its
platform does (measured across all sixteen, only TikTok needed the request itself
to be lower-cased, which `check_tiktok` does).

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

## Name order, and why a run never showed you one letter

Every run draws its names in a random order across the **whole** space, not in
enumeration order. The difference is not cosmetic. `itertools.product` varies the
*last* character fastest, so the first 46,656 names of a 5-character alphanumeric
space are all `aaa??` and the first character does not change until 1.7 million
names in. Streaming that space in order — which is what a run used to do — gives a
wall of names sharing a prefix, and a shuffle window of 50,000 never reaches past
`aa`.

Two paths, both random, and which one you get is stated:

- **The selection fits in `SELECTION_BUILD_MAX`** (2,000,000 by default): every
  name is built and shuffled globally, so the order is a true permutation of the
  space, and a run with no cap checks all of it.
- **It does not fit**: set a **cap** and that many names are drawn at random from
  the whole space, weighted by each bucket's size, with duplicates rejected.
  Measured — `5c` with a cap of 60 gave 60 distinct names across 29 different
  first characters, and `4l` with no cap put all 26 first letters inside its first
  150 names.

`limit` is the cap. The run reports the **sample size** as its total, so progress
is measured against what will actually be checked and never against a space it is
not checking. A selection over the build limit with no cap is refused, eagerly,
with the size in the message — never quietly sampled and then called done.

`SHUFFLE_NAMES=false` still restores alphabetical order, for anyone auditing a
prefix on purpose.

## Two rates, and a free name agreed twice

A single "per second" over a throttled run is the oldest lie in this genre. There
are two numbers where there used to be one:

- `checks_per_second` — requests the run completed, **refusals included**;
- `verdicts_per_second` — names actually resolved. This is the real speed.

Both are sent on every progress tick and shown side by side, never in place of
each other: a run that is 80% refused is fast on the first and slow on the second,
and the dashboard says both.

**Results are verdicts only** — `available` and `taken`. Refusals, transport errors
and unreadable answers are counted as run health, because they say something about
a proxy or a platform and nothing whatever about the name. They are on the page as
health, never as a result row.

**A free name is re-asked before it is reported.** Free is the one verdict you act
on, so a check that returns `available` is asked again through the next proxy — on
a rotating endpoint that is a different exit:

| second read | what is reported |
| --- | --- |
| agrees | free, marked **confirmed** |
| says the name is held | **taken**, and the correction is counted |
| cannot answer | free, marked **unconfirmed** — never dressed up as certain |

Exercised against a stubbed checker that says free and then taken: the run reports
`taken` with `corrected=True`. Turn it off with `SNIPE_CONFIRM_FREE=false`.

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
- If every platform ends up paused, the run **waits** for the shortest pause to
  lift and carries on, up to `SNIPE_PLATFORM_WAIT`. It used to end there, which
  is what threw away every name that had not been asked about yet ("17,303
  checks were never made"). It only stops if the wait is exhausted.

**A rotating endpoint is one proxy, and one refusal is not a verdict.** A
single-line list of one rotating global endpoint is one proxy URL — so the
old "every proxy was refused" test tripped on the very first 403, sat the whole
platform out, and (when it was the only platform) ended the run after a single
answer. Two things fix that:

- A platform's refusal of one proxy is now remembered for `SNIPE_BLOCK_MEMORY`
  seconds, not forever. A rotating endpoint answers from a different exit IP on
  the next request, so a 403 from a minute ago says nothing about the request
  going out now; the mark ages out and the proxy is tried again.
- A pause needs real evidence: at least `SNIPE_PLATFORM_BLOCK_MIN` refusals, at
  least one per live proxy, **and** refusals must be at least
  `SNIPE_PLATFORM_BLOCK_RATE` (default 0.5) of everything that platform has
  answered.
- ...and the platform has to have gone **quiet**: `SNIPE_PLATFORM_QUIET`
  (default 20s) with nothing answered. A platform that is still resolving names
  is throttling some of its exits, not refusing the list, and pausing it there
  throws away every name it would have answered. Measured through one rotating
  endpoint: discord answered `429 retry_after 1979s` on some requests while other
  requests through the same endpoint were resolving names, and the pause that
  followed took those 1979 seconds off a run that was still working — 15 minutes,
  bounded only by `SNIPE_PLATFORM_PAUSE_MAX`, which is the shape of *it started
  fast and then slowed to nothing*. Same code path, refusals arriving 60% of the
  time with successes in between: **1 verdict and a stall before, 99 verdicts and
  all 400 checks after.** Measured live on guns.lol: 46 refusals out of 1,036 answers is 4.4%
  and is a healthy run — counting raw refusals alone paused the platform every
  few hundred checks. A blanket throttle reads ~100% and still pauses at once.
- A refused name is retried once by default (`SCAN_RETRIES=1`). Through a
  rotating endpoint the retry lands on a different exit IP and most transient
  401/403s resolve into a real verdict.
- A proxy is retired for refusals only when refusals dominate its own answers
  (`SNIPE_PROXY_BLOCK_RATE`) **and** it is not the last live proxy. One rotating
  endpoint measured at 105 refusals in 594 answers (17.7%) was retired by the
  raw count of 40 and ended the sweep; its own panel showed the traffic. A host
  that is genuinely blanket-blocked reads ~100% and is still retired.

### The pacer: the platform that refuses gets asked slower, not dropped

Sitting a platform out loses its names, and hammering a refusing platform is
what produced the refusals in the first place. So between those two extremes a
platform that keeps refusing is now **paced down**: over a window of
`SNIPE_PLATFORM_PACE_WINDOW` (12) answers, refusals at or above
`SNIPE_PLATFORM_PACE_RATE` (0.3) **halve** that platform's in-flight allowance,
down to a floor of `SNIPE_PLATFORM_MIN_PARALLEL` (1); a window at or under
`SNIPE_PLATFORM_PACE_RECOVER` (0.1) grows it back by one. Quick to cut, slow to
grow is what stops a platform oscillating between throttled and hammered.
Pacing never drops a name — the checks queue up, they do not disappear — and the
dashboard shows which snipers are cut and by how much, live, so a slow platform
looks slowed rather than broken.

Measured on the pacer in isolation: at 50% refusals the ceiling walks 16 → 8 → 4
→ 2 → 1, and at 0% it climbs back to 16, with no name skipped in either case. It
is the same principle as the pause below — a refusal is a rate, not a verdict —
applied one level down, per platform instead of per run.

**guns.lol and Instagram still need residential-ish exits.** Both sit behind a
challenge that refuses datacenter ranges outright — the 403 is the exit IP, not
your tooling. A global *datacenter* rotating proxy will get 403s from them no
matter what; the run now keeps going and reports it instead of dying on it.

**A name the pool could not carry is not a result.** When every proxy is
retired, the checks that never went out are not filed as `error` rows — a run
does not get to report a failure for a name it never asked about. They are
counted as `unattempted` (`proxy_misses` covers the narrower case of a name the
pool refused when it was handed one) and the run stops with the reason. Filing
them as errors is what filled the results table with hundreds of "proxies
exhausted" lines and made a dead list look like a broken scanner.

## A run outlives the tab

The sweep used to be the HTTP response. Closing the tab, backgrounding the
browser or losing signal cancelled it, so the run — and every name it had not
reached yet — went with the reader. What a run needed was to not be a reader.

Now the run is a task on the server, and the response is only a window on it:

- **Closing the tab does not stop the sweep.** The task owns the checks; ending
  a reader ends nothing else. Verified by killing the reading client mid-run and
  watching the counters keep climbing with nobody attached.
- **Coming back reattaches.** Every line is kept in a bounded buffer, and
  `GET /runs/{id}/stream` replays what you missed and then follows live. A tab
  that was away for a minute gets the minute, not a gap — verified at 912 lines
  replayed, and at 4,070 lines on a reattach that then continued the run.
- **Only Stop, or a dead pool, ends it.** `POST /runs/stop` (or the sidebar's
  Stop button) stops it; independently, a run whose pool is fully retired stops
  with its reason instead of reporting the remainder as errors.
- **One run per kind** is enforced with a `409` that names the run already in
  flight, so a second tab cannot reset the first one's retirements and strikes
  out from under it — it reattaches to it instead. A finished run stays readable
  for `RUN_TTL` (900 s) so a phone that was asleep still finds its results.

Progress is reported against **checks**, not names: a 3-letter sweep over three
platforms is 52,728 checks, and reporting it against 17,576 names showed 0% on a
run that was a third done.

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

**A working proxy is never retired for a slow target.** A request that opened
its tunnel, went out, and then timed out is the *target* answering late — the
proxy's own panel still shows that request, which is why retiring on it produced
the classic false verdict: "the list is dead" about a list that was working and
visibly receiving traffic. Only the connect phase counts against a proxy now
(`ProxyError`, `ConnectError`, `ConnectTimeout`, a SOCKS handshake failure, or a
`407` from the proxy itself); a read timeout or a malformed answer rests the host
briefly and is retried on another, and is never a strike.

**And a run starts clean.** The pool object is cached so its warm tunnels survive
between batches, but one run's retirements used to survive with it — a proxy
written off at the tail of a bad run still read as retired at the head of the
next one, so fresh names answered "the list is dead" until the 300-second cache
expired. Every run now resets the per-run bookkeeping (retirements, strikes,
cooldowns, block tallies) while keeping the connections.

**And it is judged on a settled sample.** A failure that is fast and an answer
that is slow means the *first* outcomes to come back are the failures. At
concurrency 64 on a host dropping a third of its tunnels, the first six
completions were all drops and the pool was written off 0.05 s into a sweep it
could have carried: 8 checks, 0 answers, "the list is dead". A proxy is now only
judged once nothing it was handed is still in flight, so "every attempt failed"
means every attempt rather than every fast one. A host that is simply gone fails
instantly, settles instantly, and is still retired at the same speed.

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
| `GET /` | the console page. The other pages are in the table above — note that `/hits`, `/proxies` and `/settings` as bare paths are the JSON endpoints below, not the pages |
| `GET /info`, `GET /health` | platforms, generation, counters, and the commit this process is running (`build`) |
| `GET /menu` | every bucket with its real size |
| `GET /proxies` `POST` `DELETE` `POST /proxies/clear` `POST /proxies/upload` | the list |
| `POST /scan` | `{buckets:[{kind,length}], platforms, stream}` — the whole selection is swept |
| `POST /snipe` | the same for explicit `usernames` |
| `GET /snipe?username=` | one name |
| `GET /generate` | `letters\|alnum\|numbers\|words` by length |
| `POST /scan/stop` `POST /snipe/stop` `POST /runs/stop` `GET /runs` | the stop path |
| `GET /hits` | found free names, newest first, each with whether a second exit confirmed it |
| `DELETE /hits` | forget them |
| `GET /runs/{id}`, `GET /runs/{id}/stream` | one run's counters, and following a run that is still going |
| `GET /claim` | where to register a name |
| `GET /settings` `POST /settings` `POST /settings/test-webhook` | alerts |

`stream: true` returns NDJSON: `start`, then a `result` line per verdict
(`available`, `taken`, `banned`, `invalid`, `blocked`, `error`, `unanswered`), a
`progress` line as it goes, a `waiting` line when every live proxy is cooling
down, a `heartbeat` line when a slow stretch would otherwise leave the socket
silent for seconds on end (so a phone does not read it as a dropped
connection), and a `done` summary. A run that dies mid-response still ends with
a `done` line carrying the reason, instead of the browser reporting only
`stream error: Load failed`.

A run is stopped with `POST /runs/stop?run_id=`, not by closing the socket: the
socket is only a reader. `GET /runs` lists what is going and what just finished,
`GET /runs/{id}` is one run's counters for a tab that just returned, and
`GET /runs/{id}/stream?since=` replays from its buffer and then follows.

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
| `SHUFFLE_NAMES` | `true` | every run draws its names in a fresh random order |
| `SHUFFLE_WINDOW` | `50000` | names buffered while shuffling (a list that fits is shuffled in full) |
| `MAX_PROXIES` | `100000` | |
| `SCAN_CONCURRENCY` | `256` | checks in flight |
| `SCAN_BUFFER_MAX` | `50000` | largest buffered (non-stream) run |
| `SCAN_TARGET_RATE` | `100` | reported against on the dashboard |
| `SNIPE_PER_PROXY` | `4` | floor for connections per proxy |
| `SNIPE_PER_PROXY_MAX` | `128` | ceiling for one proxy (measured sweet spot for a single endpoint) |
| `TIKTOK_PAGE_FIRST` | `true` | read the profile page first — ~50 KB/check, and the endpoint TikTok does not throttle |
| `TIKTOK_PAGE_TIMEOUT` | `15` | read ceiling for that page |
| `SNIPE_PLATFORM_PAUSE` | `90` | seconds a blocked platform sits out |
| `SNIPE_BLOCK_MEMORY` | `75` | how long one platform's refusal of one proxy is remembered |
| `SNIPE_PLATFORM_BLOCK_MIN` | `3` | refusals before a platform may be sat out |
| `SNIPE_PLATFORM_BLOCK_RATE` | `0.5` | share of a platform's answers that must be refusals |
| `SCAN_RETRIES` | `1` | retries a refused name once, on a fresh exit IP |
| `SNIPE_PLATFORM_WAIT` | `300` | seconds a run waits when every platform is paused |
| `SNIPE_PLATFORM_PACE_WINDOW` | `12` | answers a platform's in-flight allowance is judged on |
| `SNIPE_PLATFORM_PACE_RATE` | `0.3` | refusals share that halves the allowance |
| `SNIPE_PLATFORM_PACE_RECOVER` | `0.1` | refusals share that lets it grow back by one |
| `SNIPE_PLATFORM_MIN_PARALLEL` | `1` | floor the allowance can be cut to |
| `RUN_TTL` | `900` | seconds a finished run stays reattachable |
| `SNIPE_PLATFORM_PAUSE_MAX` | `900` | cap when the platform names a `retry_after` |
| `SNIPE_PROXY_BLOCK_LIMIT` | `40` | blocks before a proxy is retired (see rate) |
| `SNIPE_PROXY_BLOCK_RATE` | `0.8` | share of a proxy's answers that must be refusals to retire it |
| `SNIPE_PROXY_FAIL_LIMIT` | `6` | attempts before a proxy that failed every one is retired, on a settled sample |
| `SNIPE_PROXY_FAIL_RATE` | `0.75` | share of failures that writes a proxy off |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` | `3` / `8` | seconds |
| `SCAN_CONNECT_TIMEOUT` / `SCAN_READ_TIMEOUT` | `2` / `6` | tighter in a scan |
| `MAX_CONCURRENT_RUNS` | `8` | 429 past it |
| `OG_WORDS_FILE` / `OG_WORDS` | — | extra wordlists for `og` |
| `LOG_LEVEL` | `info` | |

## The pages

Seven pages, one job each — the sidebar is navigation, not a set of panels stacked
onto one screen.

| page | what it is for |
| --- | --- |
| `/` | the console: pick snipers and name spaces, start, and watch the verdict feed |
| `/hits/` | every free name found, its confirmation state, a claim link, CSV export |
| `/platforms` | the sniper grid, live per-sniper counters, and how each one is read |
| `/proxies/` | the proxy list |
| `/alerts` | the Discord webhook |
| `/settings/` | browser preferences, server facts, and what the site will not do |
| `/docs` | how a verdict is reached, the run lifecycle, every endpoint |

**A trailing slash marks a page that shares a name with a JSON endpoint.** `/hits`,
`/proxies` and `/settings` are APIs and always were, so their pages wear the slash;
`/platforms`, `/alerts` and `/docs` have no twin. Serving both from one path is how
a page quietly returns JSON, or the reverse.

The console is the phantom-style layout the genre uses — a row of live counters, a
sniper grid, a name-space grid, and a verdict feed — but the palette is graphite
and copper rather than the usual purple neon. Colour is spent only where it carries
meaning: a free name is the only warm thing on the screen, a taken one is grey, and
a refusal is a muted red in the health panel. **The verdict feed shows verdicts** —
`available` and `taken` — with everything else in the health panel next to it.

- **Snipers**, one cell per platform, with the count of free names found on each.
- **Name spaces** multi-select across kind and length, with every cell's real size
  and a running total; a selection too large to build says so while you are still
  choosing, and offers the cap that turns it into a random sample.
- **Stop run**, always in reach, next to the run's real state.

There is no concurrency box and no "hunt" checkbox: the pacer decides how hard to
push each platform, and a run is started or stopped, not tuned mid-flight by
someone guessing at a number.

**What is verified, and how.** The pages are driven in a real headless Chromium:
each one loads with **zero page errors**, every page renders all seven nav links,
clicking each of the seven lands on the URL it claims, and the console renders 30
sniper cells, 16 name-space cells and 10 KPI tiles rather than an empty shell. A
run is then started from the UI, streams verdicts, and stops. At 390x844 the
document has **0px of horizontal overflow**, the nav is a sideways-scrolling strip
with all seven links tappable, and the sniper grid stays two columns.

That check exists because a per-file syntax check cannot see the class of bug it
found: `app.js` declared `const $` at global scope and every page's inline script
declared it too, so the browser threw `SyntaxError: Identifier '$' has already
been declared`, killed the inline script, and **the sidebar rendered zero links**.
The file was scoped in an IIFE and the pages were checked as a browser loads them
from then on. Still unverified by machine: the sound and desktop-notification
toggles, which need a real audio device and a permission prompt.

## Layout

```
server.py      HTTP surface, the run loop's reporting, the hits store, alerts wiring
sniper.py      the proxy pool, the scheduler, and the 30 per-platform checkers
generator.py   buckets, and the random draw over a space
proxies.py     parsing and storing the proxy list
alerts.py      the Discord webhook
static/        the seven pages, app.css and app.js (no build step)
wordlists/     og.txt
```
