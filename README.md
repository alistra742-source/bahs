# bahs

Username availability across **Discord · guns.lol · Instagram · TikTok**, run through a
proxy list you control.

Paste or upload your proxies, paste or generate usernames, press run. The dashboard
streams every verdict as it lands, with a live check rate, an available-only filter and
a Stop button that actually cancels the requests in flight.

There is no scraper and no proxy validator in here. The list you save **is** the pool: a
host that does not work shows up as an error on the name it was used for and is retired
for the rest of the run.

---

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8080
```

Open <http://localhost:8080>. On Railway the `Dockerfile` and `railway.json` are already
wired up; set `STORE_PATH` to a mounted volume to keep the list across deploys.

## Proxy list

Paste one per line, or drop a `.txt` on the box in the dashboard. All of these read
correctly:

```
1.2.3.4:8080                       -> http://1.2.3.4:8080
https://1.2.3.4:8080               -> https://1.2.3.4:8080
socks5://1.2.3.4:1080              -> socks5://1.2.3.4:1080
user:pass@1.2.3.4:8080             -> http://user:pass@1.2.3.4:8080
socks5://user:pass@1.2.3.4:1080    -> socks5://user:pass@1.2.3.4:1080
1.2.3.4:8080:user:pass             -> http://user:pass@1.2.3.4:8080
[2001:db8::1]:8080                 -> http://[2001:db8::1]:8080
proxy.example.com:3128             -> http://proxy.example.com:3128
```

A bare `host:port` is read as `http`. Duplicates are dropped, unreadable lines are counted
and shown. `socks4://` and `socks5://` work because `httpx[socks]` is pinned in
`requirements.txt`.

The list lives in `data/proxies.json` (`STORE_PATH`), written atomically — a crash
mid-write cannot leave a half-written list for a run to read.

## The four checks

Each one is the platform's own availability answer, read off the live site rather than
guessed at. Nothing here logs in, and nothing creates an account.

| platform | request | available means |
|---|---|---|
| **discord** | `POST /api/v9/unique-username/username-attempt-unauthed` — the signup form's own endpoint | `{"taken": false}` |
| **guns.lol** | `GET /{name}`, read until the verdict lands | the default page (no `profile-page-json-ld` marker) |
| **instagram** | `GET /api/v1/users/web_profile_info/?username=` with the web app's `x-ig-app-id` | `404` |
| **tiktok** | `GET /oembed?url=…/@name` | `400 {"message":"Something went wrong","code":400}` |

Statuses you will see: `available`, `taken`, `invalid` (the name itself is not allowed —
e.g. Discord's reserved words), `blocked` (the platform rate-limited or challenged the
request) and `error` (transport, or a response that could not be read).

A `blocked` or `error` says nothing about the name, only about the proxy it went through,
so those are retried on a different host. A definitive answer is never retried, and a
response that cannot be interpreted is reported as an error rather than assumed to be
available.

## API

```
GET    /info                     what this is, and every route
GET    /health                   stored-proxy stats and live runs
GET    /proxies                  the stored list (?limit / ?offset / ?format=txt)
POST   /proxies                  {"text": "...", "mode": "append"|"replace"}
POST   /proxies/upload           same, with the list as the raw request body
DELETE /proxies?proxy=...        remove one
POST   /proxies/clear            empty the list

POST   /snipe                    {"usernames": [...], "platforms": [...], "stream": true}
GET    /snipe?username=...       one name from the query string
POST   /snipe/stop               cancel every in-flight /snipe
POST   /scan                     {"patterns": [...], "platforms": [...], "stream": true}
POST   /scan/stop                cancel every in-flight /scan
GET    /runs  ·  POST /runs/stop runs in flight, and how to stop them

GET    /generate?pattern=letters&length=4&limit=500
POST   /generate                 several patterns at once
GET    /claim?platform=&username= where a free name would be registered
```

`/snipe` and `/scan` answer **NDJSON** when `stream: true` — one verdict per line, then a
`done` line — so the first answer is visible in the first second and a proxy or gateway
never has to hold a whole batch open. `stream: false` buffers the same data into one JSON
document.

Stop is a real halt, not just a closed socket: `POST /snipe/stop` flips an event the
dispatch loop is racing against its in-flight tasks, those tasks are cancelled and awaited,
and only then does the run report `stopped: true`.

```bash
# upload a list, then check a handful of names
curl -X POST --data-binary @proxies.txt 'localhost:8080/proxies/upload?mode=replace'
curl -s 'localhost:8080/snipe?username=nike&platform=tiktok' | jq
```

## Configuration

Everything tunable is an environment variable read in `config.py`.

| variable | default | what it does |
|---|---|---|
| `PORT` / `HOST` | `8080` / `0.0.0.0` | where the service binds |
| `STORE_PATH` | `data/proxies.json` | where the saved list lives (mount a volume here) |
| `MAX_PROXIES` | `100000` | ceiling on the stored list |
| `SNIPE_POOL` | `5000` | how many stored proxies one run rotates over |
| `SNIPE_CONCURRENCY` / `SCAN_CONCURRENCY` | `64` / `256` | checks in flight at once |
| `SNIPE_PER_PROXY` / `SCAN_PER_PROXY` | `4` / `6` | simultaneous requests allowed through one proxy |
| `SNIPE_MAX_CLIENTS` | `512` | warm clients kept open before the least recently used is closed |
| `SNIPE_RETRIES` / `SCAN_RETRIES` | `2` / `0` | retries on another proxy for blocked/errored names |
| `CONNECT_TIMEOUT` / `READ_TIMEOUT` | `3.0` / `8.0` | per-check timeouts |
| `SNIPE_PROXY_COOLDOWN` | `60` | how long a proxy that just failed is rested |
| `SNIPE_PROXY_FAIL_LIMIT` | `6` | failures in one run before a proxy is retired |
| `MAX_CONCURRENT_RUNS` | `8` | runs the server will track at once (429 past it) |
| `SCAN_TARGET_RATE` | `100` | the rate the dashboard reports against |
| `OG_WORDS_FILE` / `OG_WORDS` | — | extra words for the `words` pattern |

## Performance notes

Three things decide how fast a run moves, and none of them is the HTTP client:

1. **The proxies.** A pasted public list is mostly dead within minutes, and a dead host
   costs its connect timeout. `ConnectError`/`ProxyError` columns are what an exhausted
   list looks like, not a bug.
2. **`READ_TIMEOUT`.** A host that accepts TCP and then never answers costs the *read*
   timeout, not the connect timeout. Lower it and runs get faster; raise it and more
   slow-but-alive hosts survive.
3. **Warm clients.** One `httpx.AsyncClient` per proxy keeps the CONNECT tunnel and the
   TLS handshake alive across names, which is the single biggest cost in a batch. They
   share one `ssl.SSLContext`: a fresh context per client costs ~1.25 MB (the CA bundle
   is re-parsed), which at a few hundred clients is enough to OOM the process. Bounded to
   `SNIPE_MAX_CLIENTS` so a six-figure list cannot become a six-figure number of sockets.

## Honest limits

- **Instagram blocks datacenter IPs outright** — from a cloud host every request comes
  back `429` before the proxy list is involved. Its check is the one most dependent on the
  quality of the proxies you supply, and it is the one that could not be verified from a
  datacenter host.
- **TikTok's profile page is useless for this from a datacenter IP**: it answers `200`
  with a generic shell that is byte-for-byte similar whether or not the account exists,
  which would read as "available" for everything. That is why the check is oEmbed, and
  why anything other than its `200`/`400` shapes is reported as `blocked`/`error`.
- **There is no account creation here.** `/claim` copies the name and hands you the
  platform's own registration page. Automating signups is what gets an IP range banned,
  and it is not something this service does.
- **Rate limits are still real.** Rotating proxies spreads requests across IPs; it does
  not make the platforms stop counting. The `blocked` column is the platform saying so.
- Results live in the browser session only. Nothing about a run is persisted.
