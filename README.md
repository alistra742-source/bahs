# bahs

FastAPI service that generates Roblox Lua scripts with Ollama (`qwen2.5-coder:3b`),
learns from feedback, and ships a Roblox executor GUI in [`client.lua`](client.lua).

The repo builds exactly one thing: the API. Ollama is a stock Docker-image service.

## Layout

| Service  | Source                       | Build                          | Volume                        | Domain            |
| -------- | ---------------------------- | ------------------------------ | ----------------------------- | ----------------- |
| `bahs`   | this repo, branch `main`     | Dockerfile Path = `Dockerfile` | none (stateless)              | yes — the site    |
| `ollama` | Docker Image `ollama/ollama` | none, the image runs as-is     | one, at **`/root/.ollama`**   | none              |
| Postgres | Railway plugin               | —                              | (its own)                     | none              |

`bahs` reaches Ollama over private networking at `http://ollama.railway.internal:11434`
and **pulls `MODEL` into Ollama's volume itself** on startup, so the Ollama service needs
nothing but the image and the volume — no start command, no variables, no repo build.

## Setting up the `ollama` service

1. **Settings → Source → Deploy from Docker Image**, image **`ollama/ollama`**.
   It must be the namespaced name. A bare `ollama` resolves to
   `docker.io/library/ollama`, which does not exist — that is the
   `could not be pulled from the registry` failure.
2. **Settings → Volumes → + New Volume**, mount path **`/root/.ollama`**.
   That is the model root the image already uses, so the weights persist across
   redeploys and the ~2 GB download happens once.
3. **Do not** set `OLLAMA_HOST` (the image already binds `0.0.0.0:11434`), **do not**
   set `PORT`, and **do not** set a custom start command.
4. No public domain — leave the service unexposed. Only `bahs` needs a domain.
5. Name it exactly `ollama`: the API resolves it as `ollama.railway.internal`.

## Setting up the `bahs` service

1. **Settings → Source** = GitHub repo, branch `main`.
2. **Settings → Build → Dockerfile Path** = `Dockerfile`.
3. **Settings → Variables → Add Reference → Postgres → `DATABASE_URL`.**
   `OLLAMA_URL` is baked into the image as `http://ollama.railway.internal:11434`, so
   there is nothing else to set unless you rename that service.
4. **Settings → Networking → Generate Domain** — that URL is the site and the value
   `client.lua` needs.
5. No volume.

### `Redirect response '307 Temporary Redirect' for url '...//api/tags'`

`OLLAMA_URL` ends in a slash, so the request path becomes `//api/tags`; Railway's edge
answers that with a 307 to `/api/tags` instead of proxying it. The API now strips
trailing slashes and follows redirects, but the variable is also unnecessary: delete
`OLLAMA_URL` from the `bahs` service and the image's baked-in
`http://ollama.railway.internal:11434` is used, which stays on the private network
(no egress, no public hop, and it works even if the ollama service has no domain).

## Speed

Inference is CPU-only, so these are what actually decide how long a script takes:

- **The model stays loaded.** Every request sends `keep_alive` (`KEEP_ALIVE`, default
  `30m`), so the weights sit in RAM between requests. Reloading them is the slowest
  part of a cold request.
- **It is warmed on startup.** After the pull finishes, the API asks for a single token
  so the model is loaded before you ask for anything.
- **Answers are capped.** `MAX_TOKENS` (default `512`) keeps a script from rambling.
- **The page streams.** It calls `/generate/stream`, so code appears as it is written
  instead of after the whole answer, with a running timer.
- **The prompt is kept small.** The Luau rules are dense and constant, only two past
  examples are reused (truncated to 800 characters), and `NUM_CTX` is `2048` — prompt
  tokens cost the same CPU time as generated ones.
- **The private network is used.** With no `OLLAMA_URL` set, model traffic never leaves
  Railway; going through the public domain adds a hop and its own timeouts.

### If nothing appears for minutes

The page reports what it is waiting for — `loading qwen2.5-coder:3b into RAM` or
`model ready` — next to a running timer and character count, so a cold model load is
tellable apart from an actual hang.

Open the **`ollama`** service → Logs while a script runs and read the two timings. On a
container with real CPU, prompt eval is tens of tokens per second and generation is
single digits — a script in 20–40s. Numbers this far off mean the container is starved,
and no API change can help:

```
llama-server started in 270.36 seconds
prompt eval time = 114874.64 ms / 30 tokens ( 3829.15 ms per token, 0.26 tokens per second)
```

That is 3.8s for *one* prompt token, so a 300-token prompt would need ~19 minutes. The
fix is vCPU/RAM on the `ollama` service (Settings → Resources) — a 3B model wants ~3 GB
of RAM to avoid thrashing and at least 2 vCPU. `WARM_MODEL=off` is worth setting while it
is that slow, since a background warm-up otherwise holds the model's only slot for
minutes before your own request is served. Beyond that, the options are a smaller model
set on **`bahs`**:

- `MODEL=qwen2.5-coder:1.5b` — much faster, noticeably less capable.
- `MODEL=qwen2.5-coder:7b` — better at Luau, needs several GB of RAM.

A second request while one is generating returns `409 already generating a script`
rather than queueing behind it and looking hung. If Ollama itself seems stuck, restart
the `ollama` service: it serves one request per model, and an abandoned request can hold
that slot until it finishes.

`CHAT_TIMEOUT` (default `600`) is how long the API waits before returning `504`.

## Model quality

The system prompt (`RULES` in `server.py`) targets Luau rather than Lua 5.1: `task.*`
threading, cached services, `:FindFirstChild` guards, `pcall` around yields, connection
and instance cleanup on toggle, `Humanoid:MoveTo`/`CFrame`/`Raycast`/`TweenService` over
workarounds, and a keybind toggle for anything that runs continuously. Feedback from the
**works** / **broken** buttons is appended to it, so the results improve as you use it.

## Model pull

Whoever starts first, the API sorts it out: on startup it checks Ollama's `/api/tags`
and, if `MODEL` is missing, calls `/api/pull` and streams the download on a background
thread. Until the weights land, `/` and `/health` report `"model_ready": false` and
`/generate` returns `503`. After that the model is served from the volume.

To use a different model, set `MODEL` on the **`bahs`** service — that is where the
model name is read. Leave the Ollama service untouched.

## Endpoints

| Method | Path        | Purpose                                                    |
| ------ | ----------- | ---------------------------------------------------------- |
| GET    | `/`         | The site: type a prompt, get a script, mark it works/broken |
| GET    | `/health`   | Always `200`; body reports Postgres and Ollama state        |
| POST   | `/generate` | `{ "prompt": "...", "temperature": 0.7 }`                  |
| POST   | `/generate/stream` | Same, but streams NDJSON frames while the answer is written |
| POST   | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }`           |

Once `API_KEY` is set on the service, `/generate` and `/feedback` require it; `/`,
`/health` and `/docs` stay open. Scripts and feedback live in Postgres (`scripts`
table, created on first use).

## Using the site

Open the `bahs` domain, paste the API key if the page asks for it (the field only
appears when the service requires one), describe the script, and hit **Generate**. Once
you have run it in the executor, mark it **works** or **broken**: working scripts are
fed into later prompts as examples, broken ones as mistakes to avoid. The chips in the
header poll `/health`, so the page also tells you whether Postgres, Ollama and the model
are up.

`client.lua` calls the same endpoints and sends the same key — paste it into `API_KEY`
near the top of that file.

## API key

Set `API_KEY` on the **`bahs`** service (Settings → Variables) to lock down the model
endpoints. Callers then send it as a header:

```
X-API-Key: <the value>            # or: Authorization: Bearer <the value>
```

With `API_KEY` unset the endpoints are open, which is what a fresh deploy or a local run
gets. The key is never written into the page: the site stores it in that browser's
localStorage and sends it with each request.

## Environment variables

| Variable       | Default                                | Notes                                            |
| -------------- | -------------------------------------- | ------------------------------------------------ |
| `PORT`         | `8000`                                 | Injected by Railway; the API binds it             |
| `MODEL`        | `qwen2.5-coder:3b`                     | Pulled by the API into Ollama's volume            |
| `OLLAMA_URL`   | `http://ollama.railway.internal:11434` | Baked into the `bahs` image. Leave it unset to keep model traffic on the private network. A trailing slash or a bare host is tolerated |
| `API_KEY`      | —                                      | When set, `/generate` and `/feedback` need `X-API-Key` |
| `KEEP_ALIVE`   | `30m`                                  | How long Ollama holds the model in RAM between requests |
| `MAX_TOKENS`   | `512`                                  | Cap on answer length; shorter answers finish sooner |
| `NUM_CTX`      | `2048`                                 | Context window; smaller processes faster |
| `WARM_MODEL`   | `on`                                   | Loads the model at startup; `off` on a container too small to run it |
| `CHAT_TIMEOUT` | `600`                                  | Seconds to wait for an answer before returning `504` |
| `DATABASE_URL` | —                                      | Injected by the Railway Postgres plugin           |
| `POSTGRES_URL` | —                                      | Older alias, accepted as a fallback               |

## Local run

Needs a Postgres to point at (the `scripts` table is created on first request) and an
Ollama server:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
OLLAMA_URL=http://localhost:11434 \
uvicorn server:app --port 8000
```

Against a local Ollama the API is also what pulls `MODEL` the first time. `/` and
`/health` report `"database": false` while Postgres is unreachable, and `/generate`
returns `503` instead of crashing.
