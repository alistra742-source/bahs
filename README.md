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
| `DEEPSEEK_TOKEN` | either the `userToken` from chat.deepseek.com or an API key (`sk-...`) from [platform.deepseek.com](https://platform.deepseek.com) -- the endpoint follows the credential, see below |
| `API_KEY` | *optional* — if you set it, the API (`/v1`, `/chat`, `/generate`) requires it. Left unset, the Qwen token is the key. The page never needs one |

Redeploy. The page should read `api online · bridge qwen.aikit.club · token token accepted ·
reviewer key set, model served · model qwen3.8-max · mode fast · Hy kanha`.

### `chat.deepseek.com` with a `userToken`

`DEEPSEEK_TOKEN` takes either credential, and which one it is decides the transport **by itself**:

| What goes in `DEEPSEEK_TOKEN` | Transport | What to set |
| --- | --- | --- |
| the `userToken` from chat.deepseek.com | the web transport built in here | nothing — `REVIEW_URL` becomes `https://chat.deepseek.com` and the shape (`deepseek-web`) comes with it |
| an API key from platform.deepseek.com (`sk-...`) | OpenAI-shaped | nothing; `REVIEW_URL` stays `https://api.deepseek.com` |

An API key starts with `sk-` and a `userToken` does not, so a session token with no endpoint set
goes to the site instead of being rejected by the API — and so does one left pointed at
`api.deepseek.com`, because that pair cannot authenticate either. Any other endpoint set by hand
(a bridge, a mirror) is used exactly as given.
The switch is reported at boot (`[review] DEEPSEEK_TOKEN is not an sk-... API key, so the review goes
to the site instead of the API`) and on `/health` as `reviewer.endpoint`.

The error this prevents — and what it means if you see it anyway, because `REVIEW_URL` was set by
hand to the API:

```
deepseek rejected the token (Authentication Fails, Your api key: ****VEFK is invalid)
-- DEEPSEEK_TOKEN holds a chat.deepseek.com session token, not an API key, and the review is
   still going to the API. Set REVIEW_URL=https://chat.deepseek.com to use the web transport,
   or put an `sk-...` API key from platform.deepseek.com in DEEPSEEK_TOKEN
```

Same model either way; what differs is which side of your account answers. The site's endpoints
were probed from this container rather than assumed, and they answered:

| Asked | Answered |
| --- | --- |
| `POST /api/v0/chat/create_pow_challenge`, no token | `200 {"code":40002,"msg":"Missing Token"}` — reachable, no WAF challenge |
| `POST /api/v0/chat_session/create`, bad token | `200 {"code":40003,"msg":"Authorization Failed (invalid token)"}` |
| `POST /api/v0/chat/completion`, bad token, with or without a pow header | `200 {"code":40003,"msg":"INVALID_TOKEN"}` |

So the token is checked first and everything before the message already works: `/users/current`
is the token check behind the chip (an expired `userToken` shows up there, not as a hung review),
`/chat_session/create` makes a fresh chat per review so reviews never read each other, and
`/chat/create_pow_challenge` is fetched and solved (see below).

### The proof of work (`pow_solver.py`)

Every message to `/chat/completion` has to carry an `x-ds-pow-response` header. Without it the
API answers `40300 MISSING_HEADER`; with a wrong answer it answers `40301 INVALID_POW_RESPONSE`,
which is why the header is not something to guess at.

The challenge is a hash the server already computed over a small integer, and the work is
recovering that integer:

```
challenge == DeepSeekHashV1("{salt}_{expire_at}_" + str(w))   for some w in [0, difficulty)
```

`difficulty` is how many candidates that takes — the site hands out 144000, which the site's own
module searches in ~10 ms. **The module is used, not reimplemented.** `DeepSeekHashV1` is neither
SHA3-256 nor Keccak-256: it is a 256-bit-capacity sponge (168-byte rate, not 136), and its digests
match neither (both of those were checked against this module, and pycryptodome's Keccak-256
reproduces the published empty-string vector, so the comparison itself is sound). A sponge that is
subtly wrong looks exactly like the header being useless, so `sha3_wasm_bg.wasm` — the 26,612-byte
copy the site itself loads — is fetched by the Dockerfile and, if the image does not carry it, on
the first review. `wasmtime` (in `requirements.txt`) runs it, and is imported lazily so a platform
without a wheel still serves the API.

The solver was verified without a token: a challenge built from a known `w` comes back as exactly
that `w`, `w = difficulty` comes back unsolved (the range is half open), and an unsolvable
challenge sends **no** header rather than a made-up one.

If a review does fail, the codes say which half went wrong:

| Error | What it means |
| --- | --- |
| `40300 MISSING_HEADER` | no header went out — the module was unavailable, the algorithm was not `DeepSeekHashV1`, or the challenge was not solved. The `[deepseek]` lines in the log say which |
| `40301 INVALID_POW_RESPONSE` | an answer was sent and rejected — the module is not the build the site is using |
| `FAIL_SYS_USER_VALIDATE` | the AWS WAF human-check, not the proof of work. Wait a few minutes |

If the site ever answers with a browser check instead, put the `cf_clearance` cookie in
`DEEPSEEK_COOKIE`.

Requests on this path are made to look like the site's own client — its headers, its
`x-client-platform: web`, its bearer — and `thinking_enabled` and `search_enabled` are sent
`false` on every message, always. The site takes no temperature or token ceiling, so those do not
apply here.

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
| `REVIEW_URL` | follows `DEEPSEEK_TOKEN` | a `userToken` goes to `https://chat.deepseek.com`, an `sk-...` key to `https://api.deepseek.com`; set it by hand for any OpenAI-shaped endpoint / bridge and it is used as given |
| `DEEPSEEK_TOKEN` | — | the reviewer's credential: a `userToken` or a platform API key |
| `DEEPSEEK_COOKIE` | — | a `cf_clearance` cookie, if the site ever asks for one |
| `POW_WASM` | `sha3_wasm_bg.wasm` beside the code | where the proof-of-work module is read from |
| `POW_MAX_TRIES` | `5000000` | the largest `difficulty` this service will solve; past it the message goes out headerless instead of stalling |
| `REVIEW_MODEL` | `deepseek-v4-flash` | `deepseek-v4-pro` for the slower, stronger one; ignored by the web transport, whose model is whatever your account is set to |
| `REVIEW_SHAPE` | `openai` | `web` for a bridge, `deepseek-web` for the site itself (chosen for you when `REVIEW_URL` is chat.deepseek.com) |
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
| `deepseek rejected the token`, `your api key ... is invalid` | a chat `userToken` was sent to the API because `REVIEW_URL` was set by hand — clear it, or set it to `https://chat.deepseek.com` |
| `deepseek rejected the key` | an API key that is wrong or revoked; make a new one at [platform.deepseek.com](https://platform.deepseek.com) |
| `deepseek is rate limiting` | free-tier quota; wait, or `REVIEW_MODEL=deepseek-v4-pro` |
| reviewer chip red, answers still arrive | the review failed and the draft shipped. The reason is on the chip and in the log as `[job] <id> review failed: ...` |
| `40300 MISSING_HEADER` | the message went out without its proof-of-work header — see [The proof of work](#the-proof-of-work-pow_solverpy), and the `[deepseek]` lines in the log |
| `40301 INVALID_POW_RESPONSE` | the proof of work was solved with the wrong module build |
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

## Verifying a change

`verify_chain.py` runs the whole chain against stubbed Qwen and DeepSeek endpoints — no keys,
no network — and covers the brief being first, the thinking/search toggles, the three phases,
secret masking, the regression guard, the blocking and polling paths, and the chat.deepseek.com
guard:

```bash
.venv/bin/python verify_chain.py
```
