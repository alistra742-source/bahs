# bahs — a bridge to Qwen

This service is a **bridge**. It holds one secret — your Qwen access token — turns
[`qwen-api`](https://github.com/encryptarun/qwen-api) into endpoints anything can call,
and exposes them OpenAI-compatible, so the site, `client.lua` and any OpenAI client all
go through it:

```
            chat.qwen.ai                        Railway -> bahs                   callers
   ┌───────────────────────────┐        ┌───────────────────────────────┐    ┌──────────────────┐
   │  Qwen web API (no API key)│◀──────▶│  qwen-api proxy (QWEN_URL)    │    │  the site  /     │
   │  authenticated by the     │        │  OpenAI-compatible /v1/...    │◀──▶│  client.lua      │
   │  token in your browser    │        │  validates QWEN_TOKEN         │    │  any OpenAI SDK  │
   └───────────────────────────┘        └───────────────────────────────┘    └──────────────────┘
                                          ▲ this repo = the bridge
```

Why the extra hop is worth it:

- **The Qwen token never leaves the server.** It is the key to a whole Qwen account, so a
  browser page and a Roblox client must never hold it. Callers authenticate with
  `API_KEY` instead; the token is only ever put on the request this service makes.
- **Everything qwen-api does still works** through the bridge: streaming, `thinking_mode`
  / `reasoning_effort`, web search with citations, tool calling, image/video models, and
  the hidden continuation metadata that makes follow-up turns continue an existing chat.
- **Nothing to run.** No weights, no GPU, no volume, no Ollama service, no model size
  limit — Qwen3.8-Max answers instead of a 3B model on a starved container.

## What you need to do

### 1. Get your Qwen access token

Sign in at [chat.qwen.ai](https://chat.qwen.ai), open the browser console (F12 →
Console), paste this and run it — it copies your token to the clipboard:

```js
(function(){const t=localStorage.getItem("token");if(!t){alert("not logged in");return}navigator.clipboard.writeText(t).then(()=>alert("token copied")).catch(()=>prompt("token:",t))})();
```

To check it by hand, from a terminal (or /health on the service does this for you):

```sh
curl -X POST https://qwen.aikit.club/validate -H "Content-Type: application/json" \
  -d '{"token": "YOUR_QWEN_ACCESS_TOKEN"}'
```

### 2. Put it in the `bahs` service's variables

Railway → the **`bahs`** service → **Variables**:

| Variable | Value |
| --- | --- |
| `QWEN_TOKEN` | the token you just copied |
| `API_KEY` | *recommended*: any password you invent — callers (the page, `client.lua`) must send it. Without it the endpoints are open to anyone who finds the URL |
| `QWEN_MODEL` | optional, default `qwen3.8-max` |

Redeploy. That is the whole setup — the bridge is already built.

### 3. Check it came up

Open the `bahs` domain. The header shows five chips; you want:

```
api online · bridge qwen.aikit.club · token token accepted · model qwen3.8-max
```

`token rejected` or `cannot reach …` is the one to care about: the first means the token
is stale or was copied wrong (get a new one and update `QWEN_TOKEN`), the second means
the proxy is unreachable from Railway.

### 4. Tidy up

- **Delete the `ollama` service and its volume** in Railway if they are still there —
  nothing in this repo references a local model any more.
- **Postgres is optional.** Keep the plugin (and `DATABASE_URL`) if you want the page's
  *works* / *broken* buttons to teach later prompts; without it everything still works and
  the page says the script was not saved.
- If you had `HF_API`, `INFERENCE_URL`, `INFERENCE_MODEL`, `MODEL`, `OLLAMA_URL`,
  `KEEP_ALIVE`, `NUM_CTX` or `WARM_MODEL` on the service, they are ignored — delete them.

### 5. Refresh the token when it expires

Qwen tokens are session tokens and stop working after a while (weeks, not forever). The
symptom is a `token rejected` chip and a `401` on every generation. Fix = repeat step 1
and update `QWEN_TOKEN`. Nothing else changes.

## The bridge

### `POST /v1/chat/completions`

OpenAI-shaped, stream or not, with the Qwen token added server-side. Send whatever
qwen-api accepts: `messages`, `model` (optional — the service default is used if you omit
it), `temperature`, `max_tokens`, `stream`, `tools`, `web_search_options`,
`reasoning_effort`, `thinking_mode`.

```sh
curl -N https://<your-bahs-domain>/v1/chat/completions \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-max","stream":true,
       "messages":[{"role":"user","content":"write a Luau kill aura with a toggle"}]}'
```

Any OpenAI SDK works — only the base URL and key change:

```python
from openai import OpenAI
client = OpenAI(base_url="https://<your-bahs-domain>/v1", api_key="<API_KEY>")
print(client.chat.completions.create(
    model="qwen3.8-max",
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

### `GET /v1/models`

The model ids this bridge can reach, straight from qwen-api. The site's dropdown is built
from this list.

| Model | Notes |
| --- | --- |
| `qwen3.8-max` | default: strongest, thinking, web search, tools |
| `qwen3-coder-plus` | code-specialised, fastest for Luau |
| `qwen3.7-plus`, `qwen3.6-plus`, `qwen3.5-plus` | cheaper/faster general models |
| `qwen3.5-omni-plus` | audio + image input |
| `qwen-image`, `qwen-video` | image and video generation |
| `qwen-web-dev`, `qwen-full-stack`, `qwen-slides`, `qwen-deep-research` | specialised flows |

### The Luau script flow

The site and `client.lua` use these instead of raw chat, because a real request is
wrapped in the Luau rules and the answer is stripped of markdown fences:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/generate` | `{ "prompt": "...", "model": "..." }`; blocks until the script is ready (`client.lua`) |
| `POST` | `/generate/stream` | same body; starts a job and returns `{"job": "ab12..."}` at once |
| `GET` | `/generate/stream/{job}` | NDJSON frames for that job — `replay`, `beat`, `t`, then `done` or `error`. Open it again after a drop and it replays from the start |
| `POST` | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }` |
| `GET` | `/health` | always `200`; reports the bridge, the token and Postgres |
| `GET` | `/` | the site |
| `GET` | `/docs` | interactive API docs |

Feedback is what shapes later prompts: a script marked **works** is reused as an example
for similar requests, and a **broken** one as a mistake to avoid (both capped by
`EXAMPLE_CHARS`, so an old answer can never grow a new prompt).

## Errors, and what each one means

| What you see | What it means |
| --- | --- |
| `token rejected` chip, `401 QWEN_TOKEN was rejected` | the token is stale or wrong — repeat step 1 and update `QWEN_TOKEN` |
| `503 the bridge is not configured` | `QWEN_TOKEN` is missing on the service |
| `404 … is not a model this endpoint serves` | `QWEN_MODEL` isn't served — pick one from `/v1/models` |
| `429 qwen-api is rate limiting` | too many calls; wait, or use another model |
| `qwen-api is failing (5xx …)` | the proxy (or the public instance) is down; retry, or point `QWEN_URL` at your own deployment |
| `401 missing or invalid API key` | the key typed into the page / used by `client.lua` does not match `API_KEY` |

Everything the proxy or Qwen rejects is passed through with its own wording, on the error
frame, in the chip and in the service log (`[job] <id> failed: …`).

## Long generations, dropped phones

A generation is a **job**, not a request:

- `POST /generate/stream` only *starts* it and returns an id immediately, so the request
  that asked for the script is never the thing waiting on Qwen.
- `GET /generate/stream/{job}` streams the output, sends a heartbeat frame every
  `HEARTBEAT` seconds of silence, and **replays from the start** whenever it is opened
  again — a dropped connection costs nothing.
- The page reattaches on its own (with back-off) and, if it was closed entirely, picks
  the job back up from `localStorage` when it reopens. Jobs stay readable for `JOB_TTL`.

`thinking_mode` is `fast` by default (answer straight away). `auto` or `thinking` makes
Qwen reason first, and its reasoning arrives as `reasoning_content`, which the script flow
ignores. Reasoning tokens count towards `MAX_TOKENS`, so raise `MAX_TOKENS` if you turn it
on.

## Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `QWEN_TOKEN` | — | **required**: Qwen access token from chat.qwen.ai. Also read as `QWEN_API_KEY` or `QWEN_ACCESS_TOKEN` |
| `QWEN_URL` | `https://qwen.aikit.club/v1` | the qwen-api instance to bridge to (any OpenAI-compatible host) |
| `QWEN_MODEL` | `qwen3.8-max` | default model; callers can override per request |
| `QWEN_THINKING` | `fast` | `fast` \| `auto` \| `thinking` |
| `API_KEY` | — | when set, everything except `/`, `/health` and `/docs` needs `X-API-Key` |
| `MAX_TOKENS` | `512` | cap on answer length (reasoning included) |
| `CHAT_TIMEOUT` | `300` | seconds a job waits on Qwen before giving up |
| `HEARTBEAT` | `5` | seconds of silence between keep-alive frames |
| `JOB_TTL` | `3600` | seconds a finished job stays readable |
| `EXAMPLE_CHARS` | `500` | total characters of past scripts allowed in a prompt |
| `TOKEN_CHECK_TTL` | `60` | how often `/health` may re-validate the token |
| `DATABASE_URL` | — | optional Postgres; only the feedback memory needs it |
| `PORT` | `8000` | injected by Railway; the API binds it |

## Running it locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
QWEN_TOKEN=<your token> API_KEY=local uvicorn server:app --port 8000
```

`/` and `/health` report `"database": false` while Postgres is unreachable, and
generations still work — only feedback is skipped. With no `QWEN_TOKEN`, `/health` reports
the bridge as degraded and every generation is refused with that reason instead of an
opaque failure.

## If you would rather host the proxy yourself

`qwen-api` is a Cloudflare Worker; the public instance is shared and rate limited. Deploy
the repository to your own Workers account and set `QWEN_URL` to its `/v1` (and nothing
else changes). If your instance also drops the Qwen token into the requests, this bridge
still works exactly the same — it sends `Authorization: Bearer $QWEN_TOKEN` on every call.
