# bahs

One writer and two readers competing over one script, behind one API, with a chat page on top.

```
you -- ask --> bahs -- send.txt, on its own ----> DeepSeek V4 Flash (thinking off, search off)
                  |                                    |
                  |                                    +-- answers it, then waits
                  |
                  +-- draft -----------------------> Qwen   (qwen-api, thinking on)
                  |
                  +-- "write your version" -------> DeepSeek, in the brief's chat
                  |
                  +-- merge the two, same chat ---> Qwen
                  |
                  +-- "would you ship it?" -------> DeepSeek: AGREE, or another version
                  |
                  +-- send2.txt, on its own ------> GLM-5.3 Flash (deep think, max)
                  |                                    |
                  +-- "write your version" -------> GLM, in the brief's chat
                  |
                  +-- merge the two, same chat ---> Qwen
                  |
                  +-- "would you ship it?" -------> GLM: AGREE, or another version
                  |
                  +-- "which do you prefer?" ------> answered here: the longest script
```

1. **DeepSeek reads `send.txt` first**, alone, and is only asked for anything after it has
   answered that. Every message it gets — the brief, the requests, the agreement questions —
   opens with the same warning, because the one thing that would make its answer wrong is
   assuming the script is for Studio: `WARNING! THIS IS NOT FOR ROBLOX STUDIO BUT FOR A ROBLOX
   EXECUTOR SCRIPT`.
2. **Qwen drafts** an answer to what you asked, in the conversation you are keeping.
3. **DeepSeek writes its own version** of that script — not a list of complaints: a script you
   could run.
4. **Qwen merges the two** in the *same conversation the draft was written in*, keeping whatever
   is genuinely more reliable from each.
5. **DeepSeek says whether it would ship the merge.** If it would not, it writes another version
   and Qwen merges again — up to `NEGOTIATE_ROUNDS` rounds (5), and it stops the moment they agree.
   The script they settled on is what you get; the draft, the other version and the verdict stay
   under the answer (collapsed) and none of them is carried into your next question.
6. **GLM then reads `send2.txt` the same way** (alone, on its own thread, before it is asked
   anything) and repeats the whole protocol over the script the first two settled on: its own
   complete version, Qwen merging that into the script in the draft's chat, and then one more
   question — would it ship the merge? Up to `SECOND_ROUNDS` (2) of its own rounds.
7. **Two readers, two chances to disagree**, and what ships is the script they both signed off
   on. Neither is a verifier — reading code cannot prove it runs — so what this buys you is two
   complete attempts at the script plus two opinions on the merge, not proof.
8. **If Qwen offers two scripts and asks which one you prefer, that question is answered for
   you** — the option with the most lines — in the same chat, and its answer is what ships. A
   turn never ends on "Which choice do you prefer?"

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
| `ZAI_TOKEN` | an API key from [z.ai](https://z.ai) (Z.AI Open Platform -> API Keys, shaped `id.secret`) for the second reader, GLM-5.3 Flash. **A chat.z.ai session token will not work** — see below |
| `API_KEY` | *optional* — if you set it, the API (`/v1`, `/chat`, `/generate`) requires it. Left unset, the Qwen token is the key. The page never needs one |

Redeploy. The page should read `api online · bridge qwen.aikit.club · token token accepted ·
reviewer key set, model served · glm key set, model served · model qwen3.8-max · mode thinking ·
Hy kanha`.

### The second reader (`chat.z.ai` / GLM-5.3 Flash)

`ZAI_TOKEN` has to be a **platform API key**. Unlike DeepSeek, the session token from
`chat.z.ai` cannot be bridged from a server, and the wall is the captcha rather than the token:

- A request to `chat.z.ai`'s `/api/v2/chat/completions` carrying the site's own parameters (its
  version header, a timestamp, a request id, the user id) is accepted **without** any signature --
  the site does not require the `X-Signature` its own bundle computes for a request shaped that
  way. Models the account is not entitled to answer `Model not available for current user level`.
- Every *generation*, though, asks for a `captcha_verify_param`: the site answers
  `FRONTEND_CAPTCHA_REQUIRED` (`captcha_error_type: missing_param`), and that parameter exists
  only for a browser that solved the challenge on that device. Producing one without the browser
  is defeating a bot check, so nothing in this service fabricates it.

That is why this reader runs on Z.AI's OpenAI-shaped platform API
(`https://api.z.ai/api/paas/v4`), under whichever name you put the key in the variables. It is
also cheap enough that the bridge is not worth having: `GLM-4.7-Flash` and `GLM-4.5-Flash` are
**free**, and `GLM-5.3-Flash` is $0.15/$0.50 per 1M tokens.

| What goes in `ZAI_TOKEN` | What happens |
| --- | --- |
| an API key (`id.secret`) from z.ai | the reader runs, with deep think at its strongest setting |
| a chat.z.ai session token (a JWT) | recognised as one before anything is sent, refused with the reason, and the chain runs with the two readers it has |

If you would rather drive something else at that stage, `ZAI_URL` already accepts any
OpenAI-shaped endpoint: point it at your own bridge and the reader uses it unchanged.

It is a third account, so it is a third thing that can run out of credits or be revoked. The
chip on the page (`glm`) reports the model the key can actually see, and says `not served -- try
<name>` when `ZAI_MODEL` is a name the platform has retired. A reader that is unreachable never
costs you the script: the rounds of the reader before it still ship.

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

## Thinking and search

* The writer **thinks**: every Qwen call is sent `thinking_mode: "thinking"` (`QWEN_THINKING`),
  because a whole Luau script is the kind of thing the reasoning is for. It costs no answer text —
  a `reasoning_content` delta on the stream is dropped, so only the script is read. Set
  `QWEN_THINKING=fast` to have the writer answer without thinking (quicker, thinner), or `auto`
  to let the model decide per call. The values are qwen-api's own enum: `fast` | `auto` |
  `thinking`.
* The reviewer is sent `thinking: {"type": "disabled"}` — or `thinking: false, search: false` in
  the `web` shape. Every toggle the reviewer gets is built in one place (`reviewer_dialect()`),
  and **search is set to false there and cannot be set true anywhere**. Thinking is off unless
  you explicitly set `REVIEW_THINKING=on`. No `tools`, `web_search_options` or search parameter
  is ever attached to a review.
* The second reader is the other way round: GLM is sent `thinking: {"type": "enabled"}` with
  `reasoning_effort: "max"` (`ZAI_THINKING`, one of `max` / `high` / `low`, or `off`) — deep
  think at the top of its ladder, which is what GLM-5.3 Flash has (it cannot be told *not* to
  think, only how hard). It is built in one place too (`zai_dialect()`), no `tools` are ever
  attached to it, so it cannot search, and its thinking is dropped from the answer exactly like
  the writer's.
* The `/v1/chat/completions` passthrough is the one place a caller can pass its own
  `web_search_options` / `tools` through — that endpoint is a passthrough to Qwen and does not
  run the chain, so it cannot affect a review.

## The brief (send.txt / Send.txt)

The brief is read once at boot and sent **on its own, before anything else the reviewer is
asked**: that message is `send.txt`, the warning that opens every message, and one line asking for
a short acknowledgement — nothing else, and nothing about the user's request. The answer to it is
waited for before the request for a script goes out. After that the request arrives in the *same* conversation — on the site path by
threading the next message onto the id of the one before it — so the reviewer is answering inside
the chat the brief was read in. The brief goes out on its own thread **while Qwen writes the
draft**, so waiting for the acknowledgement costs no turn time: the only thing that has to be
ordered is the request for a script, which cannot go until both are done.

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
* The output contract no longer rides with the brief — it rides with the first thing that is
  actually asked of the reviewer (the request after the acknowledgement, or the request itself
  when the brief is folded in), because the answer has to come back as a script for the merge
  step to be able to use it: `VERDICT: BETTER` plus the complete script, or `VERDICT: KEEP` when
  nothing in the script in front of it can be made more reliable.
* `SEED_BRIEF=off` keeps the brief but folds it into the first request instead of sending it on
  its own, which saves one call and loses the acknowledgement.

### The second reader's brief (`send2.txt`)

GLM gets the same treatment, from its own file: `send2.txt` is read once at boot and sent **on
its own** (with the warning, and one line asking for an acknowledgement) before anything is
asked of it, on its own thread alongside the draft and the first reader's brief, so it costs no
turn time either. Its request for a script only goes out after that answer lands, in the same
chat.

* Name it `send2.txt` or `Send2.txt`; `ZAI_BRIEF=/app/Send2.txt` forces a path.
* **If it is not there, the first reader's brief is sent instead** (the same standing
  instructions are worth more than none), and `/health` says which one went out:
  `second.brief` reads `send.txt (send2.txt is not in the image)`. Add the file and redeploy to
  give GLM its own.
* `Dockerfile` copies `send*.txt`, so adding `send2.txt` **does not** need a Dockerfile change —
  the glob matches one file or two and the build never depends on which.

## The flow in detail

| Step | Who | What happens |
| --- | --- | --- |
| draft | Qwen | The turn you asked, plus a ceiling of `DRAFT_TOKENS`. The answer is unwrapped from a single ``` fence |
| check | this service | Empty, `finish_reason=length`, `content_filter`, an unterminated fence. A cut-off draft is **refused**, not shipped |
| seed | DeepSeek | `send.txt`, alone, with the acknowledgement waited for. Runs on its own thread alongside the draft, so it adds no waiting, and the page puts its bubble up first. Ceiling `SEED_TOKENS`, because it is only an acknowledgement |
| seed2 | GLM | `send2.txt` (or the first brief as a fallback), alone, the same way and on its own thread — so `send2.txt` really is the first thing this reader is ever sent. Ceiling `ZAI_SEED_TOKENS` |
| peer | DeepSeek | Round 1: your request, the target runtime, Qwen's draft (secrets masked) and what the check found — answered with a complete script of its own. Ceiling `PEER_TOKENS` |
| merge | Qwen | The *same conversation the draft was written in*, plus the draft as its own assistant turn, plus the other version pasted in whole. Asked for the single best script and nothing else |
| agree | DeepSeek | Rounds 2+: the merged script, in the same chat. `VERDICT: AGREE` ends it; another `VERDICT: BETTER` starts another merge, up to `NEGOTIATE_ROUNDS` (5, so at most 12 model calls in a turn) |
| peer2 | GLM | Round 1 of the second reader: the script the first two settled on, plus the same request, the target runtime and the checks — answered with a complete script of its own. Ceiling `SECOND_TOKENS` |
| merge2 | Qwen | The same merge over GLM's version, in the same chat as the draft. The merges are numbered in the log (`merging deepseek-v4-flash's version`, `merging glm-5.3-flash's version`) |
| agree2 | GLM | Rounds 2+: would it ship the merge? `VERDICT: AGREE` ends the second reader's rounds; another `VERDICT: BETTER` starts one more merge, up to `SECOND_ROUNDS` (2, so 3 further model calls) |
| guard | this service | A merge that comes back empty or cut off is discarded, and the script it was editing is what stands |
| choose | this service → Qwen | Only when the script that stands ends by offering two scripts and asking which is preferred: the question is answered with the option that has the **most lines**, in the same chat as the draft, and its reply is what ships. Up to `CHOICE_ROUNDS` (2). If the model will not decide, that longest option is shipped as it stands |

A few properties worth knowing:

* **Nothing is cut off for being slow.** There is no ceiling on a model call by default — not on
  the draft, not on a reviewer version, not on a merge — and a blocking caller (`/chat`,
  `/generate`) waits for the whole negotiation rather than for a fixed number of seconds.
  Connecting is still bounded to 10s, so an unreachable host fails in seconds instead of looking
  like a model that is thinking. The one clock left is the page's: it treats *silence* on the
  stream as a dead connection and reattaches, and the server beats every `HEARTBEAT` seconds, so
  that never fires while the chain is working.
* **Neither model is a verifier.** DeepSeek reading code cannot know it runs either, so what this
  chain buys you is a second complete attempt at the script plus a second opinion on the merge —
  not proof. The only machine evidence in play is the local structural check.
* **The greeting is only for the model you are talking to.** `Hy kanha <your question>` is
  applied to the newest user turn only; the merge instruction and the request for a script never
  carry it, and the reviewer is shown the question as you typed it.
* **Secrets do not go to the reviewer.** Webhooks, `hf_`/`sk-`/`ghp_` tokens, bearer strings,
  `key = "..."` assignments and long hex are replaced with `<redacted>` in the copy the reviewer
  sees. Your script is untouched.
* **Everything is measured.** Every call is logged and returned as `phases`: model, milliseconds,
  characters, finish reason, token usage.
* **What was *sent* is logged too, not just what came back.** `[upstream] qwen -> qwen3.8-max: 3
  message(s), 529 chars, max_tokens 16384` before each OpenAI-shaped call, and on the site path
  `[deepseek] sending 46819 chars (brief 45212 chars from send.txt: whole)`. That is what makes
  "it only sent part of the brief" answerable from the service's own log: the line says `whole`,
  or it says `NOT COMPLETE` with the size next to it. A site stream that stops without a finished
  status is logged as well, so a review that is only the part it managed to write is never passed
  off as the whole answer.

## Endpoints

| Endpoint | What it is |
| --- | --- |
| `POST /chat/stream` | `{messages:[{role,content},...], review?: bool}` -> `{job, model, reviewer, turns, timeout}`. Starts the chain, returns at once. `timeout` is `null` unless you set a ceiling, because there is none |
| `GET /chat/stream/{job}` | NDJSON: `{replay}`, `{t, ch}` pieces (`seed` / `seed2` / `draft` / `peer` / `peer2` / `review` / `answer`), `{reset, ch}`, `{phase, note}`, `{beat}` heartbeats, then `{done, text, draft, peer, peer2, review, second_review, phases}` or `{error}` |
| `POST /chat` | The same chain, blocking. `{text, draft, peer, peer2, review, second_review, phases}` |
| `GET /chat/result/{job}` | The same thing as one JSON object, for callers that cannot hold a stream open (Roblox). Needs the API key |
| `GET /chat/poll/{job}` | The same fields plus `done`, in a response that closes at once. Not key-gated: it is what the page falls back to when a phone network keeps cutting the stream |
| `POST /generate` | Blocking, one prompt, no history |
| `POST /generate/stream` | The same as `/chat/stream`, one prompt, no history |
| `POST /v1/chat/completions` | OpenAI-compatible passthrough to Qwen. The newest user turn gets the greeting and the model is pinned; `tools`, `web_search_options`, `reasoning_effort`, `stream` pass through. No chain |
| `GET /v1/models` | Both models, then whatever else the proxy serves |
| `GET /health` | bridge, token, reviewer (key, model, shape, brief size, last failure), second (key, model, thinking, brief, rounds, last failure), limits, last error |
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
| `QWEN_THINKING` | `thinking` | forced onto every Qwen call: `fast`, `auto` or `thinking` — qwen-api's own enum. `thinking` is the default — the writer is producing a whole script, and the reasoning is dropped from the answer, so it costs nothing but time. `fast` answers sooner |
| `CHOICE_ROUNDS` | `2` | how many times the writer may be told to stop asking which of two scripts is preferred, capped at `3`. Each round is one extra Qwen call, and only happens when the answer really offers two scripts and asks. `0` ships the question as it stands |
| `GREETING` | `Hy kanha` | in front of every question; `""` sends it untouched |
| `REVIEW_URL` | follows `DEEPSEEK_TOKEN` | a `userToken` goes to `https://chat.deepseek.com`, an `sk-...` key to `https://api.deepseek.com`; set it by hand for any OpenAI-shaped endpoint / bridge and it is used as given |
| `DEEPSEEK_TOKEN` | — | the reviewer's credential: a `userToken` or a platform API key |
| `DEEPSEEK_COOKIE` | — | a `cf_clearance` cookie, if the site ever asks for one |
| `ZAI_URL` | `https://api.z.ai/api/paas/v4` | Z.AI's OpenAI-shaped platform API. Set it by hand for any other OpenAI-shaped endpoint and it is used as given |
| `ZAI_TOKEN` | — | the second reader's credential: an API key from [z.ai](https://z.ai). A chat.z.ai session token cannot be used, so it is recognised and refused up front, with the reason in the chip; its flash models are free |
| `ZAI_MODEL` | `glm-5.3-flash` | the second reader's model. `/health` lists what the key can actually see, and suggests the nearest name when this one is not served |
| `ZAI_THINKING` | `max` | deep think at the top of its ladder: `max` / `high` / `low`, or `off` for a model that allows it. Sent together with `reasoning_effort` |
| `SECOND_ROUNDS` | `2` | GLM's own rounds, capped at `5`: one version of its own, then one chance to agree with what came back. `0` (or `PIPELINE=off`) leaves the chain at two models |
| `SECOND_SEED` | `on` | send `send2.txt` on its own and wait for the answer before the request. `off` folds it in and saves a call |
| `SECOND_TOKENS` / `ZAI_SEED_TOKENS` | `16384` / `512` | ceilings for GLM writing a script, and for it acknowledging its brief |
| `SECOND_TEMPERATURE` | `0.3` | GLM's sampling temperature (z.ai's range is 0-1) |
| `SECOND_BRIEF` / `ZAI_BRIEF` / `SECOND_BRIEF_MAX` | `send2.txt`, then `Send2.txt`, then the first brief / `60000` | the second reader's brief, and the ceiling on it |
| `ZAI_TIMEOUT` | `0` | no ceiling on a GLM call, like the rest of the chain |
| `POW_WASM` | `sha3_wasm_bg.wasm` beside the code | where the proof-of-work module is read from |
| `POW_MAX_TRIES` | `5000000` | the largest `difficulty` this service will solve; past it the message goes out headerless instead of stalling |
| `REVIEW_MODEL` | `deepseek-v4-flash` | `deepseek-v4-pro` for the slower, stronger one; ignored by the web transport, whose model is whatever your account is set to |
| `REVIEW_SHAPE` | `openai` | `web` for a bridge, `deepseek-web` for the site itself (chosen for you when `REVIEW_URL` is chat.deepseek.com) |
| `REVIEW_THINKING` | `off` | anything else turns it back on for the reviewer only |
| `NEGOTIATE_ROUNDS` | `5` | how many rounds of "DeepSeek writes a version, Qwen merges" may run, capped at `5`. Round 1 is the competition; each round after it is DeepSeek agreeing with the merge or proposing another version. `0` ships the draft alone. A round is two model calls, so 5 is up to 12 calls in a turn — lower it if you want turns to finish sooner |
| `SEED_BRIEF` | `on` | send `send.txt` on its own and wait for the answer before the request. `off` folds it into the first request and saves a call |
| `PEER_TOKENS` / `SEED_TOKENS` | `8192` / `512` | ceilings on the API path for DeepSeek writing a script, and for it acknowledging the brief |
| `MERGE_PASTE_MAX` | `48000` | how much of the other version is pasted into a merge instruction |
| `REVIEW_BRIEF` / `REVIEW_BRIEF_MAX` | `send.txt`, then `Send.txt` / `60000` | the brief, and the ceiling on it |
| `REVIEW_SCRIPT_MAX` | `48000` | how much of the draft is sent for review; past this the reviewer is told the script is truncated |
| `DRAFT_TOKENS` / `REFINE_TOKENS` | `8192` / `16384` | ceilings on the two Qwen calls. A whole script is the point of both, and 4096 tokens is roughly 200 lines of Luau; an answer that reaches its ceiling is refused rather than shipped, so a ceiling that is too low shows up as a failed turn |
| `MAX_TOKENS` | `4096` | ceiling on the `/v1` passthrough |
| `PIPELINE` | `auto` | `on` / `off` / `auto` (on whenever a reviewer key is set) |
| `TARGET_RUNTIME` | a Roblox **executor** script, not a Studio one | what the reviewer judges the script against, named in the rubric |
| `REVIEW_WARNING` | `WARNING! THIS IS NOT FOR ROBLOX STUDIO BUT FOR A ROBLOX EXECUTOR SCRIPT` | put in front of **every** message to the reviewer, so a long conversation cannot bury the fact that the target is an executor rather than Studio |
| `REVIEW_EXTRA` | `{}` | JSON merged into the reviewer's request body |
| `RATE_LIMIT` / `MAX_CONCURRENT` | `30` / `4` | per-IP requests per minute, and chains at once |
| `HISTORY_MESSAGES` / `HISTORY_CHARS` | `40` / `120000` | how much of a long chat one request may carry |
| `CHAT_TIMEOUT` / `REVIEW_TIMEOUT` | `0` / `0` | **no ceiling by default**: a model may take as long as it needs, because a long negotiation is not an error. `0` or less means no limit; set a number of seconds to put one back |
| `HEARTBEAT` / `JOB_TTL` | `5` / `3600` | stream keepalive, and how long a finished job stays readable |

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

A chain is up to twelve model calls, so allow for it: Roblox gives up on a single request well
before that, which is exactly why the client polls. Its own deadline is an hour of wall clock
(`os.time()`, not `os.clock()` — see the comment there), and the server puts no ceiling on the
chain itself.

## Troubleshooting

| What you see | What it is |
| --- | --- |
| `token rejected`, `401`s | the Qwen token expired (they last weeks). Copy a fresh one |
| `deepseek rejected the token`, `your api key ... is invalid` | a chat `userToken` was sent to the API because `REVIEW_URL` was set by hand — clear it, or set it to `https://chat.deepseek.com` |
| `deepseek rejected the key` | an API key that is wrong or revoked; make a new one at [platform.deepseek.com](https://platform.deepseek.com) |
| `deepseek is rate limiting` | free-tier quota; wait, or `REVIEW_MODEL=deepseek-v4-pro` |
| `zai rejected ZAI_TOKEN`, `glm` chip red | the credential is a revoked key, or a chat.z.ai session token. Put an API key from [z.ai](https://z.ai) in `ZAI_TOKEN`: that site's generations are captcha-gated, so a session token cannot be driven from a server |
| `[second] ZAI_TOKEN is a chat.z.ai session token...` at boot, `glm` chip red | the value is a JWT (the site's `localStorage.token`). No call is wasted on it: it cannot authenticate at the API and cannot generate at the site either. Replace it with an API key from [z.ai](https://z.ai) — its flash models are free |
| `glm-5.3-flash is not served -- try ...` | the platform does not have that model id under this key. Set `ZAI_MODEL` to the name the chip offers |
| the second reader never runs | no `ZAI_TOKEN`, a session token in it, `SECOND_ROUNDS=0`, or `PIPELINE=off`. Boot says which — `[second] no ZAI_TOKEN set; the chain runs with one reader`, or the session-token line above |
| the answer is the first reader's script rather than GLM's merge | GLM was unreachable or proposed nothing usable. The note after the turn says which: `glm-5.3-flash did not read send2.txt (...)`, `glm-5.3-flash stopped early (...)` |
| reviewer chip red, answers still arrive | the review failed and the draft shipped. The reason is on the chip and in the log as `[job] <id> seed failed: ...` or `[job] <id> negotiation stopped: ...` |
| `40300 MISSING_HEADER` | the message went out without its proof-of-work header — see [The proof of work](#the-proof-of-work-pow_solverpy), and the `[deepseek]` lines in the log |
| `40301 INVALID_POW_RESPONSE` | the proof of work was solved with the wrong module build |
| the draft is the answer, nothing merged | DeepSeek answered `VERDICT: KEEP` (nothing in it could be made more reliable), a failed review meant there was nothing to merge, or `NEGOTIATE_ROUNDS=0` |
| the answer is the longer of two scripts you were never shown | Qwen offered two and asked which was preferred. The bridge answered for you with the one that has the most lines, in the same chat; the log says `[job] <id> choose: ... offered 2 script(s) (5, 14 lines)`. `CHOICE_ROUNDS=0` turns that off |
| the answer is the draft even though the reviewer proposed a version | the merge came back empty or cut off and was thrown away; the log says `the merged script was not usable` |
| `the answer was cut off by the token ceiling` | raise `DRAFT_TOKENS` (and `REFINE_TOKENS`), or ask for less at once |
| a version was thrown away | it came back empty or cut off; the log says so and the script it was editing is what stands |
| the answer stops mid-sentence | the phone dropped the connection; the job is still running, and the page reattaches and replays it -- and after three drops it collects the answer with `GET /chat/poll/{job}` instead, one short request at a time |
| `the stream ended early` | the read was cut before the turn finished, which the page now treats as a reattach rather than a failure. It should no longer be the thing you see; if it is, the log line `[job] <id> ...` for that turn says how far it got |
| a turn takes minutes | up to 15 model calls with both readers at their defaults, and none of them is cut off. `NEGOTIATE_ROUNDS=1` and `SECOND_ROUNDS=1` for one version and one merge each, or `REVIEW_MODEL=deepseek-v4-pro` for a stronger but slower reviewer |
| a turn never ends at all | the model itself is hanging, and nothing on this side will cut it off (that is the point). Set `REVIEW_TIMEOUT=300` and `CHAT_TIMEOUT=300` to bring a ceiling back |
| `429 too many requests from ...` | `RATE_LIMIT` per IP, or `MAX_CONCURRENT` chains already running |

## Curl

```bash
curl -s https://<your-domain>/chat/stream \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}]}'
# -> {"job":"...","model":"qwen3.8-max","reviewer":"deepseek-v4-flash","second":"glm-5.3-flash","turns":1}

curl -sN https://<your-domain>/chat/stream/<job> -H "X-API-Key: $API_KEY"

# or without a stream at all
curl -s https://<your-domain>/chat -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}]}'
```

## Verifying a change

`verify_chain.py` runs the whole chain against stubbed Qwen, DeepSeek and GLM endpoints — no
keys, no network. It covers each brief being sent alone with the request only after its answer
(both of them), both provider shapes, thinking being on for the writer and off for the reviewer
and deep-think-max for the second reader, a reasoning delta never reaching the answer, the
reviewer's search/thinking toggles being off in every call and GLM's calls carrying no tools, the
rounds being bounded so a pair that never agrees still finishes (both readers), the second reader
being handed the script the first two settled on and its merge being what ships, either reader
failing without costing the script, the choice question being answered with the option that has
the most lines (and the fallbacks when the model will not decide), `CHOICE_ROUNDS=0`,
`SEED_BRIEF=off`, `SECOND_SEED=off`, `NEGOTIATE_ROUNDS=0`, `SECOND_ROUNDS=0`, secret masking, the
merge guard, a reader that dies mid-negotiation, the site's message id threading a second message
onto the first, the proof of work, and the blocking and polling paths:

```bash
.venv/bin/python verify_chain.py
```

The service is four modules, smallest dependency first:

* `bridge.py` — everything that talks to a provider: the tokens, the config, the two transports,
  the briefs, the job record, one request and its stream.
* `state.py` — what the service can say about itself (the three credentials, the model lists, the
  last failure) and the two usage limits.
* `peers.py` — the second reader: GLM's credential, model, thinking setting, brief and chip.
* `server.py` — the service on top: the chain, the jobs, the endpoints and the page.

Keeping the provider side, the health side and the second reader out of `server.py` is not only
layout: those are the parts a new reader has to touch, and this way each of them is a file you can
change without reading the whole chain.
