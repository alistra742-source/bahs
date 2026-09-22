"""The service: the modes, the jobs, the endpoints and the page.

Everything that talks to a model -- the tokens, the config, one request, its stream, and the two
kinds of continuation that keep one upstream chat instead of a new one per question -- lives in
`bridge`. The toolbox the writer calls on itself lives in `luau`. What is left here is the
service: the job that runs one turn, the loop that runs the tools, and the endpoints the page and
the Roblox client use.

A turn runs in one of three modes, and which is the caller's to pick:

    agent     the caller's turns
                -> deepseek, once, with the planner's instruction -> a plan
                -> qwen3.8-max, thinking on, with the plan and the tool schemas attached
                     -> if it calls a tool: run it here, hand the result back, ask again
                     -> the script it settles on is the answer
    qwen      the same writer on its own, no plan in front of it
    deepseek  deepseek on its own: it writes the script and no tools are attached

Agent mode is two models called once each, not a negotiation: the plan is context for the writer
and is never handed back to the planner or reviewed by it. What follows the plan is the writer's
own work, which is what `luau_check` (structure), `roblox_api` (the real API dump), `web_get` (how
something is used) and `run_script` (the connected executor) are for.

The writer has two settings, and the caller picks one per turn: `thinking` (the default) has it
reason the script out before writing it, `fast` has it spend those tokens on the script. Either
one can be asked for on any turn of the same conversation.

And what the turn produces is checked before it is called an answer. A paragraph about a script is
not a script: it is handed back to the writer with the reason and the writer is asked again, and a
turn that never gets there fails saying so -- it used to ship, marked answered, with the client
showing a paragraph where the script goes. The exception is a list of calls for the Roblox client
(`@@DEEPSCAN@@`, `@@GREP@@ word`), which cannot be a script and is not one: that ships, because the
client runs those calls and asks its next question with what they found.
"""
from bridge import *  # noqa: F401,F403 -- the providers, the config and the session stores
from thoughts import stream_with_thoughts  # the same writer's stream, its thinking kept
from luau import (TOOLS, TOOL_NAMES, deliver, executor_state, run as run_tool, take_script,
                  tool_state, tools_enabled)  # noqa: F401 -- the toolbox
from state import (client_ip, deepseek_state, last_error, list_models, note_error, rate_ok,
                   running_now, slot_give, slot_take, token_state)  # noqa: F401 -- the chips


# --- jobs ------------------------------------------------------------------------------

class Job:
    """One turn, owned by a background thread rather than by the caller.

    A phone that gave up on an answer used to take the whole generation with it: the request
    was the only thing driving the model, so nothing was left to read. A job runs to
    completion on its own thread, keeps every piece it has produced, and any number of
    readers can attach to it -- including one that comes back after the connection dropped,
    which replays the output from the start and follows along.

    Text arrives on four channels. `answer` is the script the writer is producing -- the pieces
    the model streams, with the tool XML and the continuation metadata cut out. `tool` is what
    happened instead of text: one trace line per tool call, so a turn that spends a minute
    looking up an API is visibly doing that rather than looking stuck. `plan` is the same idea for
    agent mode's first call, which is a plan rather than an answer and is none of the answer.
    `thoughts` is the writer's own chain of thought, which the provider streams on every call while
    thinking is on: it is what the client's thinking pane reads, and it is never the answer.
    """

    def __init__(self, messages: list, temperature: Optional[float], note: str, session: str,
                 mode: str, thinking: str = "", files=None, base_url: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.messages = messages
        self.temperature = temperature
        self.note = note
        self.session = session
        self.mode = mode
        # What the caller attached to this turn, by id, and the address this service is reached at:
        # a document is handed to the writer as a URL to fetch, so it has to be this service's own.
        self.files = [str(f) for f in (files or [])]
        self.base_url = base_url
        # The text of the turn an attachment belongs to, filled in once the turns are built.
        self.question = ""
        # The writer's setting for this turn, decided before the turn starts: "thinking" or
        # "fast". It is per turn, not per service, because the same question can want either --
        # a quick edit to a line does not need a minute of reasoning, and a remote protocol does.
        self.thinking = writer_thinking(thinking)
        self.writer = writer_for(self.thinking)
        self.provider = self.writer  # the provider of the call in flight, for the error it raises
        self.pieces: list = []          # (channel, piece)
        self.buffers: dict = {"answer": [], "tool": [], "plan": [], "thoughts": []}
        self.tool_text = ""             # the same trace as one string, for the poll and the summary
        self.error = ""
        self.status = "queued"          # queued -> running -> done | error
        self.phase = "queued"           # queued | draft | tool | done
        self.phases: list = []          # one record per model call
        self.started = time.time()
        self.finished = 0.0
        self.cond = threading.Condition()

    def channel(self, name: str) -> str:
        return "".join(self.buffers.get(name, []))

    def text(self) -> str:
        """What the caller asked for: the script that stands."""
        return self.channel("answer")

    def add(self, channel: str, piece: str) -> None:
        with self.cond:
            self.pieces.append((channel, piece))
            self.buffers.setdefault(channel, []).append(piece)
            self.cond.notify_all()

    def reset_channel(self, channel: str) -> None:
        """Throw away what a channel has produced so far.

        The callers are the tool rounds and the guard on the answer: a turn that ends in a tool
        call has streamed prose that is not the answer, and a version that came back cut off has
        to be replaced by the script that stands rather than shown above it. The reset is itself
        a piece, so a reader that attaches later replays the same sequence.
        """
        with self.cond:
            self.pieces.append((channel, None))
            self.buffers[channel] = []
            self.cond.notify_all()

    def finish(self, **fields) -> None:
        """Publish the outcome and wake every reader waiting on it."""
        with self.cond:
            for name, value in fields.items():
                setattr(self, name, value)
            if self.status in ("done", "error"):
                self.finished = self.finished or time.time()
            self.cond.notify_all()

    def wait(self, timeout: float) -> None:
        """Block until the job ends; the blocking endpoints are the only callers.

        A `timeout` of 0 or less means wait however long the turn takes, which is the default:
        tool rounds depend on a model and, for `run_script`, on an executor, so there is no
        honest number of seconds to cut it off at. A caller that does want a ceiling passes one.
        """
        deadline = time.time() + timeout if timeout and timeout > 0 else 0.0
        with self.cond:
            while self.status not in ("done", "error"):
                if not deadline:
                    self.cond.wait()
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise HTTPException(504, f"timed out after {timeout:g}s waiting on the turn")
                self.cond.wait(timeout=remaining)

    def report(self) -> dict:
        """Where the job stands; what the page shows next to its running timer."""
        return {
            "status": self.status,
            "phase": self.phase,
            "note": self.note,
            "mode": self.mode,
            "model": mode_label(self.mode),
            "session": self.session,
            # Agent mode's plan is made by DeepSeek and the script by Qwen; the writer's setting
            # is the one that describes the answer, so that is what is reported -- and it is the
            # choice this turn was started with, not the service's own default.
            "thinking": (thinking_label(DEEPSEEK_THINKING) if self.mode == MODE_DEEPSEEK
                         else thinking_label(self.thinking)),
            "calls": len(self.phases),
            "tools": len([p for p in self.phases if p["phase"] == "tool"]),
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "chars": len(self.text()),
            # How much the writer thought. The text itself is on the job's `thoughts` channel, so a
            # reader gets it as it arrives rather than in one lump at the end.
            "thought_chars": len(self.channel("thoughts")),
            # How many files ride on this turn, so a client can say what the model was given.
            "files": len(self.files),
        }


_jobs: dict = {}
_jobs_lock = threading.Lock()


def register(job: Job) -> None:
    """Remember the job so a reader that comes back can still find its own."""
    with _jobs_lock:
        _jobs[job.id] = job
        stale = [jid for jid, j in _jobs.items() if j.finished and time.time() - j.finished > JOB_TTL]
        for jid in stale:
            _jobs.pop(jid, None)


def lookup(job_id: str) -> Job:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown or expired job; send the turn again")
    return job


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Nothing to load or warm: there are no local weights, only API calls (and one toolbox that
    # needs no runtime of its own).
    if CONFIGURED:
        print(f"[bridge] {QWEN_URL} -> {QWEN_MODEL} (thinking: {QWEN_THINKING})", flush=True)
    else:
        print(f"[bridge] {NOT_CONFIGURED}", flush=True)
    # Which of the three modes this service can actually run, said at boot: the picker on the page
    # reads the same thing from /health, and a mode that is off is off for a named reason.
    print(f"[modes] default {default_mode()}; on: "
          f"{', '.join(m['id'] for m in modes_state() if m['on']) or 'none'}", flush=True)
    if not DEEPSEEK.configured:
        print("[modes] DEEPSEEK_TOKEN is not set: the agent and deepseek modes are off",
              flush=True)
    elif DEEPSEEK.web is not None:
        # Not loaded here: the first fetch is a network call, and boot should not wait on it.
        print(f"[deepseek] {DEEPSEEK.web.label} (thinking "
              f"{'on' if DEEPSEEK.web.thinking else 'off'}, search off), proof of work: "
              + (f"{pow_solver.MODULE_PATH.name} is in the image"
                 if pow_solver.MODULE_PATH.exists()
                 else "no sha3 module in the image, so one is fetched on the first call"),
              flush=True)
    else:
        print(f"[deepseek] {DEEPSEEK.url} -> {DEEPSEEK.model} "
              f"(thinking {DEEPSEEK_THINKING}, search off)", flush=True)
    if tools_enabled() and AGENT_ROUNDS > 0:
        print(f"[tools] {len(TOOL_NAMES)} tool(s) on, up to {AGENT_ROUNDS} round(s) a turn: "
              f"{', '.join(TOOL_NAMES)}", flush=True)
        print(f"[tools] the Roblox API dump is fetched on first use "
              f"(ROBLOX_API_DUMP={env('ROBLOX_API_DUMP') or 'not set, so setup.rbxcdn.com'})",
              flush=True)
        if env("AGENT_RUN", default="on").lower() in ("off", "0", "false", "no"):
            print("[tools] AGENT_RUN=off: the model cannot execute anything", flush=True)
    else:
        print("[tools] no tools attached; the model answers from what it knows", flush=True)
    if ATTACH_MAX > 0:
        print(f"[attach] up to {ATTACH_MAX} file(s) a turn, "
              f"{ATTACH_MAX_BYTES // 1048576} MB each, kept {ATTACH_TTL:g}s; a picture is sent as "
              "a data URI and a document as a URL back out of /attach/<id>", flush=True)
    else:
        print("[attach] off (ATTACH_MAX_FILES=0): /attach refuses everything", flush=True)
    if GREETING:
        print(f"[chat] every question is sent as {GREETING} <your question>", flush=True)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/chat/poll/{job_id}")
def poll_job(job_id: str):
    """Where a turn stands, as one short JSON object.

    The page reads a turn through /chat/stream/{job}, which is one long-lived response -- and a
    phone network or a proxy is entitled to cut those. When that keeps happening, this is what the
    page falls back to: the same information in a response that closes at once. Not key-gated, for
    the same reason the stream is not (the page holds no key), and it reveals nothing a reader of
    that stream could not already see -- a job id is 12 random hex characters, and only the reader
    that started the turn has it. `done` is what a poller waits for; `text` is the answer.
    """
    job = lookup(job_id)
    body = {
        "job": job.id,
        "status": job.status,
        "phase": job.phase,
        "note": job.note,
        "text": job.text(),
        "tool": job.tool_text,
        "plan": job.channel("plan"),
        "thoughts": job.channel("thoughts"),
        "mode": job.mode,
        "model": job.report()["model"],
        "session": job.session,
        "phases": job.phases,
        "done": job.status == "done",
    }
    body.update(job.report())
    if job.status == "error":
        body["error"] = job.error
    return body


def require_key(x_api_key: Optional[str] = Header(None),
                authorization: Optional[str] = Header(None)) -> None:
    """Gate the *API* surfaces (/v1, /generate, /chat, /agent) when a key is defined.

    The key is API_KEY when it is set, and otherwise the Qwen token itself -- one secret to
    keep. The page is deliberately not gated (it is used without a login), so what protects the
    service is the rate limit and the concurrency ceiling, not this.

    This is the only credential a caller ever handles: the Qwen token stays here, so it never
    reaches a browser, a Roblox client or a log.
    """
    if not CALLER_KEY:
        return
    supplied = x_api_key or ""
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, CALLER_KEY):
        raise HTTPException(401, "missing or invalid API key (send it as the X-API-Key header)")


# --- the chain itself --------------------------------------------------------------------
#
# One model call, its stream, and the tool rounds around it. The provider side -- the request, the
# stream, and what a failure means -- is in bridge.py, next to the provider itself.

def stream_any(provider, messages: list, temperature: Optional[float], max_tokens: int, box: dict,
               tools: Optional[list] = None, web_session: object = None):
    """One model call, on whichever transport that provider uses.

    Two of the three speak OpenAI-shaped HTTP, and that path is `bridge.stream_call`. The third is
    chat.deepseek.com: one `prompt` field, the site's own frames, and a proof of work on every
    message, so it has its own transport -- and it takes no tools, which is why none are offered
    to it here.
    """
    if provider.web is not None:
        pieces: list = []
        try:
            for piece in provider.web.stream(as_prompt(messages), box, web_session):
                pieces.append(piece)
                yield piece
        except httpx.HTTPError as e:
            # The site's own transport has no timeout mapping of its own -- bridge.upstream_error is
            # what names a stream that went quiet rather than a host that cannot be reached.
            raise upstream_error(e, provider) from e
        if box is not None:
            box["raw"] = "".join(pieces)
            box["tool_calls"] = []
            # None, not "": this answer carries no Qwen continuation marker, and an empty one
            # would forget the session's -- which, in agent mode, is what would end the Qwen chat
            # the planner call was made in.
            box["meta"] = None
        return
    # The thinking-aware copy rather than the bridge's own: the same stream, except that the
    # writer's chain of thought is kept (see thoughts.py) so a client has something real to show
    # while the script is being written.
    yield from stream_with_thoughts(messages, temperature, provider, max_tokens, box, tools)


def run_phase(job: Job, messages: list, temperature: Optional[float], max_tokens: int,
              channel: str, phase: str, note: str, tools: Optional[list] = None,
              provider=None, web_session: object = None) -> tuple:
    """Stream one model call into a channel and record what it cost.

    Every call goes through here, so every call ends up in the job's phase record: which model,
    how long, how many characters, whether it stopped early, and which tools it asked for. The
    channel is rebuilt from the raw answer first, so what a reader sees and what is stored never
    contain a tool call's XML or the continuation metadata.

    The Qwen continuation marker only rides on Qwen's turns: a DeepSeek call in the same session
    must not add it, and must not be allowed to forget it either.
    """
    provider = provider or QWEN
    job.provider = provider
    job.finish(phase=phase, note=note)
    box: dict = {"finish": None, "usage": None, "tool_calls": [], "raw": "", "meta": ""}
    started = time.time()
    pieces: list = []
    thoughts: list = []

    def note_thought(fragment: str) -> None:
        """One reasoning fragment as it arrives: onto the job's thoughts channel, live.

        This is the whole reason the stream is the thinking-aware one -- the writer is reasoning
        about an API it cannot see, and a client that shows that reasoning is showing the turn
        actually happening rather than a spinner.
        """
        thoughts.append(fragment)
        job.add("thoughts", fragment)

    box["thoughts"] = note_thought
    turns = with_continuation(messages, job.session) if provider in QWEN_PROVIDERS else messages
    # What was attached rides on the newest question, and only the writer is given it as a file:
    # Qwen takes a picture or a document as part of the turn, while DeepSeek has no vision on either
    # transport and is told what is attached rather than handed something it cannot read.
    if job.files:
        turns = (with_attachments(turns, job.files, job.base_url, job.question)
                 if provider in QWEN_PROVIDERS
                 else with_note(turns, attach_note(job.files)))
    for piece in stream_any(provider, turns, temperature, max_tokens, box, tools, web_session):
        pieces.append(piece)
        job.add(channel, piece)
    streamed = "".join(pieces)
    # The answer without the hidden parts: the XML shape of a tool call, and the metadata that
    # continues the upstream chat. The metadata is kept for the session before it is dropped.
    visible = strip_metadata(TOOL_XML.sub("", box["raw"])).strip()
    # Only a Qwen answer carries this service's continuation marker, so only a Qwen answer may
    # install or clear it: another model's answer -- and a decoy that looks like a marker -- must
    # leave the session's chat alone.
    session_remember(job.session, box.get("meta") if provider in QWEN_PROVIDERS else None)
    if streamed != visible:
        job.reset_channel(channel)
        job.add(channel, visible)
    calls = box.get("tool_calls") or []
    record = {
        "phase": phase,
        "provider": provider.name,
        "model": provider.model,
        "ms": int((time.time() - started) * 1000),
        "chars": len(visible),
        "finish": box["finish"],
        "usage": box["usage"],
        "tools": [call["name"] for call in calls],
        "thought_chars": len("".join(thoughts)),
    }
    job.phases.append(record)
    print(f"[job] {job.id} {phase}: {provider.model} {record['ms']}ms, {record['chars']} chars, "
          f"finish={record['finish']}"
          + (f", calls {record['tools']}" if record["tools"] else ""), flush=True)
    return visible, record, box


def _short(value, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."


def tool_trace(call: dict, result: dict, limit: int = 1200) -> str:
    """One tool call as the page shows it: what was asked, then what came back."""
    body = result["output"] if len(result["output"]) <= limit else \
        result["output"][:limit] + f"\n[... {len(result['output']) - limit} chars more ...]"
    arguments = ", ".join(f"{k}={_short(v)}" for k, v in (call.get("arguments") or {}).items())
    return (f"{'ok ' if result['ok'] else 'no '}{call['name']}({_short(arguments)}) "
            f"-> {result['summary']}\n{body}\n")


def code_blocks(text: str) -> list:
    """Every fenced block in an answer that looks like a script, longest first.

    The fallback for a turn that used its last round on a tool call: the script it wrote is
    inside the content of a message that also carried the call, and it is better than nothing.
    """
    found = [match.group(1).strip()
             for match in re.finditer(r"```[A-Za-z0-9_+-]*\s*\n(.*?)```", text or "", re.S)]
    return sorted([block for block in found if looks_like_code(block)], key=len, reverse=True)


def best_script(seen: list) -> str:
    """The best script this turn produced, out of the content of every call it made.

    A fenced block is an explicit script, so it wins; a whole message that is nothing but Lua is
    next; and the tool XML a message may also be carrying is cut out before either is measured,
    because a script with a tool call attached is not the script.
    """
    for text in seen:
        blocks = code_blocks(text)
        if blocks:
            return blocks[0]
    for text in seen:
        plain = TOOL_XML.sub("", text).strip()
        if looks_like_code(plain):
            return strip_fences(plain)
    return ""


def tool_loop(job: Job, turns: list, tools: Optional[list]) -> tuple:
    """Qwen asked once, then asked again with whatever its tools said, until it answers.

    The loop is the whole point of the toolbox. A call that comes back asking for a tool has not
    answered yet, so the tool is run, its result is appended to the conversation, and the model is
    asked again -- its own check, its own lookup and its own run, in the chat it is already in.
    What comes back is the first answer that asks for nothing.

    Returns the answer, how many tools were called, whether the rounds ran out, and what every
    call in between wrote (the last of which is where a script is recovered from when the rounds
    do run out).
    """
    writer = job.writer
    answer = ""
    seen: list = []
    calls_made = 0
    rewrites = 0
    asked_again = False      # the last thing it heard was "that was not a script"
    out_of_rounds = False
    rounds = AGENT_ROUNDS + 1
    for round_index in range(rounds):
        first = round_index == 0
        note = (f"{writer.model} writing a draft" if first
                else (f"{writer.model} writing the script again" if asked_again
                      else f"{writer.model} answering after its last tool call"))
        answer, record, box = run_phase(
            job, turns, job.temperature, ANSWER_TOKENS if first else REFINE_TOKENS, "answer",
            "draft" if first else "tools", note, tools, writer)
        calls = box.get("tool_calls") or []
        if not calls:
            # It stopped calling tools -- but a turn that stops is not a turn that answered. Prose
            # about the script, or a list of tools it only meant to call, is the one failure that
            # would otherwise ship as if it were the script: the caller copies a paragraph into an
            # executor and the turn says answered. So the answer is judged before the loop ends,
            # and a model that wrote prose is told so, in the chat, and asked once more.
            ok, why = script_verdict(answer)
            # A list of calls for the Roblox client is not prose about a script: the client runs
            # them and asks again, so the turn is over as far as this service is concerned.
            if ok or tool_request(answer) or rewrites >= SCRIPT_RETRIES:
                break
            rewrites += 1
            asked_again = True
            if box["raw"].strip():
                seen.append(box["raw"])
            job.reset_channel("answer")
            turns.append({"role": "assistant", "content": box["raw"]})
            turns.append({"role": "user", "content": prose_correction(why)})
            job.finish(phase="rewrite",
                       note=f"no script in that answer ({why}); asking {writer.model} again")
            print(f"[job] {job.id} prose answer ({why}); asking again", flush=True)
            continue
        calls_made += len(calls)
        asked_again = False
        # Whatever it wrote while calling a tool is not the answer; the turn is not over.
        job.reset_channel("answer")
        if box["raw"].strip():
            seen.append(box["raw"])
        turns.append({"role": "assistant", "content": box["raw"],
                      "tool_calls": [{"id": call["id"], "type": "function",
                                      "function": {"name": call["name"],
                                                   "arguments": json.dumps(call["arguments"])}}
                                     for call in calls]})
        for call in calls:
            job.finish(phase="tool", note=f"{call['name']} -- running")
            result = run_tool(call["name"], call["arguments"], job.session)
            trace = tool_trace(call, result)
            job.tool_text += trace
            job.add("tool", trace)
            output = result["output"]
            if len(output) > TOOL_RESULT_MAX:
                output = output[:TOOL_RESULT_MAX] + "\n[the tool output was cut for length]"
            turns.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            print(f"[job] {job.id} tool {call['name']}: "
                  f"{'ok' if result['ok'] else 'failed'} -- {result['summary']}", flush=True)
        if round_index == rounds - 1:
            out_of_rounds = True
            job.finish(phase="tools",
                       note=f"{writer.model} used all {AGENT_ROUNDS} tool round(s)")
    return answer, calls_made, out_of_rounds, seen


def writer_system() -> str:
    """The writer's standing instruction, and the tools it is told about."""
    return TOOL_SYSTEM if (tools_enabled() and AGENT_ROUNDS > 0) else EXECUTOR_NOTE + "\n\n" \
        + ANSWER_RULE


def think_turn(job: Job) -> tuple:
    """qwen mode: the writer on its own, with the toolbox."""
    tools = TOOLS if (tools_enabled() and AGENT_ROUNDS > 0) else None
    turns = [{"role": "system", "content": writer_system()}] + list(job.messages)
    return tool_loop(job, turns, tools)


def plan_turn(job: Job) -> tuple:
    """agent mode: deepseek plans once, then qwen writes the script from the plan.

    Two models, called once each -- there is no round of negotiation between them and no review of
    the plan. DeepSeek is asked what to build and how, the answer is put in front of the writer as
    the thing it is building from, and everything after that is the writer's own tool rounds,
    which are its own work rather than a second model's opinion.
    """
    plan, record, box = run_phase(
        job, [{"role": "system", "content": PLAN_SYSTEM}] + list(job.messages),
        DEEPSEEK_TEMPERATURE, DEEPSEEK_PLAN_TOKENS, "plan", "plan",
        f"{DEEPSEEK.model} planning", None, DEEPSEEK, deepseek_chat(job.session))
    # Unwrapped, not extracted: a plan is prose and may quote a line of Lua, so the fences come off
    # and the plan stays whole. (`strip_fences` is for an answer, where the script is the answer.)
    plan = unwrap_fences(plan)
    tools = TOOLS if (tools_enabled() and AGENT_ROUNDS > 0) else None
    turns = [{"role": "system", "content": writer_system()}] + list(job.messages)
    if plan:
        # The plan is a turn of its own rather than a line inside the question: it is what the
        # writer is answering from, and the question is what it is answering.
        turns.append({"role": "user", "content": f"The plan to build from:\n\n{plan}"})
    else:
        # A planner that answered with nothing costs the turn one call and nothing else: the
        # writer is asked anyway, and the job says which half did not happen.
        job.finish(phase="plan", note=f"{DEEPSEEK.model} returned no plan; "
                                       f"{job.writer.model} writes it alone")
    return tool_loop(job, turns, tools)


def solo_turn(job: Job) -> tuple:
    """deepseek mode: it writes the script itself, and no tools are attached.

    Nothing checks the answer on this path -- the toolbox is Qwen's, and there is no Qwen call
    here -- so the model is told as much, and the script is taken as it comes. What is still
    checked is that there *is* one: this model talks its way towards the script out loud more than
    the writer does, so a prose answer is handed back to it with the reason, the same way the
    writer's is.
    """
    writer_says = f"{DEEPSEEK.model} writing"
    turns = [{"role": "system", "content": WRITE_SYSTEM}] + list(job.messages)
    seen: list = []
    rewrites = 0
    while True:
        answer, record, box = run_phase(
            job, turns, DEEPSEEK_TEMPERATURE, DEEPSEEK_TOKENS, "answer", "draft",
            writer_says, None, DEEPSEEK, deepseek_chat(job.session))
        ok, why = script_verdict(answer)
        if ok or rewrites >= SCRIPT_RETRIES:
            break
        rewrites += 1
        if box["raw"].strip():
            seen.append(box["raw"])
        job.reset_channel("answer")
        turns.append({"role": "assistant", "content": box["raw"]})
        turns.append({"role": "user", "content": prose_correction(why)})
        job.finish(phase="rewrite",
                   note=f"no script in that answer ({why}); asking {DEEPSEEK.model} again")
        writer_says = f"{DEEPSEEK.model} writing the script again"
        print(f"[job] {job.id} prose answer ({why}); asking again", flush=True)
    return answer, 0, False, seen


def run_job(job: Job) -> None:
    """One turn, in the mode the caller picked, around a tail every mode shares.

    The modes differ in who writes and in what is attached to the writer. What follows -- what the
    answer is checked for, what is refused, and what the job reports -- is the same for all three,
    so it lives here rather than being spelled out three times.
    """
    if not slot_take():
        job.finish(status="error",
                   error=f"{MAX_CONCURRENT} turns are already running; try again shortly")
        return
    try:
        if job.mode == MODE_DEEPSEEK:
            answer, calls_made, out_of_rounds, seen = solo_turn(job)
        elif job.mode == MODE_AGENT:
            answer, calls_made, out_of_rounds, seen = plan_turn(job)
        else:
            answer, calls_made, out_of_rounds, seen = think_turn(job)
        script_ok, why = script_verdict(answer)
        if not script_ok or out_of_rounds or not answer.strip():
            # Either the last thing it wrote was a message that carried a tool call rather than an
            # answer, or what it settled on is not a script at all. Both are recovered the same way:
            # the script is taken out of what it wrote on the way -- a fenced block if there is one,
            # and a message itself only when it is nothing but Lua -- and kept if that is better than
            # the answer that failed the test.
            recovered = best_script(seen or [answer])
            if recovered:
                answer, why = recovered, ""
        answer = strip_fences(answer)
        notes, usable = structural_notes(answer, job.phases[-1]["finish"] if job.phases else None)
        if not usable:
            reason = "; ".join(notes) or "the model returned nothing"
            raise HTTPException(502, f"{reason} -- try again, or raise ANSWER_TOKENS")
        script_ok, why = script_verdict(answer)
        if not script_ok and tool_request(answer):
            # Not a script, and not a failure either: this is the Roblox client's own protocol. The
            # answer is a list of calls, the client runs them, and its next turn carries what they
            # found. Shipping it is the whole point of the tokens.
            job.finish(phase="tools",
                       note=f"{job.writer.model} asked the client's tools; waiting on them")
        elif not script_ok:
            # The one failure that used to ship: a turn marked answered whose answer is a paragraph.
            # Saying so is the whole point -- the client shows this instead of a "script" nobody can
            # paste anywhere, and the caller knows the turn produced nothing rather than guessing.
            raise HTTPException(502, f"the model answered with {why} instead of a script, and did "
                                     f"not write one when it was asked again -- send the question "
                                     f"once more, or switch mode")
        if job.channel("answer").strip() != answer:
            job.reset_channel("answer")
            job.add("answer", answer)
        note_error("")
        used = sorted({name for phase in job.phases for name in (phase.get("tools") or [])})
        who = {MODE_AGENT: f"{DEEPSEEK.model} planned, {job.writer.model} answered",
               MODE_DEEPSEEK: f"{DEEPSEEK.model} answered"}.get(
                   job.mode, f"{job.writer.model} answered ({thinking_label(job.thinking)})")
        settled = f"{who} after {calls_made} tool call(s)" if calls_made else who
        if used:
            settled += f" ({', '.join(used)})"
        if not script_ok and tool_request(answer):
            # The header line the client shows: this turn is not done, it is handing a list of
            # calls to the client, and the script comes back on the turn after those have run.
            settled = f"{who} asked the client's tools: running them now"
        job.finish(status="done", phase="done", note=settled)
        print(f"[job] {job.id} [{job.mode}] done in {job.report()['elapsed']:g}s, {len(answer)} "
              f"chars, {len(job.phases)} model call(s), {calls_made} tool call(s)", flush=True)
    except HTTPException as e:
        # Printed as well as sent: the page shows it once, the log keeps it.
        print(f"[job] {job.id} failed: {e.detail}", flush=True)
        note_error(str(e.detail))
        job.finish(status="error", phase="error", error=str(e.detail))
    except httpx.HTTPError as e:
        detail = upstream_error(e, job.provider).detail
        print(f"[job] {job.id} failed: {detail}", flush=True)
        note_error(detail)
        job.finish(status="error", phase="error", error=detail)
    except Exception as e:  # a bug here must never leave a reader waiting forever
        print(f"[job] {job.id} crashed: {e.__class__.__name__}: {e}", flush=True)
        note_error(f"{e.__class__.__name__}: {e}")
        job.finish(status="error", phase="error", error=f"{e.__class__.__name__}: {e}")
    finally:
        slot_give()


def start_job(messages: list, temperature: Optional[float] = None, session: str = "",
              request: Optional[Request] = None, mode: str = "", thinking: str = "",
              files=None) -> Job:
    """Pick the mode and the writer's setting, then set the work going on its own thread.

    The mode is resolved before anything else, so a caller who asked for one this service cannot
    run is told which variable is missing rather than getting an answer from another model. There
    is no "is it configured" gate in front of this any more: an unconfigured service is exactly a
    mode whose credential is missing, and that is what resolve_mode names.

    `thinking` is the writer's setting for this turn -- "fast" or "thinking" -- and it is resolved
    here rather than at the call, so the whole turn reads one setting and a caller that sends
    nothing gets the service's own default.
    """
    chosen = resolve_mode(mode)
    setting = writer_thinking(thinking)
    ip = client_ip(request)
    if not rate_ok(ip):
        raise HTTPException(429, f"too many requests from {ip}; {RATE_LIMIT} per minute")
    turns = greet(trim_messages(messages))
    # The session is what keeps one upstream chat: the caller keeps its own id (the page keeps it
    # in localStorage, the Roblox client per chat) and every turn in it continues the last answer.
    name = (session or "").strip()[:64] or session_new()
    base_url = request_base(request)
    job = Job(turns, temperature, f"{mode_label(chosen)} drafting", name, chosen, thinking,
              files, base_url)
    # What the attachment is attached to: the question as the model will read it, greeting and all,
    # so the part lands on the turn that asked it rather than on the plan agent mode puts above it.
    job.question = last_user_text(turns)
    register(job)
    print(f"[job] {job.id} [{chosen}/{setting}] session {name}: {len(turns)} turn(s), asking about "
          f"{last_user_text(turns).strip()[:60]!r}", flush=True)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


def request_base(request) -> str:
    """The address this service is reached at, for a document the provider has to fetch.

    PUBLIC_URL when it is set -- it is the only setting that cannot be wrong about a deployment
    behind a proxy -- and otherwise the request itself: the forwarded host and scheme when a proxy
    sent them (Railway, Fly and anything else that terminates TLS does), and what the caller
    reached when it did not. Without a scheme from the proxy, `request.base_url` says `http://` for
    a service that only answers https, which is a redirect a fetcher may or may not follow, and a
    document nobody read looks exactly like a model that ignored it.
    """
    if PUBLIC_URL:
        return PUBLIC_URL.rstrip("/")
    if request is None:
        return ""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if not proto and request.headers.get("x-forwarded-for"):
        # A proxy is in front and did not say which scheme it speaks for. Everything that puts
        # x-forwarded-for there terminates TLS, and a URL that comes out http:// on a service that
        # redirects to https is a document a fetcher may give up on.
        proto = "https"
    if host:
        scheme = proto or request.url.scheme or "https"
        return f"{scheme}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


# --- the conversation endpoints --------------------------------------------------------

class GenReq(BaseModel):
    """One-shot request (the Roblox client): a prompt, no history. The session still applies."""

    prompt: str
    temperature: Optional[float] = 0.7
    session: str = ""
    mode: str = ""                # agent | qwen | deepseek; empty means the service's default
    thinking: str = ""            # thinking | fast; empty means the service's own default
    files: list = []              # attachment ids from /attach, in the order they should be read


class ChatReq(BaseModel):
    """One turn of a conversation.

    `messages` is the whole conversation the caller is keeping, oldest first. The newest user turn
    gets the greeting in front of it, and the list is what the model sees, so it answers in the
    same chat it has been answering in. `session` is what makes that literally true upstream: the
    same value across turns is one chat on the provider's side, a new value is a new one.

    `mode` is which chain answers this turn -- agent (deepseek plans, then qwen writes), qwen
    (qwen alone) or deepseek (deepseek alone). Left empty it is the service's own default, and the
    mode can change between turns of one conversation.

    `thinking` is how much the writer reasons before it writes: "thinking" (the default, and the
    one that produces the better script) or "fast" (the same model spending its tokens on the
    script instead of on working it out first). It is per turn, so a quick edit and a remote
    protocol can be asked for in the same conversation, and it applies to whichever model writes.
    """

    messages: list
    temperature: Optional[float] = 0.7
    session: str = ""
    mode: str = ""
    thinking: str = ""               # thinking | fast; empty means the service's own default
    files: list = []                 # attachment ids from /attach, in the order they should be read


def job_summary(job: Job) -> dict:
    """A finished job as one object: the answer, the plan, the tool trace, and what it cost."""
    return {
        "job": job.id,
        "text": job.text(),
        "code": job.text(),
        "tool": job.tool_text,
        "plan": job.channel("plan"),
        "thoughts": job.channel("thoughts"),
        "mode": job.mode,
        "model": job.report()["model"],
        "session": job.session,
        "thinking": job.report()["thinking"],
        "phases": job.phases,
        "elapsed": job.report()["elapsed"],
    }


@app.post("/chat/stream")
def chat_stream(req: ChatReq, request: Request):
    """Start one turn and hand back its job id immediately.

    Nothing is generated on this request, so it cannot hang and be dropped: the whole turn runs on
    the job's thread and the page watches /chat/stream/{job} instead. This is the endpoint the page
    uses, so it is not key-gated -- only rate limited.
    """
    messages = clean_messages(req.messages)
    job = start_job(messages, req.temperature, req.session, request, req.mode, req.thinking,
                    req.files)
    # Only the writer is ever given the toolbox, and only when it is on -- so the list is empty in
    # deepseek mode, where there is no writer to attach it to.
    tools = (TOOL_NAMES if (tools_enabled() and AGENT_ROUNDS > 0 and job.mode != MODE_DEEPSEEK)
             else [])
    return {"job": job.id, "mode": job.mode, "model": job.report()["model"],
            "session": job.session, "thinking": job.report()["thinking"],
            "turns": len(job.messages), "tools": tools, "files": len(job.files),
            # null rather than 0: there is no ceiling on this turn unless one is configured.
            "timeout": CHAT_TIMEOUT or None}


@app.post("/chat")
def chat(req: ChatReq, _: None = Depends(require_key)):
    """The same turn, blocking -- for callers that cannot follow a stream."""
    job = start_job(clean_messages(req.messages), req.temperature, req.session, None, req.mode,
                    req.thinking, req.files)
    job.wait(job_wait())
    if job.status == "error":
        raise HTTPException(502, job.error)
    return job_summary(job)


def frame(payload: dict) -> str:
    return json.dumps(payload) + "\n"


def job_frames(job: Job):
    """NDJSON for one reader: everything the job has so far, then each new piece.

    Every reader starts at zero, so a browser whose connection died just asks again and
    rebuilds the same answer while the job carries on. The frames in between matter even when
    there is nothing to report: a stream that goes silent for minutes is what a proxy or a
    sleeping phone drops, so the wait is punctuated with heartbeats -- and a turn with tool
    rounds can spend minutes in one lookup.
    """
    index = 0
    last_phase = ""
    yield frame({"replay": True, "job": job.id, **job.report()})
    while True:
        with job.cond:
            # Wait only while there is nothing new and the job is still going: a finished job
            # is handed over at once rather than after a heartbeat.
            if not job.pieces[index:] and job.status in ("queued", "running"):
                job.cond.wait(timeout=HEARTBEAT)
            pieces = job.pieces[index:]
            index += len(pieces)
            report = job.report()
            status, error = job.status, job.error
        for channel, piece in pieces:
            if piece is None:
                yield frame({"ch": channel, "reset": True})
            else:
                yield frame({"t": piece, "ch": channel})
        if report["phase"] != last_phase:
            last_phase = report["phase"]
            yield frame({"phase": last_phase, "note": report["note"], **report})
        if status == "error":
            yield frame({"error": error, "text": job.text()})
            return
        if status == "done":
            yield frame({"done": True, "text": job.text(), "code": job.text(),
                         "tool": job.tool_text, "plan": job.channel("plan"),
                         "thoughts": job.channel("thoughts"),
                         "mode": job.mode, "session": job.session,
                         "phases": job.phases, **report})
            return
        if not pieces:
            yield frame({"beat": True, **report})


@app.get("/chat/stream/{job_id}")
@app.get("/generate/stream/{job_id}")
@app.get("/job/{job_id}")
def watch_stream(job_id: str):
    """Stream a job's progress. Calling it again after a drop is the whole point."""
    job = lookup(job_id)
    return StreamingResponse(
        job_frames(job),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/chat/result/{job_id}")
def job_result(job_id: str, _: None = Depends(require_key)):
    """Where a job stands, as one JSON object.

    A client that cannot hold a stream open -- Roblox's HttpService reads a response in one
    piece, so it cannot follow NDJSON -- starts the turn on /chat/stream and polls this. The
    same fields as the terminal frame, plus the running state, so progress is visible instead
    of one silent wait while the tools run.
    """
    job = lookup(job_id)
    body = job_summary(job) if job.status in ("done", "error") else {
        "job": job.id,
        "phase": job.phase,
        "note": job.note,
        "text": job.text(),
        "tool": job.tool_text,
        "plan": job.channel("plan"),
        "thoughts": job.channel("thoughts"),
        "mode": job.mode,
        "model": job.report()["model"],
        "session": job.session,
        "phases": job.phases,
    }
    body.update(job.report())
    if job.status == "error":
        body["error"] = job.error
    return body


def job_wait() -> float:
    """How long a blocking caller waits: no limit by default, like every call in the chain.

    `CHAT_TIMEOUT` is the per-call ceiling, and 0 (the default) means there isn't one -- so there
    is nothing to multiply out into a total either, and a blocking caller waits for the whole
    turn, tool rounds and all. Set it to a number and the total becomes a call per round plus a
    little slack on top.
    """
    if CHAT_TIMEOUT <= 0:
        return 0.0
    return CHAT_TIMEOUT * (AGENT_ROUNDS + 2) + 30


@app.post("/generate")
def generate(req: GenReq, _: None = Depends(require_key)):
    """Block until the whole turn is done -- this is the path `client.lua` uses."""
    job = start_job([{"role": "user", "content": req.prompt}], req.temperature, req.session,
                    None, req.mode, req.thinking, req.files)
    job.wait(job_wait())
    if job.status == "error":
        raise HTTPException(502, job.error)
    return job_summary(job)


@app.post("/generate/stream")
def start_stream(req: GenReq, _: None = Depends(require_key)):
    """The one-shot flow as a job, for callers that stream but keep no history."""
    job = start_job([{"role": "user", "content": req.prompt}], req.temperature, req.session,
                    None, req.mode, req.thinking, req.files)
    return {"job": job.id, "mode": job.mode, "model": job.report()["model"],
            "session": job.session, "timeout": CHAT_TIMEOUT or None}


# --- taking a file in, and handing it back out -------------------------------------------
#
# Two calls, and both exist for the same reason: what the writer should look at is a file, and a
# file is not a long question. `POST /attach` takes the bytes (base64 in JSON, because the Roblox
# client has no multipart) and hands back an id; every turn after that names the ids it wants in
# front of it. `GET /attach/{id}` gives the bytes back, and it is the one endpoint here that is not
# key-gated -- the reader is the model provider fetching the document it was pointed at, and it has
# no key to send. An id is 12 random hex characters, and a file is the caller's own.

class AttachReq(BaseModel):
    """One file: a name, whatever type it claims, and its bytes as base64."""

    name: str = "file"
    mime: str = ""
    data: str = ""            # base64, with or without a `data:<mime>;base64,` prefix
    session: str = ""         # so one chat holds no more than a turn can use


@app.post("/attach")
def attach(req: AttachReq, _: None = Depends(require_key)):
    """Take one file and hand back what to call it by.

    Key-gated, because this is the Roblox client's own upload: `API_KEY` is set on any deployment
    that is exposed, and what comes in here is then served back out at a public URL.
    """
    raw = (req.data or "").strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    if not raw:
        raise HTTPException(400, "data is empty: send the file as base64")
    try:
        blob = base64.b64decode(raw, validate=False)
    except ValueError:
        raise HTTPException(400, "data is not base64")
    kept = attach_put(req.name, blob, req.mime, req.session)
    print(f"[attach] {kept['id']} {kept['name']} {kept['mime']}, {kept['bytes']} bytes, "
          f"session {kept['by'][:6] or '-'}", flush=True)
    return kept


@app.get("/attach/{attach_id}")
def attach_raw(attach_id: str):
    """The bytes themselves, for whoever was pointed at them.

    Deliberately not key-gated: the provider downloading this has no key to send, and an
    unguessable id is what protects it -- the same bargain /chat/stream/{job} makes. A name is
    cleaned before it goes into a header, so nothing in it can rewrite the response.
    """
    found = attach_get(attach_id)
    if found is None:
        raise HTTPException(404, "unknown or expired attachment; upload it again")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", found["name"])[:80] or "file"
    return Response(found["data"], media_type=found["mime"],
                    headers={"Content-Disposition": f'inline; filename="{name}"',
                             "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})


# --- the executor on the other end -------------------------------------------------------
#
# The `run_script` tool needs Roblox to actually run something. client.lua is that side: it polls
# /agent/pull, runs whatever comes back, and posts the console output and the traceback to
# /agent/push. Both are key-gated, because a client that can be handed arbitrary Luau is a client
# that has to be one you trust.

class AgentPush(BaseModel):
    """One executor result: which run, whether it threw, what it printed, and the traceback."""

    run: str
    ok: bool = False
    output: str = ""
    error: str = ""


@app.get("/agent/pull")
def agent_pull(client: str = "", _: None = Depends(require_key)):
    """The next script for a listening executor, or nothing.

    Deliberately a 200 with an empty `run` rather than a 204: Roblox's HttpService reads a
    response in one piece, and an empty body is one more thing that can read as a failure.
    """
    task = take_script(client)
    if not task:
        return {"run": "", "script": "", "executor": executor_state()["clients"]}
    return task


@app.post("/agent/push")
def agent_push(req: AgentPush, _: None = Depends(require_key)):
    """The executor's answer for one run, which is what the waiting tool call reads."""
    if not deliver(req.run, req.ok, req.output, req.error):
        raise HTTPException(404, "unknown run, or it was already answered")
    return {"ok": True}


# --- the OpenAI-compatible surface -----------------------------------------------------

def relay_stream(body: dict):
    """Pass qwen-api's SSE through untouched.

    Nothing is rewritten on this path, which is what keeps the rest of qwen-api working
    through the bridge: reasoning_content, tool calls, web-search annotations and the hidden
    continuation metadata all survive, so an OpenAI client that manages its own history keeps
    working exactly as it would against Qwen itself -- including keeping one chat, which is what
    that metadata is for. This surface does not run the agent loop; a caller that wants the tools
    run for it uses /chat.
    """
    with httpx.Client(timeout=client_timeout(CHAT_TIMEOUT),
                      follow_redirects=True) as c:
        with c.stream("POST", QWEN.endpoint(), json=body, headers=QWEN.headers()) as r:
            if r.status_code >= 400:
                yield sse_error(failure_reason(r.status_code,
                                               r.read().decode("utf-8", "replace"), QWEN))
                return
            if "event-stream" not in r.headers.get("content-type", ""):
                raw = r.read().decode("utf-8", "replace")
                text = message_text(raw)
                if not text:
                    yield sse_error(failure_reason(200, raw, QWEN))
                    return
                yield sse({"id": "chatcmpl-bridge", "object": "chat.completion.chunk",
                           "model": QWEN_MODEL,
                           "choices": [{"index": 0,
                                        "delta": {"role": "assistant", "content": text},
                                        "finish_reason": None}]})
                yield "data: [DONE]\n\n"
                return
            for chunk in r.iter_bytes():
                yield chunk


@app.post("/v1/chat/completions")
def chat_completions(payload: dict = Body(...), _: None = Depends(require_key)):
    """The bridge itself: OpenAI-compatible, token injected, streaming preserved.

    A caller sends exactly what it would send to OpenAI. The newest user turn gets the greeting
    in front of it and the model is pinned to QWEN_MODEL. Everything else -- tools,
    web_search_options, reasoning_effort, temperature, stream -- passes through. Server-side tool
    execution is not part of this surface: pass a tool here and the call comes back to you, which
    is what an OpenAI client expects.
    """
    if not CONFIGURED:
        raise HTTPException(503, NOT_CONFIGURED)
    body = dict(payload)
    messages = trim_messages(clean_messages(payload.get("messages")))
    body["messages"] = greet(messages)
    asked = str(body.get("model") or "").strip()
    if asked and asked != QWEN_MODEL:
        print(f"[bridge] {asked} requested, using {QWEN_MODEL} (pinned)", flush=True)
    body["model"] = QWEN_MODEL
    body["thinking_mode"] = QWEN_THINKING
    if MAX_TOKENS > 0 and "max_tokens" not in body and "max_completion_tokens" not in body:
        body["max_tokens"] = MAX_TOKENS
    if body.get("stream"):
        return StreamingResponse(relay_stream(body), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    try:
        with httpx.Client(timeout=client_timeout(CHAT_TIMEOUT),
                          follow_redirects=True) as c:
            r = c.post(QWEN.endpoint(), json=body, headers=QWEN.headers())
    except httpx.HTTPError as e:
        raise upstream_error(e, QWEN)
    if r.status_code >= 400:
        raise HTTPException(502, failure_reason(r.status_code, r.text, QWEN))
    return Response(r.content, media_type="application/json")


@app.get("/v1/models")
def models(_: None = Depends(require_key)):
    """The models this bridge can answer with, then what else the proxy serves, for reference."""
    ids = [QWEN_MODEL]
    if DEEPSEEK.configured and DEEPSEEK.model not in ids:
        ids.append(DEEPSEEK.model)
    for mid in list_models():
        if mid not in ids:
            ids.append(mid)
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": mid, "object": "model", "created": now, "owned_by": "qwen"} for mid in ids
    ]}


# --- the page ---------------------------------------------------------------------------


class KanhaReq(BaseModel):
    """A plain conversational turn for the lightweight Kanha side."""

    messages: list
    mode: str = "qwen"
    provider: str = ""  # accepted for compatibility with the earlier picker


@app.get("/kanha/providers")
def kanha_providers():
    """Tell the small chat surface which configured modes can answer."""
    return {"providers": [
        {"id": "qwen", "name": QWEN.model, "configured": QWEN.configured},
        {"id": "deepseek", "name": DEEPSEEK.model, "configured": DEEPSEEK.configured},
        {"id": "agent", "name": "Agent", "configured": QWEN.configured and DEEPSEEK.configured},
    ]}


@app.get("/kanha", response_class=HTMLResponse)
async def kanha_page():
    """Serve the uncluttered, non-Roblox chat surface."""
    try:
        return HTMLResponse(INDEX.parent.joinpath("kanha.html").read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("<h1>Hy kanha</h1><p>kanha.html is missing.</p>", status_code=500)


@app.post("/kanha/chat")
def kanha_chat(req: KanhaReq):
    """Answer ordinary conversation through the provider selected on the Kanha side."""
    mode = (req.mode or req.provider or "qwen").strip().lower()
    if mode not in ("qwen", "deepseek", "agent"):
        raise HTTPException(400, "unknown Kanha mode")

    messages = [{"role": "system", "content":
                 "You are Kanha. Have a natural, helpful conversation. "
                 "Do not turn ordinary questions into Roblox or programming tasks."}]
    messages.extend(clean_messages(req.messages)[-40:])

    if mode == "agent":
        if not (QWEN.configured and DEEPSEEK.configured):
            raise HTTPException(503, "Agent mode needs both Qwen and DeepSeek configured")
        job = start_job(messages[1:], 0.7, "kanha", None, MODE_AGENT, "", [])
        job.wait(job_wait())
        if job.status == "error":
            raise HTTPException(502, job.error)
        answer = job.text()
    else:
        provider = {"qwen": QWEN, "deepseek": DEEPSEEK}[mode]
        if not provider.configured:
            raise HTTPException(503, f"{mode} is not configured on this service")

        if provider.web is not None:
            box = {}
            try:
                answer = "".join(stream_any(provider, messages, 0.7,
                                            MAX_TOKENS or 2048, box,
                                            web_session=deepseek_chat("kanha")))
            except HTTPException:
                raise
            except httpx.HTTPError as error:
                raise upstream_error(error, provider)
        else:
            body = provider.request(messages, 0.7, MAX_TOKENS or 2048, stream=False)
            try:
                with httpx.Client(timeout=client_timeout(provider.timeout),
                                  follow_redirects=True) as client:
                    response = client.post(provider.endpoint(), json=body,
                                           headers=provider.headers())
            except httpx.HTTPError as error:
                raise upstream_error(error, provider)
            if response.status_code >= 400:
                raise HTTPException(502, failure_reason(response.status_code,
                                                        response.text, provider))
            answer = message_text(response.text)

    answer = strip_metadata(answer).strip()
    if not answer:
        raise HTTPException(502, f"{mode} returned an empty answer")
    return {"message": answer, "mode": mode}


def chip(ok: bool, name: str, detail: str) -> str:
    """One status chip on the page. The browser refreshes these from /health."""
    return (
        f'<div class="chip {"ok" if ok else "bad"}" data-chip="{name}">'
        f'<span class="led"></span><span class="name">{html.escape(name)}</span>'
        f'<span class="detail">{html.escape(detail)}</span></div>'
    )


@app.get("/", response_class=HTMLResponse)
async def root():
    state = await snapshot()
    tools = state["tools"]
    chips = "".join([
        chip(True, "api", "online"),
        chip(state["bridge"], "bridge",
             state["last_error"] or (state["provider_label"] if state["bridge"]
                                     else "QWEN_TOKEN is not set -- qwen and agent are off")),
        chip(state["token_ok"], "token", state["token_detail"]),
        # The second model: without it two of the three modes cannot run, and the picker says so.
        chip(state["deepseek"]["ok"], "deepseek",
             state["deepseek"]["detail"] if DEEPSEEK.configured
             else "DEEPSEEK_TOKEN is not set -- agent and deepseek are off"),
        chip(tools["on"], "tools",
             f"{len(tools['names'])} on -- {', '.join(tools['names'][:3])}..."
             if tools["on"] else "off -- the model answers from what it knows"),
        # The API dump is what makes "does that member exist" answerable, and it is fetched on
        # first use, so its state is worth a chip of its own.
        chip(tools["dump_ok"], "roblox",
             f"{tools['dump_classes']} classes" if tools["dump_ok"]
             else (tools["dump_error"] or "fetched on the first lookup")),
        # And the executor, which is the only thing that can prove a script runs.
        chip(tools["executor_ok"], "executor",
             (" · ".join(tools["executor_clients"])[:60] if tools["executor_ok"]
              else "not listening -- run_script says so instead of waiting")),
        chip(True, "model", state["chain"]),
        chip(True, "mode", f"{state['thinking']} · {state['greeting']}" if state["greeting"]
             else state["thinking"]),
    ])
    try:
        page = INDEX.read_text(encoding="utf-8")
    except OSError:
        # The API stays usable if the static page was not copied into the image.
        return HTMLResponse("<h1>bahs</h1><p>web/index.html is missing; use /health and /docs.</p>")
    return HTMLResponse(
        page.replace("__CHIPS__", chips)
            .replace("__MODEL__", html.escape(state["model"]))
            .replace("__GREETING__", html.escape(GREETING))
            .replace("__TOOLS__", html.escape(", ".join(tools["names"]) if tools["on"] else "off"))
            .replace("__TOOL_COUNT__", str(len(tools["names"]) if tools["on"] else 0))
            .replace("__ROUNDS__", str(AGENT_ROUNDS))
            # The picker: the three modes as JSON (escaped, so it can sit in an attribute) and the
            # one this service runs when the caller picks nothing.
            .replace("__MODES__", html.escape(json.dumps(state["modes"])))
            .replace("__MODE__", html.escape(state["mode"]))
            # The writer's setting, which the page offers per turn beside the mode.
            .replace("__THINKING__", html.escape(state["thinking_default"]))
    )


async def snapshot() -> dict:
    # Always reports rather than raising, so the platform healthcheck only depends on the API
    # being up. The token check and the dump's own state come back in the body; what is slow is a
    # network call, so it runs off the event loop -- /health is polled every few seconds and must
    # never hold up a turn. Nothing here fetches the API dump: the tool does that, on first use.
    state = await asyncio.to_thread(token_state)
    deep = await asyncio.to_thread(deepseek_state)
    tools = await asyncio.to_thread(tool_state)
    mode = default_mode()
    body = {
        "status": "ok",
        "bridge": CONFIGURED,
        "endpoint": QWEN_URL,
        "provider_label": QWEN.label(),
        "model": QWEN_MODEL,
        # The chain the default mode runs, which is what the page shows in place of one model.
        "chain": mode_label(mode),
        "mode": mode,
        "modes": modes_state(),
        "thinking": QWEN_THINKING,
        # The writer's setting is the caller's to pick per turn, so what is reported here is what a
        # turn that says nothing gets, and the two settings it may ask for instead.
        "thinking_default": writer_thinking(""),
        "thinking_choices": [{"id": value, "label": thinking_label(value),
                              "mode": QWEN_FAST_THINKING if value == "fast" else QWEN_THINKING}
                             for value in ("thinking", "fast")],
        "greeting": GREETING,
        "token_ok": state["ok"],
        "token_detail": state["detail"],
        "deepseek": {**deep, "model": DEEPSEEK.model, "endpoint": DEEPSEEK.url,
                     "configured": DEEPSEEK.configured, "shape": DEEPSEEK_SHAPE,
                     "thinking": DEEPSEEK_THINKING,
                     "transport": "web" if DEEPSEEK.web is not None else DEEPSEEK_SHAPE,
                     "chats": deepseek_chats()},
        "tools": tools,
        "sessions": session_count(),
        "attachments": attach_count(),
        # What a caller may attach, said out loud so a client can refuse a file before sending it
        # rather than reading the refusal back off a failed upload.
        "attachment_limits": {"files": ATTACH_MAX, "max_mb": ATTACH_MAX_BYTES // 1048576,
                              "ttl": ATTACH_TTL, "public_url": bool(PUBLIC_URL)},
        "rounds": AGENT_ROUNDS,
        "limits": {"per_minute": RATE_LIMIT, "concurrent": MAX_CONCURRENT,
                   "running": running_now()},
        "last_error": last_error(),
        "api_key_required": bool(CALLER_KEY),
    }
    if not any(m["on"] for m in body["modes"]):
        # Nothing can answer: no Qwen token and no DeepSeek credential. The platform healthcheck
        # only depends on the API being up, so this is reported in the body like any other state.
        body["status"] = "degraded"
        body["error"] = NOT_CONFIGURED
    return body


@app.get("/health")
async def health():
    return await snapshot()
