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
- No login: the API is open, so the dashboard needs no key.

### Why a cycle is fast

Three things keep the wall clock down, in order of impact:

1. **Dead proxies are not re-checked.** A proxy that has never once answered is
   dropped on its first failure and remembered for `FAIL_COOLDOWN`. Free lists
   hand back the same dead addresses on every scrape, so without this every
   cycle re-checked the same ~20k corpses; with it, a repeat cycle validates
   only the alive set plus genuinely new hosts (measured: 10 offered → 1
   checked on a repeat scrape).
2. **One round trip per alive proxy.** The judge and the three platform probes
   start together; a proxy the judge rejects has its probes cancelled rather
   than awaited.
3. **Short connect timeouts and high concurrency** — see `CONNECT_TIMEOUT` and
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
```

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

# force a refresh off-schedule
curl -s -X POST "https://<domain>/refresh"

# manage the validated set
curl -s -X DELETE "https://<domain>/proxies?proxy=http%3A%2F%2F1.2.3.4%3A8080"
curl -s -X POST "https://<domain>/proxies/purge?scope=dead"
```

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
