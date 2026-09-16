# bahs

One model, one chat, and a toolbox it runs on its own work, behind one API, with a chat page on
top.

```
you -- ask --> bahs -- one call, thinking on ----------> Qwen (qwen3.8-max)
                 |                                       |
                 |                                       +-- calls a tool --> luau.py
                 |                                       |     luau_check   the script's blocks, strings, brackets
                 |                                       |     roblox_api   does that member exist, and how
                 |                                       |     run_script   run it in the executor and read the error
                 |                                       |     apply_edit   change one line, not the whole file
                 |                                       |     secret_scan  credentials that must not ship
                 |                                       |     luau_format  re-indent what it assembled
                 |                                       |
                 |                                       +-- reads the result, on it goes (AGENT_ROUNDS)
                 |
                 +-- always the same session -------------------> the same upstream chat
```

1. **One model.** Qwen 3.8 Max, with `thinking_mode: "thinking"` on every call. There is no
   reviewer and no second reader any more: where a second opinion used to be, there is a set of
   checks the writer runs on its own script (`luau.py`), which is worth more than a second model
   guessing — one of them can actually execute it.
2. **One chat.** Every turn carries a session id, and the service keeps the hidden
   `<!-- qwen_metadata: ... -->` the proxy puts in each answer, puts it back on the next request
   of that session, and cuts it out of everything you see. That is what continues the upstream
   chat instead of opening a new one per question.
3. **Tool rounds.** A call that comes back asking for a tool has not answered yet: the tool is
   run here, its result is appended to the conversation, and the model is asked again — up to
   `AGENT_ROUNDS` times. What ships is the first answer that asks for nothing.
4. **The executor makes it real.** `run_script` queues a script, `client.lua` runs it in the
   Roblox client it is injected into, and the prints and the traceback come back to the model as
   a tool result. Nothing else can prove a script works.

chat.qwen.ai has no public API. [`qwen-api`](https://github.com/encryptarun/qwen-api) turns it
into OpenAI-compatible endpoints (including tool calling and the continuation metadata) using the
Qwen *access token* from your browser (`localStorage.token`). That token is the key to a whole
account, so it lives in **this** service's variables and never in a page or a Roblox script.

There is no model here: nothing is pulled, loaded or warmed, and there is no database. The
conversation lives in the browser (or in `client.lua`) and is sent back with every turn.

## What to set on Railway

`bahs` -> Settings -> Variables:

| Variable | Value |
| --- | --- |
| `QWEN_TOKEN` | Qwen access token: chat.qwen.ai -> F12 -> Console -> `localStorage.token` |
| `API_KEY` | *optional* — if you set it, the API (`/v1`, `/chat`, `/generate`, `/agent`) requires it. Left unset, the Qwen token is the key. The page never needs one |

Redeploy. The page should read `api online · bridge qwen.aikit.club · token token accepted ·
tools 6 on -- luau_check, luau_format, roblox_api... · roblox 682 classes · executor not
listening -- run_script says so instead of waiting · model qwen3.8-max · mode thinking · Hy kanha`.

## The tools

The schemas are in `luau.py`; the loop that runs them is `run_job` in `server.py`. All six are
local to this image — no extra service, no extra key — except that `roblox_api` fetches the
Roblox API dump the first time it is called.

| Tool | What it is |
| --- | --- |
| `luau_check` | Structural read of the script: blocks (`function`/`if`/`for`/`while`/`do`/`repeat` against `end`/`until`), unterminated strings and long strings, unbalanced brackets, and the calls that break in an executor (`wait()`, a Studio-only service, `require(assetId)`, a `while true do` with no wait). Errors and warnings carry line numbers |
| `roblox_api` | The real API dump: a class's members with parameters, return types and tags (`NotReplicated`, security levels), a member searched across classes, or a name that does not exist. This is what stops the model inventing `Humanoid:SetSpeed()` |
| `run_script` | Hands the script to the executor polling `/agent/pull`, waits for `/agent/push`, and returns what it printed and any traceback. Says so at once when nothing is listening rather than waiting |
| `apply_edit` | `find`/`replace` on the script in the conversation, refusing an ambiguous match instead of guessing — the cheap way to change one line |
| `secret_scan` | Webhooks, `sk-`/`hf_`/`ghp_` tokens, bearer strings, `key = "..."`, long hex. Reports the line and the kind, never the value |
| `luau_format` | Re-indents by block depth (whitespace only), for a script assembled from pieces |

The API dump is fetched once and remembered (`ROBLOX_API_TTL`, six hours). `ROBLOX_API_DUMP` can
point at a file or a URL instead — that is how the tests run it with no network.

## The executor bridge (`client.lua`)

`client.lua` is the other end of `run_script`. Paste your domain and key at the top and press
**listen**: it polls `/agent/pull` once a second, runs whatever the model queued, collects what was
printed plus `debug.traceback` on a failure, and posts both to `/agent/push`. `luau_check` tells
the model what is structurally wrong; a run tells it what actually happened.

- Only a client that has polled recently counts as listening (`EXECUTOR_IDLE`, 90s), which is why
  the tool never blocks for a client that is not there.
- The wait for one run is `RUN_TIMEOUT` (45s) by default, capped at five minutes by the tool.
- Both endpoints are key-gated: a client that can be handed arbitrary Luau has to be a client you
  trust.
- Without `listen` the model simply cannot run anything, and the tool says so. Everything else
  still works.
- The other buttons are **ask** (a turn, with the phase shown live), **execute** (run the answer
  yourself), **tools** (what the model did to its own work), **new chat** (a new session) and
  **copy**.

## Variables

| Variable | Default | Notes |
| --- | --- | --- |
| `QWEN_URL` | `https://qwen.aikit.club/v1` | point it at your own qwen-api deployment if the public one is rate limited |
| `QWEN_MODEL` | `qwen3.8-max` | the only model used |
| `QWEN_THINKING` | `thinking` | forced onto every call: `fast`, `auto` or `thinking` — qwen-api's own enum. `thinking` is the default and the point: a whole script, reasoned, with the `reasoning_content` dropped from the answer |
| `GREETING` | `Hy kanha` | in front of every question; `""` sends it untouched |
| `AGENT_TOOLS` | `on` | `off` and no tool schemas are attached: the model answers from what it knows |
| `AGENT_ROUNDS` | `8` | how many tool rounds one turn may take, capped at 12. A round is a model call plus the tools it asked for; `0` disables the toolbox as well |
| `AGENT_RUN` | `on` | `off` and `run_script` refuses: nothing can be executed, whatever is listening |
| `RUN_TIMEOUT` | `45` | seconds one `run_script` waits for the executor before giving up (5–300) |
| `EXECUTOR_IDLE` | `90` | how long after its last poll a client still counts as listening |
| `ROBLOX_API_DUMP` | — | a file or URL to read the API dump from. Unset, the current client version is asked for and the dump fetched beside it |
| `ROBLOX_API_URL` | `https://setup.rbxcdn.com/versionQTStudio` | where the client version comes from |
| `ROBLOX_API_TTL` | `21600` | how long the dump is kept before it is fetched again |
| `ANSWER_TOKENS` | `8192` | ceiling on the first call of a turn (alias `DRAFT_TOKENS`). 4096 tokens is roughly 200 lines of Luau, and an answer that stops at its ceiling is refused rather than shipped |
| `REFINE_TOKENS` | `16384` | ceiling on the calls after a tool round, which may be rewriting a whole script |
| `TOOL_RESULT_MAX` | `20000` | how much of a tool's output is handed back to the model |
| `SESSION_TTL` | `3600` | how long one session's continuation marker is kept. `/health` reports how many sessions are held |
| `CHAT_TIMEOUT` | `0` | **no ceiling by default**: a turn with tool rounds may take as long as it takes. `0` means no limit; a number puts one back on each call |
| `HISTORY_MESSAGES` / `HISTORY_CHARS` | `40` / `120000` | how much of a long chat one request may carry |
| `MAX_TOKENS` | `4096` | ceiling on a `/v1` passthrough the caller did not set one for |
| `RATE_LIMIT` / `MAX_CONCURRENT` | `30` / `4` | per-IP requests per minute, and turns at once |
| `API_KEY` | — | what callers must send on the API surfaces, including `/agent/*` |
| `HEARTBEAT` / `JOB_TTL` | `5` / `3600` | stream keepalive, and how long a finished job stays readable |
| `LOG_REQUESTS` | `1` | `0` silences the one `[upstream]` line per model call |

`send.txt` / `Send.txt` / `send2.txt` are **no longer read**: they were the readers' briefs, and
the readers are gone. The files are still in the repository, untouched — nothing sends them
anywhere.

## The flow in detail

| Step | Who | What happens |
| --- | --- | --- |
| ask | this service | The caller's turns, greeted, with `TOOL_SYSTEM` in front and the session's continuation marker on the newest assistant turn |
| answer | Qwen | One streaming call with the six tool schemas attached. `reasoning_content` is dropped; the tool XML and the metadata are cut out of what is streamed |
| tool | this service | If the call asked for tools: each is run, one trace line per call goes to the page's `tool` channel, and the results go back as `tool` turns |
| round | Qwen | Asked again, in the same chat, with the results in front of it — until an answer asks for nothing |
| guard | this service | An empty answer, one cut off at the token ceiling, or a content filter is refused. A turn that used its last round on a tool call ships the best script it wrote on the way |
| ship | this service | The script is the answer. The tool trace and the per-call records stay under it and are not carried into the next question |

A few properties worth knowing:

* **Nothing is cut off for being slow.** There is no ceiling on a model call by default, and
  connecting is still bounded to 10s, so an unreachable host fails in seconds instead of looking
  like a model that is thinking.
* **The model is not a verifier.** `luau_check` reads structure, `roblox_api` reads the dump, and
  only `run_script` runs anything. Without a listening executor, "it works" is still a claim.
* **Secrets do not leave for the provider.** Webhooks, tokens, `key = "..."` assignments and long
  hex are replaced with `<redacted>` in the copy sent upstream, and `secret_scan` reports them to
  the model. Your script on the page is untouched.
* **Everything is measured.** Every call is logged and returned as `phases`: model, milliseconds,
  characters, finish reason, token usage, and which tools it called.
* **What was sent is logged too**, not just what came back (`[upstream] qwen -> qwen3.8-max: 4
  message(s), 2170 chars, max_tokens 16384, 6 tool(s)`), which is what makes "it only sent part of
  the conversation" answerable from the service's own log.

## Endpoints

| Endpoint | What it is |
| --- | --- |
| `POST /chat/stream` | `{messages:[{role,content},...], session?: str}` -> `{job, model, session, tools, turns, timeout}`. Starts the turn, returns at once |
| `GET /chat/stream/{job}` | NDJSON: `{replay}`, `{t, ch}` pieces (`answer` / `tool`), `{reset, ch}`, `{phase, note}`, `{beat}`, then `{done, text, tool, session, phases}` or `{error}` |
| `POST /chat` | The same turn, blocking. `{text, tool, session, phases}` |
| `GET /chat/result/{job}` | The same thing as one JSON object, for callers that cannot hold a stream open (Roblox). Needs the API key |
| `GET /chat/poll/{job}` | The same fields plus `done`, in a response that closes at once. Not key-gated: it is what the page falls back to when a phone network keeps cutting the stream |
| `POST /generate` / `POST /generate/stream` | One prompt, no history. The session still applies |
| `GET /agent/pull?client=...` | The next script the model queued for the executor, or nothing. Needs the API key |
| `POST /agent/push` | `{run, ok, output, error}` — the executor's answer for one run |
| `POST /v1/chat/completions` | OpenAI-compatible passthrough to Qwen. The newest user turn gets the greeting and the model is pinned; `tools`, `web_search_options`, `reasoning_effort`, `stream` pass through. No tool loop here — a tool call comes back to you, which is what an OpenAI client expects |
| `GET /v1/models` | The model this bridge uses, then whatever else the proxy serves |
| `GET /health` | bridge, token, tools (on, names, dump state, executor state), sessions, limits, last error |
| `GET /` | the chat page |

An OpenAI client needs two lines changed:

```python
client = OpenAI(base_url="https://<your-domain>/v1", api_key="<API_KEY>")
```

## The page needs no login

The chat endpoints are deliberately not key-gated, because the page is used without signing in.
What protects the account behind it is the rate limit and the concurrency ceiling instead. If that
is not enough for you, raise `API_KEY` and put the page behind something else — the API surfaces
(`/v1`, `/chat`, `/generate`, `/agent`) are gated, and the page's own endpoints are not.

## Curl

```bash
curl -s https://<your-domain>/chat/stream \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}],"session":"my-chat-1"}'
# -> {"job":"...","model":"qwen3.8-max","session":"my-chat-1","tools":[...],"turns":1}

curl -sN https://<your-domain>/chat/stream/<job> -H "X-API-Key: $API_KEY"

# or without a stream at all
curl -s https://<your-domain>/chat -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}],"session":"my-chat-1"}'
```

## Verifying a change

`verify_chain.py` runs the whole chain against a stubbed Qwen and the toolbox against itself — no
keys, no network (the API dump is a fixture). It covers: one call to one model with thinking on
and the schemas attached; a tool round trip in both the XML and the OpenAI shape (fragmented
arguments and all); the tool result going back and being matched to its call; the interim prose
and the tool XML never reaching the reader; the session keeping one chat and no other session
getting it; the executor round trip behind `run_script` (including the refusal when nothing is
listening); the round limit; a cut-off answer being refused; the toolbox's own units; and the
surface (`/health`, `/v1/models`, the page, the gate).

```bash
.venv/bin/python verify_chain.py
```

The service is four modules, smallest dependency first:

* `bridge.py` — everything that talks to the provider: the token, the config, one request and its
  stream, the marker cut out of it, and the session store that keeps one chat.
* `state.py` — what the service can say about itself (the token, the model list, the last failure)
  and the two usage limits.
* `luau.py` — the toolbox: the six tools, the Roblox API dump, and the queue the executor polls.
* `server.py` — the service on top: the turn, the tool rounds, the jobs, the endpoints and the page.
