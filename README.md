# bahs

FastAPI service that turns a description into a Roblox Luau script, learns from
feedback, and ships a Roblox executor GUI in [`client.lua`](client.lua).

Models are served by Hugging Face's [Inference
Providers](https://huggingface.co/docs/inference-providers) router, so the repo builds
exactly one thing: the API. There is no local model, no GPU and no volume — a script is
written in seconds and the container stays small.

## Layout

| Service  | Source                   | Build                          | Volume           | Domain         |
| -------- | ------------------------ | ------------------------------ | ---------------- | -------------- |
| `bahs`   | this repo, branch `main` | Dockerfile Path = `Dockerfile` | none (stateless) | yes — the site |
| Postgres | Railway plugin           | —                              | (its own)        | none           |

Scripts and feedback live in Postgres (`scripts` table, created on first use). Nothing
else is required: no Ollama service, no model volume, no `ollama/ollama` image.

## Setting up the `bahs` service

1. **Settings → Source** = GitHub repo, branch `main`.
2. **Settings → Build → Dockerfile Path** = `Dockerfile`.
3. **Settings → Variables**:
   - `DATABASE_URL` — **Add Reference → Postgres → `DATABASE_URL`**.
   - `HF_API` — a Hugging Face token with the **Inference Providers** permission
     (huggingface.co → Settings → Access Tokens → create one, tick *Make calls to
     Inference Providers*). A token without that permission authenticates and is then
     rejected with `401` on every generation.
4. **Settings → Networking → Generate Domain** — that URL is the site and the value
   `client.lua` needs.
5. No volume, and nothing else to configure.

Two optional variables change where the requests go: `INFERENCE_URL` (default
`https://router.huggingface.co/v1`, any OpenAI-compatible `/chat/completions` host) and
`INFERENCE_MODEL` (default `Qwen/Qwen2.5-Coder-32B-Instruct`). Good router models to try:

- `Qwen/Qwen2.5-Coder-32B-Instruct` — the default: fast, code-specialised, best fit for Luau.
- `Qwen/Qwen3-Coder-480B-A35B-Instruct` — stronger and heavier.
- `openai/gpt-oss-120b`, `zai-org/GLM-4.5` — strong general models.

A provider suffix pins one backend (`Qwen/Qwen3-Coder-480B-A35B-Instruct:baseten`); without
it the router picks. An unknown model id comes back as HTTP `404` with Hugging Face's own
message.

## How a generation works

- **It is a job, not a request.** `POST /generate/stream` starts the generation and
  returns a job id immediately; the page then reads `GET /generate/stream/{job}`. The
  request that asked for the script is never the thing waiting on the model, so a dropped
  connection cannot take the answer with it.
- **The stream never goes quiet.** Frames arrive as tokens do, and every `HEARTBEAT`
  seconds (default `5`) of silence sends a keep-alive frame, because a silent connection
  is what proxies and sleeping phones drop.
- **A reattach replays.** A reader that comes back — auto-retry, or a reload after the
  page was closed — starts from the job's beginning and rebuilds the same output. Job ids
  are kept in the browser's `localStorage` and jobs stay readable for `JOB_TTL`.
- **Answers are capped** at `MAX_TOKENS` (default `512`) so a script cannot ramble.
- **The prompt is kept small.** The Luau rules are constant and dense, past examples share
  a hard `EXAMPLE_CHARS` budget (default `500` characters in total), and only two failure
  notes are included, trimmed to 120 characters.
- **Requests are parallel.** Nothing is queued behind anything else, so several people can
  generate at the same time.

Feedback is what improves the results: scripts marked **works** are reused as examples for
similar requests, and **broken** ones as mistakes to avoid.

### Why there is no local model

It used to run `qwen2.5-coder:3b` in an Ollama service in this project. On a CPU-only
container that model evaluated the ~430-token prompt at about **0.26 tokens per second**,
so a request took 400–1100 seconds before the first character of the script appeared, and
browsers gave up long before that. A hosted model answers in seconds for the same prompt,
and the API container no longer needs the RAM to hold weights.

## If something goes wrong

### The page says `Load failed`

That wording comes from the browser, not the API: it gave up on a connection that was
quiet for too long. The generation is not lost. The page reattaches on its own (eight
tries with a growing back-off) and replays the output from the start; if the browser was
closed or reloaded, it reattaches from `localStorage` when the page loads again.

If a reload shows nothing to reattach to, the job was never created. Check the
**`bahs`** service log, and the error shown in the page:

| What you see | What it means |
| ------------ | ------------- |
| `503 inference is not configured` | `HF_API` is missing on the service |
| `inference (...) 401` | the token is wrong, or lacks the Inference Providers permission |
| `inference (...) 404` | `INFERENCE_MODEL` is not a model the router serves |
| `inference (...) 429` | rate limited by the provider — retry, or switch `INFERENCE_MODEL` |
| `401 missing or invalid API key` | the key typed into the page does not match `API_KEY` on the service |

Anything the provider rejects is passed through with its own message, so the error frame
usually says exactly what is wrong.

### The chips in the header

The page polls `/health` every 8 seconds. `api` is this service, `postgres` is the
database, `inference` is the Hugging Face token and endpoint, and `model` names the model
that is answering. A red `inference` chip means `HF_API` is not set.

## Endpoints

| Method | Path        | Purpose                                                    |
| ------ | ----------- | ---------------------------------------------------------- |
| GET    | `/`         | The site: type a prompt, get a script, mark it works/broken |
| GET    | `/health`   | Always `200`; body reports Postgres and inference state     |
| POST   | `/generate` | `{ "prompt": "...", "temperature": 0.7 }`; blocks until the script is ready |
| POST   | `/generate/stream` | Same body; starts a job and returns `{"job": "ab12..."}` at once |
| GET    | `/generate/stream/{job}` | NDJSON frames for that job — `replay`, `beat`, `t`, then `done` or `error`. Open it again after a drop and it replays the job from its start |
| POST   | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }`           |

Once `API_KEY` is set on the service, `/generate`, `/generate/stream` and `/feedback`
require it; `/`, `/health` and `/docs` stay open.

## Using the site

Open the `bahs` domain, paste the API key if the page asks for it (the field only appears
when the service requires one), describe the script, and hit **Generate**. The script
arrives as it is written, and if the connection drops the page reattaches to the job that
is still running rather than making you wait for a second generation. Once you have run it
in the executor, mark it **works** or **broken**: working scripts are fed into later
prompts as examples, broken ones as mistakes to avoid.

`client.lua` calls the same endpoints and sends the same key — paste it into `API_KEY`
near the top of that file.

## API key

Set `API_KEY` on the **`bahs`** service (Settings → Variables) to lock down the model
endpoints. Callers then send it as a header:

```
X-API-Key: <the value>            # or: Authorization: Bearer <the value>
```

With `API_KEY` unset the endpoints are open, which is what a fresh deploy or a local run
gets. Neither key is ever written into the page: the site stores the API key in that
browser's localStorage and sends it with each request, and the Hugging Face token stays in
the service's variables and never reaches a browser.

## Environment variables

| Variable       | Default                                | Notes                                            |
| -------------- | -------------------------------------- | ------------------------------------------------ |
| `PORT`         | `8000`                                 | Injected by Railway; the API binds it             |
| `HF_API`       | —                                      | Hugging Face token with the Inference Providers permission. Also read as `INFERENCE_KEY` or `HF_TOKEN` |
| `INFERENCE_URL` | `https://router.huggingface.co/v1`    | Any OpenAI-compatible `/chat/completions` host    |
| `INFERENCE_MODEL` | `Qwen/Qwen2.5-Coder-32B-Instruct`    | Router model id, optionally with a provider suffix |
| `API_KEY`      | —                                      | When set, `/generate`, `/generate/stream` and `/feedback` need `X-API-Key` |
| `MAX_TOKENS`   | `512`                                  | Cap on answer length                              |
| `CHAT_TIMEOUT` | `600`                                  | Seconds a job waits for the provider before giving up |
| `EXAMPLE_CHARS` | `500`                                 | Total characters of past scripts allowed in a prompt |
| `HEARTBEAT`    | `5`                                    | Seconds of silence between keep-alive frames on `/generate/stream/{job}` |
| `JOB_TTL`      | `3600`                                 | Seconds a finished job stays readable, so a late page can still reattach |
| `DATABASE_URL` | —                                      | Injected by the Railway Postgres plugin           |
| `POSTGRES_URL` | —                                      | Older alias, accepted as a fallback               |

## Local run

Needs a Postgres to point at (the `scripts` table is created on first request) and a
Hugging Face token:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
HF_API=hf_xxxxxxxx \
uvicorn server:app --port 8000
```

`/` and `/health` report `"database": false` while Postgres is unreachable, and
`/generate` returns `503` instead of crashing. With no `HF_API` the page still loads and
`/health` reports `"inference": false`, with generations refused and a message saying so.
