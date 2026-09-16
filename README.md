# bahs

Two models in a chain, behind one API, with a chat page on top.

```
you -- ask --> bahs -- draft ----------------> Qwen        (qwen-api, thinking off)
                  |
                  +-- review ----------------> DeepSeek V4 Flash (thinking off, search off)
                  |
                  +-- rewrite, same chat ----> Qwen
```

1. **Qwen drafts** an answer to what you asked, in the conversation you are keeping.
2. **DeepSeek reviews** that draft against your request and returns a numbered list of what
   would actually break, what that looks like to the user, and the exact change to make.
3. **Qwen rewrites it** — in the *same conversation the draft was written in*, with the
   reviewer's list pasted in — and that rewrite is what you get. The draft and the review stay
   available under the answer (collapsed), and neither is carried into your next question.

chat.qwen.ai has no public API. [`qwen-api`](https://github.com/encryptarun/qwen-api) turns it
into OpenAI-compatible endpoints using the Qwen *access token* from your browser
(`localStorage.token`). That token is the key to a whole account, so it lives in **this**
service's variables and never in a page or a Roblox script.

There is no model here: nothing is pulled, loaded or warmed, and there is no database. The
conversation lives in the browser and is sent back with every turn.

## What to set on Railway

`bahs` -> Settings -> Variables:

| Variable | Value |
| --- | --- |
| `QWEN_TOKEN` | Qwen access token: chat.qwen.ai -> F12 -> Console -> `localStorage.token` |
| `DEEPSEEK_TOKEN` | DeepSeek **API key** (`sk-...`) from [platform.deepseek.com](https://platform.deepseek.com) |
| `API_KEY` | *optional* — if you set it, the API (`/v1`, `/chat`, `/generate`) requires it. Left unset, the Qwen token is the key. The page never needs one |

Redeploy. The page should read `api online · bridge qwen.aikit.club · token token accepted ·
reviewer key set, model served · model qwen3.8-max · mode fast · Hy kanha`.

### About `chat.deepseek.com`

`DEEPSEEK_TOKEN` has to be an **API key from platform.deepseek.com**, not the `userToken` from
chat.deepseek.com's localStorage. They are different credentials — a chat session token will
come back as `deepseek rejected the key`.

That is not an oversight, it is the shape of the site: chat.deepseek.com is a web app with no
public API, and calling it directly needs

* a signed-in browser session that has already passed its AWS WAF human-check (a
  `cf_clearance` cookie), and
* a **proof of work solved per request**, by executing DeepSeek's own `sha3_wasm_bg.wasm`.

The second can be done in code; the first cannot be done from a datacenter IP at all, which is
what this service runs on. So this service does not pretend to try.

If you want to go through the web chat anyway, it has to be through a bridge that sits in front
of it and speaks OpenAI —
[`xtekky/deepseek4free`](https://github.com/xtekky/deepseek4free) and
[`sums001/Deepseek-API`](https://github.com/sums001/Deepseek-API) both do exactly that, and both
need a real browser once to get the cookie. Run one of those somewhere that can, then:

| Variable | Value |
| --- | --- |
| `REVIEW_URL` | that bridge, e.g. `http://your-host:8000/v1` |
| `DEEPSEEK_TOKEN` | the token/cookie bundle that bridge expects |
| `REVIEW_SHAPE` | `web` — no system role, and the toggles as plain booleans |

`REVIEW_SHAPE=web` folds everything into one prompt with `Send.txt` first, and sends
`thinking: false, search: false`. Anything else keeps the OpenAI shape.

## Search and thinking are never switched on

* The draft is sent with `thinking_mode: "fast"` (`QWEN_THINKING`), so the model answers instead
  of spending the token budget reasoning.
* The reviewer is sent `thinking: {"type": "disabled"}` — or `thinking: false, search: false` in
  the `web` shape. Every toggle the reviewer gets is built in one place (`reviewer_dialect()`),
  and **search is set to false there and cannot be set true anywhere**. Thinking is off unless
  you explicitly set `REVIEW_THINKING=on`. No `tools`, `web_search_options` or search parameter
  is ever attached to a review.
* The `/v1/chat/completions` passthrough is the one place a caller can pass its own
  `web_search_options` / `tools` through — that endpoint is a passthrough to Qwen and does not
  run the chain, so it cannot affect a review.

## The brief (send.txt / Send.txt)

The brief is read once at boot and put **before anything else** in the review request: it is
the first system message (or, in the `web` shape, the very start of the single prompt), ahead of
the request, the draft and the automatic checks. Nothing is prepended to it.

**There are two of them in the repo** — `send.txt` and `Send.txt` — because they differ only in
the case of one letter. That is a trap worth knowing about: a clone on a case-insensitive
filesystem (macOS, Windows) can only hold one of them, and whichever lands second wins. So:

* `send.txt` (the newer one, ~48 KB) is the one used. `Send.txt` is the fallback, not ignored.
* Whichever is in use is named on `/health` as `reviewer.brief`, so it is never a guess, and
  `REVIEW_BRIEF=/app/Send.txt` forces the other one.
* Consider deleting the one you do not want — it removes the ambiguity and the collision.

* It is sent whole. Yours is ~48 KB, which is a real cost on every review — `REVIEW_BRIEF_MAX`
  (60000) is the ceiling, and the size that was actually used is on `/health` as
  `reviewer.brief_chars`.
* A missing file is not fatal — the built-in rubric still applies — but it is reported rather
  than silently skipped. `Dockerfile` copies both into the image; if you edit either on GitHub,
  the service picks it up on the next deploy.
* The built-in rubric is always appended after it, because the review has to come back as
  `VERDICT: OK` or a numbered list (`1. what is wrong | where | why it fails | fix`) for the
  rewrite step to be able to apply it.

## The flow in detail

| Step | Who | What happens |
| --- | --- | --- |
| draft | Qwen | The turn you asked, plus a ceiling of `DRAFT_TOKENS`. The answer is unwrapped from a single ``` fence |
| check | this service | Empty, `finish_reason=length`, `content_filter`, an unterminated fence. A cut-off draft is **refused**, not shipped |
| review | DeepSeek | Your request, the target runtime, the draft (secrets masked), and what the check found. `temperature 0.2`, ceiling `REVIEW_MAX_TOKENS` |
| verdict | this service | `VERDICT: OK` -> the draft ships and no third call is spent. Otherwise the numbered list is capped at `REVIEW_ITEMS` |
| rewrite | Qwen | The *same conversation*, plus the draft as its own assistant turn, plus the list pasted in whole. Asked to return the full script and nothing else |
| guard | this service | If the rewrite comes back empty or cut off, it is discarded and the draft ships instead |

A few properties worth knowing:

* **A review is not a proof.** DeepSeek reading code cannot know it runs. The chain is
  "structural check -> reviewer's opinion -> constrained rewrite", not verification.
* **The greeting is only for the model you are talking to.** `Hy kanha <your question>` is
  applied to the newest user turn only; the rewrite instruction and the review prompt never
  carry it, and the reviewer is shown the question as you typed it.
* **Secrets do not go to the reviewer.** Webhooks, `hf_`/`sk-`/`ghp_` tokens, bearer strings,
  `key = "..."` assignments and long hex are replaced with `<redacted>` in the copy the reviewer
  sees. Your script is untouched.
* **Everything is measured.** Every call is logged and returned as `phases`: model, milliseconds,
  characters, finish reason, token usage.

## Endpoints

| Endpoint | What it is |
| --- | --- |
| `POST /chat/stream` | `{messages:[{role,content},...], review?: bool}` -> `{job, model, reviewer, turns}`. Starts the chain, returns at once |
| `GET /chat/stream/{job}` | NDJSON: `{replay}`, `{t, ch}` pieces (`draft` / `review` / `answer`), `{reset, ch}`, `{phase, note}`, `{beat}` heartbeats, then `{done, text, draft, review, phases}` or `{error}` |
| `POST /chat` | The same chain, blocking. `{text, draft, review, phases}` |
| `GET /chat/result/{job}` | The same thing as one JSON object, for callers that cannot hold a stream open (Roblox) |
| `POST /generate` | Blocking, one prompt, no history |
| `POST /generate/stream` | The same as `/chat/stream`, one prompt, no history |
| `POST /v1/chat/completions` | OpenAI-compatible passthrough to Qwen. The newest user turn gets the greeting and the model is pinned; `tools`, `web_search_options`, `reasoning_effort`, `stream` pass through. No chain |
| `GET /v1/models` | Both models, then whatever else the proxy serves |
| `GET /health` | bridge, token, reviewer (key, model, shape, brief size, last failure), limits, last error |
| `GET /` | the chat page |

An OpenAI client needs two lines changed:

```python
client = OpenAI(base_url="https://<your-domain>/v1", api_key="<API_KEY>")
```

## Variables

| Variable | Default | Notes |
| --- | --- | --- |
| `QWEN_URL` | `https://qwen.aikit.club/v1` | point it at your own qwen-api deployment if the public one is rate limited |
| `QWEN_MODEL` | `qwen3.8-max` | the only model used for drafting and rewriting |
| `QWEN_THINKING` | `fast` | forced onto every Qwen call |
| `GREETING` | `Hy kanha` | in front of every question; `""` sends it untouched |
| `REVIEW_URL` | `https://api.deepseek.com` | any OpenAI-shaped endpoint, or a web-chat bridge |
| `DEEPSEEK_TOKEN` | — | the reviewer's key |
| `REVIEW_MODEL` | `deepseek-v4-flash` | `deepseek-v4-pro` if you want the slower, stronger one |
| `REVIEW_SHAPE` | `openai` | `web` for a bridge in front of chat.deepseek.com |
| `REVIEW_THINKING` | `off` | anything else turns it back on for the reviewer only |
| `REVIEW_ITEMS` | `8` | cap on the numbered list |
| `REVIEW_BRIEF` / `REVIEW_BRIEF_MAX` | `send.txt`, then `Send.txt` / `60000` | the brief, and the ceiling on it |
| `REVIEW_SCRIPT_MAX` | `24000` | how much of the draft is sent for review |
| `DRAFT_TOKENS` / `REFINE_TOKENS` | `4096` / `8192` | ceilings on the two Qwen calls |
| `MAX_TOKENS` | `4096` | ceiling on the `/v1` passthrough |
| `PIPELINE` | `auto` | `on` / `off` / `auto` (on whenever a reviewer key is set) |
| `TARGET_RUNTIME` | Roblox Luau | what the reviewer judges the script against |
| `REVIEW_EXTRA` | `{}` | JSON merged into the reviewer's request body |
| `RATE_LIMIT` / `MAX_CONCURRENT` | `30` / `4` | per-IP requests per minute, and chains at once |
| `HISTORY_MESSAGES` / `HISTORY_CHARS` | `40` / `120000` | how much of a long chat one request may carry |
| `HEARTBEAT` / `JOB_TTL` / `CHAT_TIMEOUT` | `5` / `3600` / `300` | stream keepalive, how long a finished job stays readable, provider backstop |

## The page needs no login

The chat endpoints are deliberately not key-gated, because the page is used without signing in.
What protects the two accounts behind it is the rate limit and the concurrency ceiling instead.
If that is not enough for you, raise `API_KEY` and put the page behind something else — the API
surfaces (`/v1`, `/chat`, `/generate`) are gated, and the page's own endpoints are not.

## The in-game client

`client.lua` starts a turn on `/chat/stream` and then polls `/chat/result/{job}` once a second,
because Roblox reads an HTTP response in one piece and cannot follow NDJSON. It shows the phase
it is on (`deepseek-v4-flash reviewing the draft (44s)`) instead of one silent wait, and has
**ask**, **execute** (runs a fenced ```lua block), **review** (shows what the reviewer said),
**new chat** and **copy**.

Paste your domain and key at the top. The key is `API_KEY` if you set one, otherwise the Qwen
token itself — and remember that a Roblox script is not a private place, so `API_KEY` is the
better option if other people can read the script.

A chain is three model calls, so allow for it: Roblox gives up on a single request well before
that, which is exactly why the client polls.

## Troubleshooting

| What you see | What it is |
| --- | --- |
| `token rejected`, `401`s | the Qwen token expired (they last weeks). Copy a fresh one |
| `deepseek rejected the key` | `DEEPSEEK_TOKEN` is not an API key — see [About chat.deepseek.com](#about-chatdeepseekcom) |
| `deepseek is rate limiting` | free-tier quota; wait, or `REVIEW_MODEL=deepseek-v4-pro` |
| reviewer chip red, answers still arrive | the review failed and the draft shipped. The reason is on the chip and in the log as `[job] <id> review failed: ...` |
| the draft is the answer, no rewrite | the reviewer answered `VERDICT: OK`, or a failed review meant there was no list to apply |
| `the answer was cut off by the token ceiling` | raise `DRAFT_TOKENS` (and `REFINE_TOKENS`), or ask for less at once |
| a rewrite was thrown away | it came back empty or cut off; the log says so and the draft shipped |
| the answer stops mid-sentence | the phone dropped the connection; the job is still running, reload and it replays |
| `429 too many requests from ...` | `RATE_LIMIT` per IP, or `MAX_CONCURRENT` chains already running |

## Curl

```bash
curl -s https://<your-domain>/chat/stream \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}]}'
# -> {"job":"...","model":"qwen3.8-max","reviewer":"deepseek-v4-flash","turns":1}

curl -sN https://<your-domain>/chat/stream/<job> -H "X-API-Key: $API_KEY"

# or without a stream at all
curl -s https://<your-domain>/chat -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}]}'
```
