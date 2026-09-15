# bahs

FastAPI service that generates Roblox Lua scripts, learns from feedback, and ships a
Roblox executor GUI in [`client.lua`](client.lua). It answers through Hugging Face's
Inference Providers router (any OpenAI-compatible endpoint works) when the token is in
its variables, and through Ollama running in the same project (`qwen2.5-coder:3b`)
otherwise — see [Hosted inference](#hosted-inference-the-fast-answer), which is what you
want if CPU-only inference is taking minutes per request.

The repo builds exactly one thing: the API. Ollama is a stock Docker-image service.

## Layout

| Service  | Source                       | Build                          | Volume                        | Domain            |
| -------- | ---------------------------- | ------------------------------ | ----------------------------- | ----------------- |
| `bahs`   | this repo, branch `main`     | Dockerfile Path = `Dockerfile` | none (stateless)              | yes — the site    |
| `ollama` | Docker Image `ollama/ollama` | none, the image runs as-is     | one, at **`/root/.ollama`**   | none              |

`ollama` is optional: give `bahs` a hosted model (below) and this service can be deleted
along with its volume — the API then has no local weights to pull, load or hold.
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
  `24h`), so the weights sit in RAM for as long as the service runs. Unloading them is
  the slowest part of a cold request — 270s in the log above — and the reason a first
  request after an idle spell looks broken.
- **It is warmed on startup.** After the pull finishes, the API asks for a single token
  *with the same rules prompt a real request uses*, so the weights are loaded and the
  rules are already in Ollama's prompt cache before you ask for anything.
- **Answers are capped.** `MAX_TOKENS` (default `512`) keeps a script from rambling.
- **The page streams.** It calls `/generate/stream`, so code appears as it is written
  instead of after the whole answer, with a running timer.
- **A generation outlives its reader.** The answer is produced by a background job, not
  by the request that asked for it. Generation carries on when the browser goes away,
  and a page that comes back replays the job from its start instead of starting over.
- **The stream never goes quiet.** A stream that sends nothing for the minutes a cold
  model takes is what a proxy or a sleeping phone drops, so the wait is punctuated with
  a heartbeat frame every `HEARTBEAT` seconds (default `5`).
- **The prompt is kept small.** The Luau rules are dense and constant, past examples
  share a hard `EXAMPLE_CHARS` budget (default `500` characters in total, not 800 each),
  failure notes are cut to 120 characters and the page's placeholder note is dropped
  entirely, and `NUM_CTX` is `2048` — prompt tokens cost the same CPU time as generated
  ones.
- **The private network is used.** With no `OLLAMA_URL` set, model traffic never leaves
  Railway; going through the public domain adds a hop and its own timeouts.

### Why a `hi` takes 400 seconds

Your prompt is never just `hi`. The API prepends the whole Luau ruleset as a system
message, which is ~286 tokens before your one word is added, and every one of those
tokens is CPU work before the model emits anything. So the wait is:

| Part | Time | Evidence |
| ---- | ---- | -------- |
| Loading ~2 GB of weights into RAM | ~270s | `llama-server started in 270.36 seconds` |
| Evaluating the rules prompt | minutes | `prompt eval time = 114874.64 ms / 30 tokens (0.26 tokens per second)` |
| Writing the answer | seconds to minutes | `num_predict` capped at `MAX_TOKENS` |

Those two log lines are from your own `ollama` service, and they are the whole answer:
the container is short of RAM and vCPU, so weights are paged back in from the volume and
evaluation runs at a fraction of a token per second. Nothing the API sends can be fast on
a machine like that, which is why the fixes are in this order:

1. **`ollama` service → Settings → Resources: about 4 GB RAM and 2+ vCPU.** A 3B `q4`
   model needs roughly 2.5 GB resident; below that it thrashes and both the load and the
   evaluation slow down together. This is the one change that fixes the 400 seconds.
2. **Keep the model resident.** With RAM for it, the `keep_alive` of `24h` means only the
   first request after a restart pays the load, and the startup warm-up pays it for you.
3. **A smaller model, if the container cannot grow.** `MODEL=qwen2.5-coder:1.5b` needs
   about half the memory and is roughly twice as fast per token, at some cost in Luau
   quality. `qwen2.5-coder:0.5b` is faster again and noticeably worse.
4. **If the warm-up is the thing holding the single slot**, set `WARM_MODEL=off` on
   `bahs` and accept the load on the first request instead.

While a script is being written the page reports the job's real age, so a cold load is
tellable from a hang: the timer keeps moving and the note says `loading
qwen2.5-coder:3b into RAM`.

### If the page says `Load failed`

That wording comes from the browser, not the API: it gave up on a connection that was
quiet for too long, which a phone on a slow network does during a cold model load. The
generation is not lost. Once the page has the job id it reattaches on its own (eight
tries with a growing back-off) and replays the output from the start, and if the browser
was closed or reloaded it reattaches from `localStorage` when the page loads again. The
footer of the page states the same thing, and nothing has to be generated twice.

If a reload still shows nothing to reattach to, the job was not created: check the log
for the `bahs` service — `409 already generating a script` means the previous generation
still holds Ollama's only slot, and `401 missing or invalid API key` means the key typed
into the page does not match `API_KEY` on the service.

### If nothing appears for minutes

The page reports what it is waiting for — `loading qwen2.5-coder:3b into RAM` or
`model ready` — next to a running timer and character count, so a cold model load is
tellable apart from an actual hang. Open the **`ollama`** service → Logs while a script
runs and read the two timings; on a container with real CPU, prompt eval is tens of
tokens per second and generation is single digits, which is a script in 20–40s. Numbers
far off that are the starvation described above, and no API change can help.

For reference, a 3B model wants ~3 GB of RAM to avoid thrashing and at least 2 vCPU.
`MODEL=qwen2.5-coder:7b` is better at Luau but needs several GB of RAM, and if this
container can never be that big, a smaller model set on **`bahs`** (`qwen2.5-coder:1.5b`,
or `0.5b` for speed over quality) is the way to keep it usable.

A second request while one is generating returns `409 already generating a script`
rather than queueing behind it and looking hung. Because the generation runs on its own
job, that slot is held for the whole answer even if every reader has gone: a job that is
still running when you press Generate again is the one thing you cannot start past.
Check `/health` for the two chips that matter, and give the job time — its age is the
timer on the page. If Ollama itself seems stuck, restart the `ollama` service: it serves
one request per model, and an abandoned request can hold that slot until it finishes.

`CHAT_TIMEOUT` (default `600`) is how long the API waits before returning `504`.

## Hosted inference (the fast answer)

CPU-only inference has a floor this API cannot get under: a prompt of a few hundred
tokens takes many minutes to evaluate on a small container, and the answer comes after
that. If you want seconds instead, put a hosted model behind the API and leave the local
weights behind.

1. Get a Hugging Face token with the **Inference Providers** permission — Settings →
   Access Tokens on huggingface.co. A token without that permission authenticates but is
   rejected with `401` on `/chat/completions`.
2. On the **`bahs`** service → Settings → Variables, add **one** variable:

| Variable  | Value        |
| --------- | ------------ |
| `HF_API`  | `hf_...`     |

   A key under any of the names `INFERENCE_KEY`, `HF_API` or `HF_TOKEN` turns hosted
   inference on, and without `INFERENCE_URL` the default endpoint is Hugging Face's
   router (`https://router.huggingface.co/v1`).
3. Redeploy `bahs`. The page's `ollama` chip then reads `not used -- hosted
   (router.huggingface.co)` and the `model` chip names the hosted model, because nothing
   is pulled, loaded or warmed any more. Answers arrive in seconds, and the `ollama`
   service (and its volume) can be deleted.

Two optional variables change where and what it calls:

| Variable           | Default                                 | Notes |
| ------------------ | --------------------------------------- | ----- |
| `INFERENCE_URL`    | `https://router.huggingface.co/v1`      | Any OpenAI-compatible `/chat/completions` host |
| `INFERENCE_MODEL`  | `Qwen/Qwen2.5-Coder-32B-Instruct`       | Router model id, optionally with a provider suffix |

Good router models to try: `Qwen/Qwen2.5-Coder-32B-Instruct` (the default, fast and
code-specialised, the best fit for Luau), `Qwen/Qwen3-Coder-480B-A35B-Instruct` (stronger
and heavier), `openai/gpt-oss-120b`, `zai-org/GLM-4.5`. A provider suffix such as
`:baseten` or `:ovhcloud` pins one backend; without it the router picks.

Hosted inference is used by both API surfaces: the page's `/generate/stream` jobs and
`client.lua`'s blocking `/generate`. With a hosted model the single-slot `409 already
generating a script` guard is skipped as well, since there is no local model to queue
behind, so several scripts can be written at once. The token stays in the service's
variables: it is never written into the page, and `API_KEY` still gates who can call the
API.

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
| POST   | `/generate` | `{ "prompt": "...", "temperature": 0.7 }`; blocks until the script is ready |
| POST   | `/generate/stream` | Same body; starts a job and returns `{"job": "ab12..."}` at once  |
| GET    | `/generate/stream/{job}` | NDJSON frames for that job — `replay`, `beat`, `t`, then `done` or `error`. Open it again after a drop and it replays the job from its start |
| POST   | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }`           |

Once `API_KEY` is set on the service, `/generate`, `/generate/stream` and `/feedback`
require it; `/`, `/health` and `/docs` stay open. Scripts and feedback live in Postgres
(`scripts` table, created on first use). The site is the two-step streaming flow: the
POST only starts the job, so it can never hang on a slow model, and generation keeps
going whether or not a browser is still reading it. `client.lua` uses the blocking
`/generate` and needs no change.

## Using the site

Open the `bahs` domain, paste the API key if the page asks for it (the field only
appears when the service requires one), describe the script, and hit **Generate**. The
script arrives as it is written, and if the connection drops the page reattaches to the
job that is still running rather than making you wait for a second generation. Once you
have run it in the executor, mark it **works** or **broken**: working scripts are fed
into later prompts as examples, broken ones as mistakes to avoid. The chips in the
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
| `MODEL`        | `qwen2.5-coder:3b`                     | Pulled by the API into Ollama's volume; ignored when hosted inference is configured |
| `OLLAMA_URL`   | `http://ollama.railway.internal:11434` | Baked into the `bahs` image. Leave it unset to keep model traffic on the private network. A trailing slash or a bare host is tolerated |
| `API_KEY`      | —                                      | When set, `/generate` and `/feedback` need `X-API-Key` |
| `KEEP_ALIVE`   | `24h`                                  | How long Ollama holds the model in RAM between requests; unloading costs a multi-minute reload |
| `MAX_TOKENS`   | `512`                                  | Cap on answer length; shorter answers finish sooner |
| `NUM_CTX`      | `2048`                                 | Context window; smaller processes faster |
| `WARM_MODEL`   | `on`                                   | Loads the model at startup; `off` on a container too small to run it |
| `CHAT_TIMEOUT` | `600`                                  | Seconds a job waits for its answer before giving up on it |
| `HEARTBEAT`    | `5`                                    | Seconds of silence between keep-alive frames on `/generate/stream/{job}` |
| `JOB_TTL`      | `3600`                                 | Seconds a finished job stays readable, so a late page can still reattach |
| `EXAMPLE_CHARS` | `500`                                 | Total characters of past scripts allowed in a prompt; they cost minutes on CPU |
| `HF_API` / `INFERENCE_KEY` / `HF_TOKEN` | —                        | Any of these being set turns on hosted inference instead of Ollama |
| `INFERENCE_URL` | `https://router.huggingface.co/v1`      | Endpoint used when a hosted key is set; any OpenAI-compatible host |
| `INFERENCE_MODEL` | `Qwen/Qwen2.5-Coder-32B-Instruct`     | Model id sent to `INFERENCE_URL` when hosted inference is on |
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
