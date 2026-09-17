# bahs

Two models, three ways to be answered, and a toolbox the writer runs on its own work — behind one
API, with a chat page on top. The writer reasons before it writes by default, and any turn can ask
it not to (`thinking` or `fast`, per question).

```
                    mode: agent
you -- ask --> bahs ---- deepseek ------------------------> a plan (not the answer)
                 |                                          |
                 |        qwen3.8-max (thinking on) <--------+
                 |          |
                 |          +-- calls a tool --> luau.py
                 |          |     luau_check   the script's blocks, strings, brackets
                 |          |     roblox_api   does that member exist, and how
                 |          |     web_get      read the page that says how it is used
                 |          |     run_script   run it in the executor and read the error
                 |          |     apply_edit   change one line, not the whole file
                 |          |     luau_find    the lines that match, with line numbers
                 |          |     secret_scan  credentials that must not ship
                 |          |     luau_format  re-indent what it assembled
                 |          |
                 |          +-- reads the result, on it goes (AGENT_ROUNDS)
                 |
                 +-- always the same session ----------> the same upstream chat, on both sides
```

1. **Three modes, and the choice is per question.**
   * **agent** — DeepSeek is asked what to build and how (once), then Qwen 3.8 Max writes the
     script from that plan with its tools. Two models, called once each: there is no negotiation
     loop, no review of the plan, and no third call to a second opinion. What follows the plan is
     the writer's own tool work.
   * **qwen** — Qwen 3.8 Max on its own, with the toolbox.
   * **deepseek** — DeepSeek on its own. It writes the script itself and no tools are attached.

   The writer's setting travels with the question too: `thinking` (the default) has it work the
   script out before writing it, and `fast` has the same model spend those tokens on the script
   instead. Either can be asked for on any turn of the same conversation.
2. **One chat, not one per question.** Every turn carries a session id. The service keeps the
   hidden `<!-- qwen_metadata: ... -->` the proxy puts in each Qwen answer *and* the message id
   chat.deepseek.com threads its chats with, puts each back on the next request of that session,
   and cuts the marker out of everything you see.
3. **Tool rounds.** A call that comes back asking for a tool has not answered yet: the tool is run
   here, its result is appended to the conversation, and the model is asked again — up to
   `AGENT_ROUNDS` times. What ships is the first answer that asks for nothing.
4. **The executor makes it real.** `run_script` queues a script, `client.lua` runs it in the
   Roblox client it is injected into, and the prints and the traceback come back to the model as a
   tool result. Nothing else can prove a script works.

chat.qwen.ai has no public API. [`qwen-api`](https://github.com/encryptarun/qwen-api) turns it into
OpenAI-compatible endpoints (including tool calling and the continuation metadata) using the Qwen
*access token* from your browser (`localStorage.token`). That token is the key to a whole account,
so it lives in **this** service's variables and never in a page or a Roblox script.

There is no model here: nothing is pulled, loaded or warmed, and there is no database. The
conversation lives in the browser (or in `client.lua`) and is sent back with every turn.

## What to set on Railway

`bahs` -> Settings -> Variables:

| Variable | Value |
| --- | --- |
| `QWEN_TOKEN` | Qwen access token: chat.qwen.ai -> F12 -> Console -> `localStorage.token` |
| `DEEPSEEK_TOKEN` | *optional* — enables the **agent** and **deepseek** modes. Either an API key (`sk-...`, from platform.deepseek.com) or the site's own `userToken`: chat.deepseek.com -> F12 -> Console -> `JSON.parse(localStorage.getItem("userToken")).value`. Which one it is picks the endpoint on its own |
| `API_KEY` | *optional* — if you set it, the API (`/v1`, `/chat`, `/generate`, `/agent`) requires it. Left unset, the Qwen token is the key. The page never needs one |

Only `QWEN_TOKEN` is needed for **qwen** mode; only `DEEPSEEK_TOKEN` is needed for **deepseek**
mode; **agent** mode needs both. `CHAIN_MODE` picks which one a caller gets when it does not ask
for one (default `qwen`), and if that mode cannot run the service falls back to one that can — a
mode a caller asks for *by name*, on the other hand, is refused rather than swapped for another
model.

The one failure that looks like something else entirely is a build that is missing a module: the
image builds, uvicorn exits on the import error at startup, and every request comes back a `502`
from Railway with a body that names no reason at all -- which reads like the service refusing the
request rather than the service never having started. That is why the `Dockerfile` copies each
module by name *and* imports the app as its last step, so a module that was never added to the
`COPY` list fails the build instead of the deployment.

Redeploy. The page should read `api online · bridge qwen.aikit.club · token token accepted ·
deepseek token accepted · tools 8 on -- luau_check, luau_format, roblox_api... · roblox 682
classes · executor not listening -- run_script says so instead of waiting · model qwen3.8-max,
thinking on, with the toolbox · mode thinking · Hy kanha`.

### The DeepSeek credential

Both credentials work, and the endpoint follows the one you paste:

* **An API key** (`sk-...`) goes to `https://api.deepseek.com`, OpenAI-shaped. Set
  `DEEPSEEK_MODEL` if `deepseek-v4-flash` is not what your key serves — the `deepseek` chip says
  so, and names a nearby model the key *can* see.
* **The site's `userToken`** is not accepted by that API, so a token that is not an `sk-` key is
  sent to `chat.deepseek.com` instead, over the endpoints the web app itself calls. Every message
  there is gated by a proof of work, which is solved with the site's own sha3 module
  (`pow_solver.py`, `wasmtime`): the algorithm is neither SHA3-256 nor Keccak-256, so a
  reimplementation that is subtly wrong earns the same refusal as no answer at all. The module is
  fetched into the image at build time and, if it is not there, on first use.

Thinking is on for DeepSeek (`DEEPSEEK_THINKING`), and its reasoning is *not* the answer: on both
transports it is kept out of what comes back -- the plan or the script -- and handed to the
`thoughts` channel instead, which carries the model's own chain of thought as it arrives and is
what a client's thinking pane reads (`thoughts.py`). Search is never switched on anywhere in this
service.

## The tools

The schemas are in `luau.py`; the loop that runs them is `tool_loop` in `server.py`. All eight are
local to this image — no extra service, no extra key — except that `roblox_api` fetches the Roblox
API dump the first time it is called, and `web_get` reads a public page with `AGENT_WEB` on. Only
the writer is given them: **deepseek** mode attaches none.

| Tool | What it is |
| --- | --- |
| `luau_check` | Structural read of the script: blocks (`function`/`if`/`for`/`while`/`do`/`repeat` against `end`/`until`), unterminated strings and long strings, unbalanced brackets, and the calls that break in an executor (`wait()`, a Studio-only service, `require(assetId)`, a `while true do` with no wait). Errors and warnings carry line numbers |
| `roblox_api` | The real API dump: a class's members with parameters, return types and tags (`NotReplicated`, security levels), a member searched across classes, or a name that does not exist. This is what stops the model inventing `Humanoid:SetSpeed()` |
| `run_script` | Hands the script to the executor polling `/agent/pull`, waits for `/agent/push`, and returns what it printed and any traceback. Says so at once when nothing is listening rather than waiting |
| `apply_edit` | `find`/`replace` on the script in the conversation, refusing an ambiguous match instead of guessing — the cheap way to change one line |
| `secret_scan` | Webhooks, `sk-`/`hf_`/`ghp_` tokens, bearer strings, `key = "..."`, long hex. Reports the line and the kind, never the value |
| `luau_format` | Re-indents by block depth (whitespace only), for a script assembled from pieces |
| `luau_find` | The lines of a script matching a Lua pattern, numbered, with optional context — how the writer looks at the one part of a long script it is changing |
| `web_get` | Reads a page as text (Roblox documentation, a DevForum answer, a raw file) and hands it back trimmed. The only way to check how something is *used* rather than whether it exists; `AGENT_WEB=off` switches it off |

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
- The other buttons are **ask** (a turn, with the phase shown live), **mode** (which chain the next
  question takes: agent / qwen / deepseek), **execute** (run the answer yourself), **tools** (what
  the model did to its own work), **new chat** (a new session) and **copy**. In agent mode the plan
  is shown above the script.

## The Roblox client (`ghaith.lua`)

The panel you actually use in the game: it talks to `POST /chat/stream` and `GET /chat/result/{job}`
and keeps the whole conversation, so every turn goes out with all of it. Buttons: **ask**, the mode
picker, **WRITER** (thinking or fast — the setting travels with the turn, so one chat can be asked
either way), **scan game**, **run last**, **copy code** (the script and nothing else), **full
script**, **console**, and **auto**, which runs what it wrote, hands the console back, takes the fix
and repeats until the script stops changing. It fills the screen it was given minus the strip
Roblox keeps for its own buttons, and it and every window it opens are dragged by their bars — with
a finger, which `Draggable` cannot do.

**Thinking is mentioned, never printed.** The chain of thought arrives on the `thoughts` channel
and the header turns it into one line (`thinking · 12s · 340 chars thought`); it is not shown in a
pane and not copied. What the SCRIPT pane holds is exactly what **copy code** would copy, and prose
is never either of them: an answer that has not produced a script yet leaves the pane empty, and
one that never produces one ends the turn with the header saying so rather than `idle`.

**Its own tool protocol.** The model asks the client for what it needs by writing a token in its
answer — `@@GREP remote@@` or `@@GREP@@ remote`, both are read, and a multi-line argument
(`@@EXEC@@`, which is Luau rather than a path) ends at `@@` alone on a line. Eight calls per answer,
two passes per turn;
what the tools found goes back as the next turn, which is the agentic part. A line carrying a token
is never part of the script.

The 29 tools, and every one of them is about the game this client is running in rather than the
machine it is running on:

| | |
| --- | --- |
| **the game** | `@@DEEPSCAN@@`, `@@REMOTES@@`, `@@TREE path@@`, `@@PROPS path@@`, `@@FIND name@@`, `@@PLAYERS@@` |
| **its scripts** | `@@SCRIPTS@@`, `@@MODULES@@`, `@@SOURCE path@@`, `@@DECOMPILE path@@`, `@@GREP word@@`, `@@DUMP_STRINGS word@@` |
| **watching it** | `@@HOOK path@@`, `@@UNHOOK path@@`, `@@SPY@@`, `@@SIGNAL path Event@@`, `@@WATCH path Property@@` |
| **acting on it** | `@@FIRE path args@@` (fires a remote for real and reads the reply), `@@SET path Property value@@` |
| **inside a function** | `@@HOOKFN path@@`, `@@UPVALUES path@@`, `@@CONSTANTS path@@`, `@@GETGC word@@` |
| **this client** | `@@EXEC code@@`, `@@RUN@@`, `@@CONSOLE@@`, `@@SELF@@`, `@@ENV word@@`, `@@HTTP url@@` |

A path is read however the model wrote it: `ReplicatedStorage.X`, `game.ReplicatedStorage.X`,
`game:GetService("ReplicatedStorage").X`, a service name in the wrong case, a bare instance name,
and a path that misses a link is completed by name rather than refused. What the tool prints is the
*real* path, so the model can see where the instance was. When nothing matches, the answer is not
"there is no such thing" but the names in the game closest to the piece that failed — a tool that
reads the game is only useful if it can be pointed at something.

## Variables

| Variable | Default | Notes |
| --- | --- | --- |
| `CHAIN_MODE` | `qwen` | which mode runs when the caller asks for none: `agent`, `qwen` or `deepseek`. Falls back to one that can run |
| `QWEN_URL` | `https://qwen.aikit.club/v1` | point it at your own qwen-api deployment if the public one is rate limited |
| `QWEN_MODEL` | `qwen3.8-max` | the writer |
| `QWEN_THINKING` | `thinking` | the writer's setting, and the one a turn that asks for none gets: `fast`, `auto` or `thinking` — qwen-api's own enum. `thinking` is the default and the point: a whole script, reasoned, with the `reasoning_content` kept out of the answer and shown on the `thoughts` channel instead. A caller can ask for the other one per turn (`"thinking": "fast"`) and gets `QWEN_FAST_THINKING` |
| `QWEN_FAST_THINKING` | `fast` | what a `fast` turn sends instead — the same model, reached with the other value |
| `SCRIPT_RETRIES` | `2` | how many times one turn may tell the writer "that was not a script, send the script" before the turn fails. A paragraph about a script is not shipped as one |
| `AGENT_WEB` | `on` | `off` and `web_get` refuses instead of fetching |
| `WEB_MAX_CHARS` | `8000` | how much of a fetched page is kept |
| `WEB_TIMEOUT` | `20` | connecting to a page, in seconds |
| `DEEPSEEK_TOKEN` | — | an `sk-...` API key (aliases `DEEPSEEK_API_KEY`, `DEEPSEEK_KEY`) or the site's `userToken` |
| `DEEPSEEK_URL` | followed from the credential | `https://api.deepseek.com`, or `https://chat.deepseek.com` when the credential is a site `userToken` |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | the planner and the deepseek-mode writer. `deepseek-web` when the site transport is used and no model was asked for, because the site's model is whatever the account is set to |
| `DEEPSEEK_THINKING` | `on` | `on`/`thinking`/`enabled` or `off`. Dropped from the answer either way |
| `DEEPSEEK_SHAPE` | `openai` | `openai`, `web` (a bridge in front of the web chat: no system role, boolean toggles) or `deepseek-web` (the site's own endpoints — chosen automatically from the URL) |
| `DEEPSEEK_COOKIE` | — | a `cf_clearance` cookie, in case chat.deepseek.com ever answers a request with a browser check |
| `DEEPSEEK_TEMPERATURE` | `0.3` | a planner and a writer are not asked to be creative |
| `DEEPSEEK_TOKENS` | `8192` | ceiling on a DeepSeek answer that is a whole script |
| `DEEPSEEK_PLAN_TOKENS` | `4096` | ceiling on the plan in agent mode: it is prose, not a script |
| `DEEPSEEK_TIMEOUT` | `0` | no ceiling on a DeepSeek call, like the rest of the chain |
| `POW_WASM` | `sha3_wasm_bg.wasm` beside the code | where the sha3 module is looked for |
| `POW_MAX_TRIES` | `5000000` | the largest difficulty the proof of work will attempt |
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
| `SESSION_TTL` | `3600` | how long one session's continuation marker (and its DeepSeek chat) is kept. `/health` reports how many of each are held |
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
| ask | this service | The mode is resolved first (an unknown one is a 400, one whose credential is missing a 503 naming it), then the caller's turns are greeted and the session's continuation marker goes on the newest Qwen assistant turn |
| plan | DeepSeek | *agent mode only, exactly once.* The conversation with `PLAN_SYSTEM` in front of it, streamed to the page's `plan` channel. The answer is a build plan, not a script |
| answer | Qwen | One streaming call with the eight tool schemas attached, in the setting the turn asked for (`thinking` or `fast`), and the plan as the turn above it. The tool XML and the metadata are cut out of what is streamed, and so is `reasoning_content` -- not by being thrown away, but onto the `thoughts` channel, so a client can show the writer thinking while it writes |
| tool | this service | If the call asked for tools: each is run, one trace line per call goes to the page's `tool` channel, and the results go back as `tool` turns |
| round | Qwen | Asked again, in the same chat, with the results in front of it — until an answer asks for nothing |
| guard | this service | An empty answer, one cut off at the token ceiling, or a content filter is refused. A turn that used its last round on a tool call ships the best script it wrote on the way |
| check | this service | Is the answer a script? A paragraph about one is not: it is handed back with the reason (`only 2 of 6 lines read as Lua`) and the writer is asked again, in the same chat, up to `SCRIPT_RETRIES` times — and then the turn *fails*, with the reason in it, rather than reporting an answer nobody can paste anywhere. The one answer that is not a script and not a failure is a list of calls for the Roblox client: those ship, because the client runs them |
| ship | this service | The script is the answer. The plan, the tool trace and the per-call records stay under it and are not carried into the next question |

A few properties worth knowing:

* **Nothing is cut off for being slow.** There is no ceiling on a model call by default, and
  connecting is still bounded to 10s, so an unreachable host fails in seconds instead of looking
  like a model that is thinking.
* **A fence is taken off, not shipped.** Both models are told, in as many words, never to wrap the
  script in a markdown fence (it is pasted straight into an executor, where a fence line is a
  syntax error). When one does it anyway the turn is not refused: a whole-answer block is
  unwrapped, a script fenced with talk around it (or two fenced versions) is extracted, and a stray
  fence line is dropped. An answer that is *nothing but* fenced blocks — one script the model split
  across "part one" and "part two" — has its pieces put back together in the order they were
  written, rather than the longest one being shipped on its own and looking whole. Every other line
  is left exactly as it was. What `text` carries is therefore the finished script and no fence, so
  a client that pulls code out of an answer must accept it **bare**: a client that only looks inside
  ```lua blocks finds nothing at all on this endpoint.
* **A second model is not an opinion.** In agent mode DeepSeek produces the plan and is never
  asked again; the plan is context for the writer, not a verdict on its script.
* **Secrets do not leave for a provider.** Webhooks, tokens, `key = "..."` assignments and long
  hex are replaced with `<redacted>` in the copy sent upstream, and `secret_scan` reports them to
  the model. Your script on the page is untouched.
* **Everything is measured.** Every call is logged and returned as `phases`: phase, model,
  milliseconds, characters, finish reason, token usage, and which tools it called.
* **What was sent is logged too**, not just what came back (`[upstream] qwen -> qwen3.8-max: 3
  message(s), 1542 chars, max_tokens 8192, 6 tool(s)`), which is what makes "it only sent part of
  the conversation" answerable from the service's own log.

## Endpoints

| Endpoint | What it is |
| --- | --- |
| `POST /chat/stream` | `{messages:[{role,content},...], session?: str, mode?: str, thinking?: str}` -> `{job, mode, model, session, thinking, tools, turns, timeout}`. Starts the turn, returns at once. `thinking` is the writer's setting for this turn: `thinking` (the default) or `fast` |
| `GET /chat/stream/{job}` | NDJSON: `{replay}`, `{t, ch}` pieces (`answer` / `tool` / `plan` / `thoughts`), `{reset, ch}`, `{phase, note}`, `{beat}`, then `{done, text, tool, plan, thoughts, mode, session, phases}` or `{error}` |
| `POST /chat` | The same turn, blocking. `{text, tool, plan, thoughts, mode, session, phases}` |
| `GET /chat/result/{job}` | The same thing as one JSON object, for callers that cannot hold a stream open (Roblox). Needs the API key |
| `GET /chat/poll/{job}` | The same fields plus `done`, in a response that closes at once. Not key-gated: it is what the page falls back to when a phone network keeps cutting the stream |
| `POST /generate` / `POST /generate/stream` | One prompt, no history. The session and the mode still apply |
| `GET /agent/pull?client=...` | The next script the model queued for the executor, or nothing. Needs the API key |
| `POST /agent/push` | `{run, ok, output, error}` — the executor's answer for one run |
| `POST /v1/chat/completions` | OpenAI-compatible passthrough to Qwen. The newest user turn gets the greeting and the model is pinned; `tools`, `web_search_options`, `reasoning_effort`, `stream` pass through. No tool loop here — a tool call comes back to you, which is what an OpenAI client expects |
| `GET /v1/models` | The writer, then DeepSeek when it is configured, then whatever else the proxy serves |
| `GET /health` | bridge, the Qwen token, the DeepSeek credential, the three modes and which can run, the writer's setting and the two it can be, tools (on, names, dump state, executor state), sessions, limits, last error |
| `GET /` | the chat page, with the mode picker |

An OpenAI client needs two lines changed:

```python
client = OpenAI(base_url="https://<your-domain>/v1", api_key="<API_KEY>")
```

## The page needs no login

The chat endpoints are deliberately not key-gated, because the page is used without signing in.
What protects the accounts behind it is the rate limit and the concurrency ceiling instead. If that
is not enough for you, raise `API_KEY` and put the page behind something else — the API surfaces
(`/v1`, `/chat`, `/generate`, `/agent`) are gated, and the page's own endpoints are not.

## Curl

```bash
# qwen mode (the default) -- or add "mode":"agent" / "mode":"deepseek"
curl -s https://<your-domain>/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}],"session":"my-chat-1","mode":"agent"}'
# -> {"job":"...","mode":"agent","model":"deepseek-v4-flash plans, then qwen3.8-max writes",
#     "session":"my-chat-1","thinking":"thinking","tools":[...],"turns":1}

curl -sN https://<your-domain>/chat/stream/<job> -H "X-API-Key: $API_KEY"

# or without a stream at all
curl -s https://<your-domain>/chat -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"make walkspeed 100"}],"session":"my-chat-1","mode":"deepseek"}'
```

## Verifying a change

`verify_chain.py` runs the three modes against a stubbed pair of models and the toolbox against
itself — no keys, no network (both models are one local stub, and the API dump is a fixture). It
covers: one call to one model with thinking on and the schemas attached; a tool round trip in both
the XML and the OpenAI shape (fragmented arguments and all); the tool result going back and being
matched to its call; the interim prose and the tool XML never reaching the reader; the writer's
thinking arriving on the `thoughts` channel and never in the answer or the finished text; a script
the model split across two fenced blocks being put back together in order rather than half of it
shipping; the session keeping one chat and no other session getting it; the executor round trip behind `run_script`
(including the refusal when nothing is listening); the round limit; a cut-off answer being refused;
the toolbox's own units; and, on the answer itself: a paragraph read as prose and asked again until a
script comes back, a model that never gets there failing loudly instead of reporting an answer, a
list of calls for the Roblox client shipping rather than being refused, `fast` reaching the same
model with the other setting and still continuing the session's chat, and a fetched page reduced to
its words. And the modes: agent calling DeepSeek exactly once and Qwen exactly once
in that order, the plan streaming on its own channel and never into the answer, a second model
being unable to install or clear the session's Qwen continuation, deepseek mode attaching no tools,
a mode whose credential is missing being refused by name, the default following `CHAIN_MODE` and
falling back when it cannot run, and the surface (`/health`, `/v1/models`, the page, the picker,
the gate). Last, the Roblox client itself (`ghaith.lua`), read as the other half of the tool
protocol: its own tool table, that everything in it reads the game rather than the player's
machine, that the count its header claims is the count there is, and that neither the SCRIPT pane
nor **copy code** can end up holding a paragraph of the model's notes.

```bash
.venv/bin/python verify_chain.py
```

The service is six modules, smallest dependency first:

* `bridge.py` — everything that talks to a model: the tokens, the config, one request and its
  stream, the two transports, the marker cut out of it, and the sessions that keep one chat each
  (plus the three modes and what each model is told).
* `pow_solver.py` — the proof of work chat.deepseek.com wants on every message, solved with the
  site's own sha3 module.
* `state.py` — what the service can say about itself (both credentials, the model list, the last
  failure) and the two usage limits.
* `luau.py` — the toolbox: the eight tools, the Roblox API dump, and the queue the executor polls.
* `thoughts.py` — the writer's stream with its chain of thought kept: the same call as
  `bridge.stream_call`, except that every reasoning fragment is handed to the job's `thoughts`
  channel as it arrives. A copy rather than a wrapper, because the fragment has to be caught
  inside the loop that reads the provider's frames and there is no seam outside it.
* `server.py` — the service on top: the modes, the writer's setting, the turn, the tool rounds,
  the answer check, the jobs, the endpoints and the page.
