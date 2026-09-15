# bahs

A bridge to Qwen, and one chat on top of it.

```
chat.qwen.ai  <-  qwen-api (OpenAI-shaped)  <-  bahs  <-  the page, client.lua, anything OpenAI
                                                  ^
                                      holds QWEN_TOKEN, callers use API_KEY
```

chat.qwen.ai has no public API. [`qwen-api`](https://github.com/encryptarun/qwen-api) turns
it into OpenAI-compatible endpoints, but it authenticates with the Qwen *access token* that
chat.qwen.ai keeps in your browser (`localStorage.token`). That token is the key to a whole
Qwen account, so it lives in **this** service's variables and never in a page or a Roblox
script. Callers talk to `bahs` with an API key instead.

There is no model here: nothing is pulled, loaded or warmed, and there is no database. The
conversation lives in the browser and is sent back with every turn.

## The flow

1. **Every turn is the whole conversation.** The page keeps the turns -- what you asked and
   what came back -- and sends them with the next request, so a follow-up is answered in the
   same chat by a model that can see what it already said. It survives a reload of the page;
   **new chat** drops it. `client.lua` keeps its own list the same way.
2. **Every question is addressed.** What you type is sent as `Hy kanha <your question>` --
   the greeting is applied by the service (and by the page, so the bubble shows exactly what
   went out) and never doubled. Set `GREETING` to `""` on the service to send messages
   untouched.
3. **One model, one mode.** Every generation goes to `QWEN_MODEL` with `thinking_mode:
   "fast"`, whoever is asking and whatever they asked for.
4. **A dropped connection is not a lost answer.** A generation is a server-side job, so a
   phone that gives up reattaches and replays instead of starting over.

## Endpoints

| Endpoint | What it is |
| --- | --- |
| `POST /chat/stream` | `{messages: [{role, content}, ...]}` -> `{job, model, thinking, turns}`. Starts the turn, returns at once |
| `GET /chat/stream/{job}` | NDJSON for that turn: `{replay}`, `{t}` pieces, `{beat}` heartbeats, then `{done, text}` or `{error}` |
| `POST /generate` | blocking, no history -- what `client.lua` uses: `{prompt}` -> `{text, code, model}` |
| `POST /generate/stream` | the same as `/chat/stream`, one prompt, no conversation |
| `POST /v1/chat/completions` | OpenAI-compatible. The newest user turn gets the greeting, the model and thinking are pinned, everything else (`tools`, `web_search_options`, `reasoning_effort`, `temperature`, `stream`) passes through untouched |
| `GET /v1/models` | `QWEN_MODEL` first, then whatever else the proxy serves |
| `GET /health` | bridge, token, model, mode, greeting, and the last failure |
| `GET /` | the chat page |

An OpenAI client needs two lines changed:

```python
client = OpenAI(base_url="https://<your-domain>/v1", api_key="<API_KEY>")
```

## Setup on Railway

`bahs` -> Settings -> Variables:

| Variable | Value |
| --- | --- |
| `QWEN_TOKEN` | a Qwen access token: sign in at chat.qwen.ai, DevTools console, `localStorage.token` |
| `API_KEY` | *recommended* -- any password you invent. Without it, the API is open to whoever finds the URL |
| `QWEN_MODEL` | optional, default `qwen3.8-max` |

Redeploy. The page should read
`api online · bridge qwen.aikit.club · token token accepted · model qwen3.8-max ·
mode fast · Hy kanha`.

Then tidy up: delete any `ollama` service and its volume, and delete the Postgres service if
you made one -- nothing here uses it. `HF_API`, `INFERENCE_*`, `MODEL`, `OLLAMA_URL`,
`KEEP_ALIVE`, `NUM_CTX`, `WARM_MODEL` and `DATABASE_URL` are all dead now.

## Other variables

| Variable | Default | Notes |
| --- | --- | --- |
| `QWEN_URL` | `https://qwen.aikit.club/v1` | point it at your own qwen-api deployment if the public one is rate limited |
| `QWEN_MODEL` | `qwen3.8-max` | the only model used |
| `QWEN_THINKING` | `fast` | forced onto every request; reasoning tokens are billed against `MAX_TOKENS` |
| `GREETING` | `Hy kanha` | put in front of every question; `""` sends it untouched |
| `MAX_TOKENS` | `1024` | ceiling on one answer |
| `HISTORY_MESSAGES` / `HISTORY_CHARS` | `40` / `120000` | how much of a long chat one request may carry; the newest turns always stay |
| `HEARTBEAT` | `5` | seconds of quiet before a heartbeat frame |
| `JOB_TTL` | `3600` | how long a finished answer stays readable |
| `CHAT_TIMEOUT` | `300` | backstop for a provider that hangs |

## Troubleshooting

| What you see | What it is |
| --- | --- |
| `token rejected` chip, `401`s | the Qwen token expired (they last weeks). Copy a fresh one and update `QWEN_TOKEN` |
| `404 ... is not a model this endpoint serves` | `QWEN_MODEL` is retired; pick the proxy's id from `/v1/models` |
| `429` | the shared public qwen-api is rate limiting -- deploy your own and set `QWEN_URL` |
| the answer stops mid-sentence | the phone dropped the connection; reload the page and the job replays |
| a question never gets an answer | check the chips: the last failure is kept on `bridge` until a generation succeeds, and every failure is printed to the service log as `[job] <id> failed: ...` |

## The in-game client

`client.lua` talks to `/v1/chat/completions`, keeps its own `messages` table so a follow-up
lands in the same chat, and has **ask**, **execute** (runs a fenced ```lua block from the
answer), **new chat** and **copy** buttons. Paste your service domain and `API_KEY` at the
top; leave `API_KEY` empty if you never set one.

## Curl

```bash
curl -s https://<your-domain>/chat/stream \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}]}'
# -> {"job":"...","model":"qwen3.8-max","thinking":"fast","turns":1}

curl -sN https://<your-domain>/chat/stream/<job> -H "X-API-Key: $API_KEY"
```
