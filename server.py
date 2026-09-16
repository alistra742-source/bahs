"""The service: the chain, the jobs, the endpoints and the page.

Everything that talks to a provider -- the tokens, the config, the two transports, the briefs,
and the job record a reader attaches to -- lives in `bridge`; the second reader (GLM, and the
brief it reads) lives in `peers`. This module is what turns that into a service: the app, the
chain that runs one turn, and the endpoints the page and the Roblox client use.

The chain is one writer and two readers: Qwen drafts, DeepSeek reads send.txt and writes the
version it would ship, Qwen merges the two in the chat it drafted in, then GLM reads send2.txt
and does the same over the script the first two settled on -- which Qwen merges once more. What
ships is the script both readers agreed on.
"""
from bridge import *  # noqa: F401,F403 -- the providers, the config and the job record
from peers import (MAX_SECOND_ROUNDS, SECOND_BRIEF, SECOND_BRIEF_NAME, SECOND_ROUNDS,
                   SECOND_SEED, SECOND_SEED_TOKENS, SECOND_TEMPERATURE, SECOND_TOKENS, ZAI,
                   second_enabled, second_state)  # noqa: F401 -- the second reader
from state import (client_ip, last_error, list_models, note_error, note_review_error,
                   rate_ok, reviewer_state, running_now, slot_give, slot_take,
                   token_state)  # noqa: F401 -- the checks behind the page's chips
import peers  # the second reader's own failure note, read through the module


# --- jobs ------------------------------------------------------------------------------

class Job:
    """One turn, owned by a background thread rather than by the caller.

    A phone that gave up on an answer used to take the whole generation with it: the request
    was the only thing driving the model, so nothing was left to read. A job runs to
    completion on its own thread, keeps every piece it has produced, and any number of
    readers can attach to it -- including one that comes back after the connection dropped,
    which replays the output from the start and follows along.

    Text arrives on seven channels, because there are seven things to show: each reader reading
    its own brief (send.txt for the first, send2.txt for the second), the draft, each reader's own
    version of the script, what the first reader said about the merged one, and the answer that
    comes out of it -- which is also where the script chosen for the writer's "which one do you
    prefer?" streams in, because that is the answer.
    """

    def __init__(self, messages: list, temperature: Optional[float], note: str, review: bool):
        self.id = uuid.uuid4().hex[:12]
        self.messages = messages
        self.temperature = temperature
        self.note = note
        self.want_review = review
        self.pieces: list = []          # (channel, piece)
        self.buffers: dict = {"seed": [], "seed2": [], "draft": [], "peer": [], "peer2": [],
                              "review": [], "answer": []}
        self.error = ""
        self.status = "queued"          # queued -> running -> done | error
        self.phase = ("queued"          # queued | seed | seed2 | draft | peer | merge | agree
                      " | peer2 | agree2 | choose | done")
        self.phases: list = []          # one record per model call
        self.review_text = ""
        # The second reader's last answer (its verdict), kept beside the first reader's.
        self.second_text = ""
        self.started = time.time()
        self.finished = 0.0
        self.cond = threading.Condition()

    def channel(self, name: str) -> str:
        return "".join(self.buffers.get(name, []))

    def text(self) -> str:
        """What the caller asked for: the negotiated answer, or the draft without one."""
        return self.channel("answer") or self.channel("draft")

    def add(self, channel: str, piece: str) -> None:
        with self.cond:
            self.pieces.append((channel, piece))
            self.buffers.setdefault(channel, []).append(piece)
            self.cond.notify_all()

    def reset_channel(self, channel: str) -> None:
        """Throw away what a channel has produced so far.

        The callers are the regression guard and the negotiation: a merge or a version that came
        back cut off has already been streamed to whoever is reading, and it has to be replaced
        by the script that stands rather than shown above it. The reset is itself a piece, so a
        reader that attaches later replays the same sequence and ends up with the same text.
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

        A `timeout` of 0 or less means wait however long the chain takes, which is the default:
        the negotiation has rounds and each round has two model calls, so there is no honest
        number of seconds to cut it off at. A caller that does want a ceiling passes one.
        """
        deadline = time.time() + timeout if timeout and timeout > 0 else 0.0
        with self.cond:
            while self.status not in ("done", "error"):
                if not deadline:
                    self.cond.wait()
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise HTTPException(504, f"timed out after {timeout:g}s waiting on the chain")
                self.cond.wait(timeout=remaining)

    def report(self) -> dict:
        """Where the job stands; what the page shows next to its running timer."""
        return {
            "status": self.status,
            "phase": self.phase,
            "note": self.note,
            "model": QWEN_MODEL,
            "reviewer": REVIEWER.model if self.want_review else "",
            "thinking": QWEN_THINKING,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "chars": len(self.text()),
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
    # Nothing to load or warm: there are no local weights, only API calls.
    if CONFIGURED:
        print(f"[bridge] {QWEN_URL} -> {QWEN_MODEL} (thinking: {QWEN_THINKING})", flush=True)
    else:
        print(f"[bridge] {NOT_CONFIGURED}", flush=True)
    if review_enabled():
        where = REVIEWER.web.label if REVIEWER.web is not None else REVIEWER.url
        print(f"[review] {where} -> {REVIEWER.model} (thinking: {REVIEW_THINKING}, "
              f"shape: {REVIEW_SHAPE}), brief {len(BRIEF)} chars from "
              f"{BRIEF_PATH.name if BRIEF else 'the built-in rubric'}", flush=True)
        if REVIEW_URL_AUTO:
            print("[review] DEEPSEEK_TOKEN is not an sk-... API key, so the review goes to the "
                  "site instead of the API. Set REVIEW_URL to override that", flush=True)
        if REVIEWER.web is not None:
            print("[review] the userToken is the credential; search and thinking are sent false",
                  flush=True)
            # Not loaded here: the first fetch is a network call, and boot should not wait on it.
            print(f"[deepseek] proof of work: "
                  + (f"{pow_solver.MODULE_PATH.name} is in the image"
                     if pow_solver.MODULE_PATH.exists()
                     else "no sha3 module in the image, so one is fetched on the first review"),
                  flush=True)
    else:
        print("[review] no reviewer configured; answers are sent as the model writes them",
              flush=True)
    if not ZAI.configured:
        print("[second] no ZAI_TOKEN set; the chain runs with one reader", flush=True)
    elif peers.is_session_token(peers.ZAI_TOKEN):
        # Named exactly, because the tempting fix (paste the site's token) is what produced it.
        print(f"[second] ZAI_TOKEN is a chat.z.ai session token, which cannot generate from a "
              f"server: {peers.SESSION_TOKEN_NOTE}", flush=True)
    elif second_enabled():
        where2 = ZAI.web.label if ZAI.web is not None else ZAI.url
        print(f"[second] {where2} -> {ZAI.model} (thinking {ZAI_THINKING}, no search), "
              f"brief {len(SECOND_BRIEF)} chars from {SECOND_BRIEF_NAME}, "
              f"up to {SECOND_ROUNDS} round(s)", flush=True)
    else:
        print("[second] ZAI_TOKEN is set but the stage is off (rounds 0, or PIPELINE=off)",
              flush=True)
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
        "draft": job.channel("draft"),
        "peer": job.channel("peer"),
        "peer2": job.channel("peer2"),
        "review": job.review_text,
        "second_review": job.second_text,
        "model": QWEN_MODEL,
        "reviewer": REVIEWER.model if job.want_review and review_enabled() else "",
        "second": ZAI.model if job.want_review and second_enabled() else "",
        "phases": job.phases,
        "done": job.status == "done",
    }
    body.update(job.report())
    if job.status == "error":
        body["error"] = job.error
    return body


def require_key(x_api_key: Optional[str] = Header(None),
                authorization: Optional[str] = Header(None)) -> None:
    """Gate the *API* surfaces (/v1, /generate, /chat) when a key is defined.

    The key is API_KEY when it is set, and otherwise the Qwen token itself -- one secret to
    keep, as asked. The page is deliberately not gated (it is used without a login), so what
    protects the service is the rate limit and the concurrency ceiling, not this.

    This is the only credential a caller ever handles: the Qwen token and the reviewer key
    stay here, so they never reach a browser, a Roblox client or a log.
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
# The provider side of this -- one request, its stream, and what a failure means -- is in
# bridge.py, next to the providers themselves. What is left here is the chain: who asks whom,
# and what the answer is worth.

def run_phase(job: Job, messages: list, temperature: Optional[float], provider: Provider,
              max_tokens: int, channel: str, phase: str, note: str,
              web_session: Optional[object] = None) -> tuple:
    """Stream one model call into a channel and record what it cost.

    Every call in the chain goes through here, so every call ends up in the job's phase
    record: which model, how long, how many characters, and whether it stopped early. Without
    that, "is this more reliable?" is not a question anyone can answer.
    """
    job.finish(phase=phase, note=note)
    box: dict = {"finish": None, "usage": None}
    started = time.time()
    pieces: list = []
    for piece in stream_answer(messages, temperature, provider, max_tokens, box, web_session):
        pieces.append(piece)
        job.add(channel, piece)
    text = "".join(pieces)
    record = {
        "phase": phase,
        "provider": provider.name,
        "model": provider.model,
        "ms": int((time.time() - started) * 1000),
        "chars": len(text),
        "finish": box["finish"],
        "usage": box["usage"],
    }
    job.phases.append(record)
    print(f"[job] {job.id} {phase}: {provider.model} {record['ms']}ms, {record['chars']} chars, "
          f"finish={record['finish']}", flush=True)
    return text, record


def request_body(job: Job, code: str, notes: list, heading: str) -> str:
    """One asking-a-model-about-this-script body: the request, the target, the script, checks.

    Both requests share it so the two can never drift apart in what they show: what the user
    asked for, what it has to run in, the script in question (with secrets masked), and what
    this service already checked about it.
    """
    checked = "\n".join(f"- {n}" for n in notes) if notes else "- nothing flagged"
    script, masked = redact(code)
    if len(script) > REVIEW_SCRIPT_MAX:
        script = script[:REVIEW_SCRIPT_MAX] + "\n-- [the rest was cut for length] --"
    asked = "\n".join(f"- {without_greeting(t)[:2000]}" for t in asked_for(job.messages)) or "(none)"
    if masked:
        print(f"[job] {job.id} {heading}: {masked} secret(s) masked before sending", flush=True)
    return f"""REQUEST (what the user asked for):
{asked}

TARGET: {TARGET_RUNTIME}

{heading.upper()}:
{script}

CHECKS THIS SERVICE ALREADY RAN:
{checked}"""


def peer_request(job: Job, code: str, from_model: str, notes: list) -> str:
    """What the reviewer is asked first: write the version of this script you would ship.

    Not a request for complaints. The answer is a script, because the step after it puts the
    reviewer's script beside the writer's and keeps the best of both.
    """
    body = request_body(job, code, notes, f"the script {from_model} wrote")
    return f"""{body}

Write the version of this script you would ship for the request above, in the format you were \
given: VERDICT: BETTER and then the complete script, or VERDICT: KEEP if nothing in it can be \
made more reliable."""


def verify_request(job: Job, code: str, from_model: str, notes: list) -> str:
    """The later rounds: would you ship the merged script, or does it need another version?

    This is the question that ends those rounds. An answer of AGREE is that reader and the
    writer agreeing on one script, and the writer is left with it.
    """
    body = request_body(job, code, notes, "the merged script")
    return f"""The script below is what came out of the last merge: {from_model} took your last \
version, kept whatever it judged more reliable, and put the rest back.

{body}

Would you ship this exactly as it is?
- If you would: answer VERDICT: AGREE, and nothing else.
- If you would not: answer VERDICT: BETTER and then the complete script, changing only what \
would actually break and saying nothing about the rest."""


def looks_like_code(text: str) -> bool:
    """Whether what came back is a script rather than a sentence about one.

    The verdict can be read off a line; a script cannot. This is what decides whether a round
    has something to merge, so it is deliberately about structure -- most lines have to look
    like Lua -- rather than about the words in them.
    """
    body = (text or "").strip()
    if len(body) < 40:
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    markers = ("local ", "function", "end", "then", "do ", "else", "return", "print(",
               "Instance.", "game:", "script.", "require(", "task.", "wait(", "pcall", "--")
    def code_line(line: str) -> bool:
        if line.startswith("--"):
            return True
        if any(marker in line for marker in markers):
            return True
        return ("=" in line or "(" in line) and len(line) > 3
    hits = sum(1 for line in lines if code_line(line))
    return hits >= max(2, (len(lines) * 2) // 3)


def parse_verdict(text: str) -> tuple:
    """The reviewer's verdict and the script it wrote, when it wrote one.

    BETTER carries a whole script; AGREE and KEEP mean the script in front of it stands. A
    reviewer that ignored the format is read as kindly as it can be: a script is taken from
    whatever it wrote, and an answer with no script in it is a round that changed nothing
    rather than a round that failed.
    """
    body = (text or "").strip()
    verdict = ""
    match = re.search(r"(?im)^\s*VERDICT\s*[:=]\s*([A-Za-z]+)", body)
    if match:
        verdict = match.group(1).strip().upper()
        body = body[match.end():].strip()
    if verdict in ("AGREE", "OK", "KEEP", "SAME", "APPROVED"):
        return "AGREE", ""
    code = strip_fences(body)
    if not looks_like_code(code):
        return ("KEEP", "") if verdict else ("", "")
    return "BETTER", code


def merge_instruction(proposed: str, from_model: str, notes: list) -> str:
    """What goes back into the chat that wrote the draft: the other model's script, whole.

    The draft is already an assistant turn in that conversation, so this is one model editing
    its own work against a competing version of it -- which is the whole reason the merge
    happens in the same chat rather than starting the task again.
    """
    block = proposed
    if len(block) > MERGE_PASTE_MAX:
        block = block[:MERGE_PASTE_MAX] + "\n-- [the rest was cut for length] --"
    extra = ""
    if notes:
        extra = ("\nThis service's own checks flagged:\n"
                 + "\n".join(f"- {n}" for n in notes) + "\n")
    return f"""{from_model} read the script you just wrote and wrote its own version of it. Here \
it is, whole:

{block}
{extra}
Produce the single best version of the script: keep everything in yours that already works, \
take from the other version whatever is genuinely more reliable, and where the two disagree \
choose the one that cannot fail at runtime. If something in the other version is wrong, ignore \
it and keep yours. Change nothing else, and rename nothing the request did not name.

Return the complete script and nothing else: no explanation, no notes, no commentary, no \
markdown code fences."""


class ReviewerChat:
    """One reader's conversation: its brief first, and every question after it in one chat.

    Two things matter here and nowhere else. The brief is sent on its own and its answer is
    waited for before any request goes out, so the reader has read its instructions before it is
    asked to do anything. And every later question lands in the conversation that brief opened --
    on the API path by carrying the turns, on the site path by holding one chat session open --
    so the reader answers about the script it was shown instead of starting over.

    One of these drives each reader in the chain: DeepSeek with send.txt, then GLM with send2.txt.
    The defaults are the first reader's, so a reader configured the same way needs no arguments.
    """

    def __init__(self, job: Job, provider: Optional[Provider] = None,
                 brief: Optional[str] = None, brief_name: str = "",
                 seed_on: Optional[bool] = None, seed_tokens: int = 0,
                 warning: Optional[str] = None, temperature: Optional[float] = None,
                 seed_channel: str = "seed"):
        self.job = job
        self.provider = provider or REVIEWER
        self.brief = BRIEF if brief is None else brief
        self.brief_name = brief_name or (BRIEF_PATH.name if BRIEF else "the built-in rubric")
        self.seed_on = SEED_BRIEF if seed_on is None else seed_on
        self.seed_tokens = seed_tokens or SEED_TOKENS
        self.warning = REVIEW_WARNING if warning is None else warning
        self.temperature = REVIEW_TEMPERATURE if temperature is None else temperature
        self.seed_channel = seed_channel
        self.turns: list = []
        self.last_answer = ""
        self.contract_sent = False
        # A live chat on chat.deepseek.com, when that is the transport: the site threads a
        # conversation by message id, so the session is what makes the second message a
        # continuation rather than a new branch of the same chat.
        self.web = self.provider.web.new_session() if self.provider.web is not None else None

    def _turns_for(self, text: str, contract: bool = True) -> list:
        """One message to the reviewer: the warning, then the brief if it is the first one.

        The brief leads the very first message and is never repeated, so what the reviewer reads
        first is Send.txt. The answer contract rides with the first thing actually *asked* of it
        instead -- which is the request after the acknowledgement when the brief is seeded, and
        the request itself when it is not -- so the brief goes out alone.
        """
        parts = []
        if self.warning:
            # On every message, not just the first: the target runtime is the one thing a reader
            # must not lose track of, and a long conversation is where it gets lost.
            parts.append(self.warning)
        if not self.turns and self.brief:
            parts.append(self.brief)
        if contract and not self.contract_sent:
            self.contract_sent = True
            parts.append(RUBRIC)
        parts.append(text)
        self.turns.append({"role": "user",
                           "content": "\n\n".join(part for part in parts if part.strip())})
        # The API path sends the whole conversation; the site path sends only the new message,
        # because the session itself is holding the earlier ones.
        return [self.turns[-1]] if self.web is not None else list(self.turns)

    def say(self, text: str, channel: str, phase: str, note: str, max_tokens: int,
            contract: bool = True) -> str:
        answer, _ = run_phase(self.job, self._turns_for(text, contract), self.temperature,
                              self.provider, max_tokens, channel, phase, note, self.web)
        self.turns.append({"role": "assistant", "content": answer})
        self.last_answer = answer
        return answer

    def seed(self) -> bool:
        """Send the brief on its own, and wait for the answer to it, before anything is asked.

        This message is Send.txt (and the warning) plus one line asking for an acknowledgement --
        nothing else, and nothing about the user's request. The reply is an acknowledgement
        rather than work, so what comes back here is thrown away on purpose: it is the *reading*
        of Send.txt that is wanted, and the request that follows rides on it.
        """
        if not self.seed_on:
            return False
        which = self.brief_name
        answer = self.say(SEED_NOTE, self.seed_channel, self.seed_channel,
                          f"{self.provider.model} reading {which}", self.seed_tokens,
                          contract=False)
        sent = self.turns[0]["content"] if self.turns else ""
        whole = ("; the whole brief is in it" if self.brief and self.brief in sent
                 else "; WARNING: the brief did not fit the message" if self.brief else "")
        print(f"[job] {self.job.id} {self.seed_channel}: {which} went out first and on its own "
              f"({len(sent)} chars sent, {len(answer)} back{whole}); the request goes next",
              flush=True)
        return True


def merge_versions(job: Job, current: str, proposed: str, notes: list, reader: str = "") -> str:
    """The writer's turn: one script out of its own version and the reviewer's.

    It happens in the chat that wrote the draft, so the model is editing its own work with the
    other version in front of it. A merge that comes back unusable is discarded -- the same
    guard the draft went through -- and the version being edited is what stands instead.
    """
    who = reader or REVIEWER.model
    turns = list(job.messages) + [
        {"role": "assistant", "content": current},
        {"role": "user", "content": merge_instruction(proposed, who, notes)},
    ]
    merged, record = run_phase(job, turns, job.temperature, QWEN, REFINE_TOKENS, "answer",
                               "merge", f"{QWEN.model} merging {who}'s version")
    merged = strip_fences(merged)
    if job.channel("answer").strip() != merged:
        job.reset_channel("answer")
        job.add("answer", merged)
    _, usable = structural_notes(merged, record["finish"])
    if not usable:
        print(f"[job] {job.id} merge: the merged script was not usable; keeping the last one",
              flush=True)
        return ""
    return merged


# --- when the writer asks which script you want --------------------------------------------
#
# A turn has to end with a script, and one way it does not is the model offering two and asking
# which one is preferred. That is a question, not an answer, so the bridge answers it: the option
# with the most lines wins, said back to the model in the chat that asked.

# The question, in the shapes it gets asked. Only ever looked for near the end of an answer: a
# "which" in the middle of a script's own comment is not the model asking the reader anything.
CHOICE_QUESTION = re.compile(
    r"(?is)\bwhich\b[^.?!\n]{0,120}?\b(?:prefer|preferred|choose|pick|like|want|"
    r"should i (?:use|pick|choose|go with|proceed with|ship|keep))\b")

# "Option 1:", "Choice B -", "**Version 2**": how the scripts get labelled when they are not
# wrapped in fences.
OPTION_HEAD = re.compile(
    r"(?im)^\s*(?:[-*>]\s*)?(?:#+\s*)?(?:\*\*)?(?:option|choice|version|script|alternative)\s*"
    r"([0-9]{1,2}|[A-Fa-f])\b\s*(?:\*\*)?\s*[:.\-\u2013)]*")

ORDINALS = ("first", "second", "third", "fourth", "fifth")


def code_line_count(code: str) -> int:
    """Lines with something on them -- what "the code with the most lines" is measured in."""
    return sum(1 for line in (code or "").splitlines() if line.strip())


def code_options(text: str) -> list:
    """The scripts an answer is offering, when it ends by asking which one is preferred.

    Empty unless the answer really is a choice: the question has to be there and at least two
    of the blocks have to look like code. Fenced blocks are read first, since that is how two
    scripts are usually offered; an answer that labels them instead is read from the labels.
    """
    body = text or ""
    if not CHOICE_QUESTION.search(body[-1500:]):
        return []
    blocks = [match.group(1)
              for match in re.finditer(r"```[A-Za-z0-9_+-]*\s*\n(.*?)```", body, re.S)]
    if len(blocks) < 2:
        heads = list(OPTION_HEAD.finditer(body))
        blocks = ([body[heads[i].end():heads[i + 1].start() if i + 1 < len(heads) else len(body)]
                   for i in range(len(heads))] if len(heads) >= 2 else [])
    options = []
    for index, block in enumerate(blocks):
        code = strip_fences(block)
        ordinal = ORDINALS[index] if index < len(ORDINALS) else str(index + 1)
        options.append({"label": f"option {index + 1} (the {ordinal} one)",
                        "code": code, "lines": code_line_count(code)})
    if sum(1 for option in options if looks_like_code(option["code"])) < 2:
        return []
    return options


def longest_option(options: list) -> dict:
    """The option with the most lines. A tie goes to the one offered first."""
    return max(options, key=lambda option: option["lines"])


def choice_instruction(pick: dict, options: list, notes: list) -> str:
    """What is said back to the writer: which one, and why that one."""
    prefix = f"{GREETING} " if GREETING else ""
    others = ", ".join(f"{option['label']} has {option['lines']}"
                       for option in options if option is not pick)
    extra = ("\n\nThis service's own checks flagged:\n"
             + "\n".join(f"- {n}" for n in notes)) if notes else ""
    return f"""{prefix}I prefer {pick['label']} — it has the most code ({pick['lines']} lines; \
{others}).{extra}

Ship exactly that one: the complete script, nothing before it, no markdown code fences, no \
notes, no alternatives, and no questions. Do not ask me which one I prefer again."""


def settle_choice(job: Job, script: str, notes: list) -> tuple:
    """Answer the writer's "which choice do you prefer?" instead of shipping the question.

    Returns the script to ship and whether a choice had to be settled. The option with the most
    lines is what the answer asks for, and it is also the fallback: if the follow-up call cannot
    be made, or does not come back with a script, that option is what ships as it stands.
    """
    settled = False
    for _ in range(max(0, CHOICE_ROUNDS)):
        options = code_options(script)
        if not options:
            break
        pick = longest_option(options)
        settled = True
        print(f"[job] {job.id} choose: {QWEN.model} offered {len(options)} script(s) "
              f"({', '.join(str(o['lines']) for o in options)} lines) and asked which; "
              f"answering with {pick['label']}", flush=True)
        job.finish(phase="choose", note=f"{QWEN.model} asked which one; taking {pick['label']}")
        # The question has already been streamed into the answer, so it is thrown away before the
        # chosen script replaces it -- the same rule a merged script that came back unusable gets.
        job.reset_channel("answer")
        turns = list(job.messages) + [
            {"role": "assistant", "content": script},
            {"role": "user", "content": choice_instruction(pick, options, notes)},
        ]
        try:
            picked, record = run_phase(job, turns, job.temperature, QWEN, DRAFT_TOKENS,
                                       "answer", "choose",
                                       f"{QWEN.model} shipping {pick['label']}")
        except HTTPException as e:
            print(f"[job] {job.id} choose: {e.detail}; shipping {pick['label']} as it stands",
                  flush=True)
            job.reset_channel("answer")
            job.add("answer", pick["code"])
            return pick["code"], settled
        picked = strip_fences(picked)
        _, usable = structural_notes(picked, record["finish"])
        if not (usable and looks_like_code(picked)):
            print(f"[job] {job.id} choose: the reply was not a script ({len(picked)} chars); "
                  f"shipping {pick['label']} as it stands", flush=True)
            job.reset_channel("answer")
            job.add("answer", pick["code"])
            return pick["code"], settled
        if job.channel("answer").strip() != picked:
            job.reset_channel("answer")
            job.add("answer", picked)
        script = picked
    return script, settled


def negotiate(job: Job, chat: "ReviewerChat", draft: str, notes: list,
              rounds: int = NEGOTIATE_ROUNDS, version_channel: str = "peer",
              agree_channel: str = "agree", tokens: int = PEER_TOKENS) -> tuple:
    """One reader's rounds: its own script, then the writer's merge, until it would ship it.

    Round one is the reader writing the script itself instead of complaining about the other one.
    Every round after that is the reader reading the merged script: AGREE ends those rounds,
    another version starts the next merge. Bounded by `rounds`, so a pair that never agrees still
    finishes -- and the last script that stands is what ships either way. The second reader runs
    through this same loop, over the script the first one settled on, in its own channels.
    """
    best = draft
    outcome = "draft"
    reader = chat.provider.model
    for index in range(max(0, rounds)):
        opening = index == 0
        phase = version_channel if opening else agree_channel
        note = (f"{reader} writing its own version" if opening
                else f"{reader} reading the merged script")
        request = (peer_request(job, best, QWEN_MODEL, notes) if opening
                   else verify_request(job, best, QWEN_MODEL, notes))
        answer = chat.say(request, phase, phase, note, tokens)
        verdict, proposed = parse_verdict(answer)
        if verdict == "AGREE":
            outcome = "draft" if opening else "agreed"
            who = "the draft" if opening else "the merged script"
            print(f"[job] {job.id} {phase}: {reader} agreed with {who}", flush=True)
            break
        if verdict != "BETTER" or not proposed:
            outcome = "draft" if opening else "kept"
            print(f"[job] {job.id} {phase}: {reader} proposed nothing usable "
                  f"({len(answer)} chars back); {QWEN_MODEL}'s version stands", flush=True)
            break
        print(f"[job] {job.id} {phase}: {reader} proposed a version "
              f"({len(proposed)} chars of script)", flush=True)
        # The bubble shows the script, not the verdict line in front of it.
        if job.channel(phase).strip() != proposed:
            job.reset_channel(phase)
            job.add(phase, proposed)
        merged = merge_versions(job, best, proposed, notes, reader)
        if not merged:
            outcome = "kept"
            break
        best = merged
        outcome = "merged"
    return best, outcome


def outcome_note(outcome: str, calls: int) -> str:
    """One sentence for the turn's status line: what the models settled on."""
    if outcome == "chosen":
        return "the option with the most lines ships"
    if outcome == "agreed":
        return f"{REVIEWER.model} agreed with the merged script"
    if outcome == "merged":
        return (f"{REVIEWER.model} still proposed changes; the last merged script ships "
                f"({calls} model calls)")
    if outcome == "kept":
        return f"{QWEN.model} kept its own version"
    if outcome == "stopped":
        return f"{QWEN.model} answered; the negotiation stopped early"
    return f"{REVIEWER.model} had nothing better; {QWEN.model}'s draft ships"


def script_that_stands(job: Job, fallback: str) -> str:
    """The last script a completed call put in the answer channel, or `fallback`.

    Used when a reader dies in the middle of its rounds: the rounds' own variable went with the
    failure, but a merge that finished is in the channel. A call that failed part-way is not a
    completed call -- run_phase records one only after its stream ends -- so this can never hand
    back half a script.
    """
    if job.phases and job.phases[-1]["phase"] == "merge":
        return job.channel("answer").strip() or fallback
    return fallback


def second_outcome_note(outcome: str) -> str:
    """One sentence for the second reader's rounds, in the same vocabulary as the first's."""
    if outcome == "agreed":
        return f"{ZAI.model} agreed with the script both writers settled on"
    if outcome == "merged":
        return (f"{ZAI.model} proposed changes and {QWEN.model} merged them; the merged "
                "script ships")
    if outcome == "draft":
        return f"{ZAI.model} had nothing better than the script it read"
    return f"{ZAI.model} proposed nothing usable; the script in front of it stands"


def run_job(job: Job) -> None:
    """Draft, then both readers competing on the same script, on a thread of its own.

    The shape is: the writer drafts; the first reader is briefed, writes its own version, the
    writer merges the two in the chat it drafted in, and the reader says whether it would ship
    that; then the second reader does the same over the script the first two settled on. Nothing
    here is a suggestion box -- every model produces scripts, and what is sent to the user is the
    one they settled on. A turn never ends on "which one do you prefer?" either: that question is
    answered here, in the writer's own chat, with the option that has the most lines.
    """
    if not slot_take():
        job.finish(status="error",
                   error=f"{MAX_CONCURRENT} chains are already running; try again shortly")
        return
    try:
        # Each reader reads its brief on its own thread, while the writer drafts. They are three
        # different providers and none of them waits on another, so the acknowledgements cost no
        # turn time at all -- the only thing that has to be ordered is the request for a script,
        # which cannot go until the draft and that reader's reading are both done.
        chat = (ReviewerChat(job) if (job.want_review and review_enabled() and NEGOTIATE_ROUNDS > 0)
                else None)
        # The second reader only runs when the first one does: the chain is draft -> DeepSeek ->
        # merge -> GLM -> merge, so GLM reads what the first two already settled on.
        chat2 = (ReviewerChat(job, ZAI, SECOND_BRIEF, SECOND_BRIEF_NAME, SECOND_SEED,
                              SECOND_SEED_TOKENS, temperature=SECOND_TEMPERATURE,
                              seed_channel="seed2")
                 if (chat is not None and second_enabled()) else None)
        seed_error: dict = {}
        seed2_error: dict = {}

        def seed_reader(reader: "ReviewerChat", box: dict) -> None:
            """Read one reader's brief, recording a failure rather than raising it.

            A reader that is down, rate limited or out of credits must not cost the user the
            draft that is already written, so the reason is kept and shown on that reader's chip.
            """
            try:
                reader.seed()
            except HTTPException as e:
                box["detail"] = str(e.detail)
            except Exception as e:  # never let a broken reader thread take the turn down
                box["detail"] = f"{e.__class__.__name__}: {e}"

        seeder = threading.Thread(target=seed_reader, args=(chat, seed_error), daemon=True)
        seeder2 = threading.Thread(target=seed_reader, args=(chat2, seed2_error), daemon=True)
        if chat is not None:
            # Announced before the draft's own call starts, because each brief really is read
            # first: the page puts a brief's bubble up on this frame, so it lands above the draft
            # rather than wherever the reader happened to attach.
            job.finish(phase="seed",
                       note=f"{REVIEWER.model} reading "
                            f"{BRIEF_PATH.name if BRIEF else 'the brief'}")
            seeder.start()
        if chat2 is not None:
            # send2.txt goes out on its own, before anything is asked of the second reader, and
            # in parallel with the draft and the first brief, so it costs no turn time.
            job.finish(phase="seed2", note=f"{ZAI.model} reading {SECOND_BRIEF_NAME}")
            seeder2.start()

        draft, _ = run_phase(job, job.messages, job.temperature, QWEN, DRAFT_TOKENS,
                             "draft", "draft", f"{QWEN.model} writing a draft")
        draft = strip_fences(draft)
        # The channel is what a reader sees and what a later reader replays, so the fences the
        # model wrapped the script in are dropped from it as well as from the answer.
        if job.channel("draft").strip() != draft:
            job.reset_channel("draft")
            job.add("draft", draft)
        notes, usable = structural_notes(draft, job.phases[-1]["finish"])
        if not usable:
            reason = "; ".join(notes) or "the model returned nothing"
            raise HTTPException(502, f"{reason} -- try again, or raise DRAFT_TOKENS")

        # Nothing is reviewed when nothing is configured or asked for, and nothing is competed
        # over when the negotiation is off: the draft is the answer, and the answer channel
        # carries it so a reader sees one stream either way.
        if chat is None:
            # Even with nothing to compete with, a turn cannot end on "which one do you
            # prefer?": the writer is answered with the option that has the most code.
            best, chosen = settle_choice(job, draft, notes)
            if job.channel("answer").strip() != best.strip():
                job.reset_channel("answer")
                job.add("answer", best)
            job.finish(status="done", phase="done",
                       note=(f"{QWEN.model} asked which one; {outcome_note('chosen', 1)}"
                             if chosen else
                             (f"{QWEN.model} answered"
                              if not (job.want_review and review_enabled())
                              else "the competition is off; the draft ships")))
            note_error("")
            return

        best = draft
        outcome = "draft"
        seeder.join()
        if seed_error:
            # A reviewer that is down, rate limited or out of credits must not cost the user the
            # draft that is already written. It is recorded instead, so the reviewer chip shows it
            # rather than looking like a negotiation that changed nothing.
            detail = seed_error["detail"]
            print(f"[job] {job.id} seed failed: {detail}", flush=True)
            note_review_error(detail)
            job.review_text = f"[the review did not happen: {detail}]"
            job.add("review", job.review_text)
            # The second reader is not asked either: the chain is draft -> first reader -> merge
            # -> second reader, so there is nothing for it to read when the first one never got
            # through its own brief. Said out loud rather than passed off as a chain that ran.
            second_note = ((f"{ZAI.model} was not asked: the first reader could not read its own "
                            "brief") if chat2 is not None else "")
            best, chosen = settle_choice(job, best, notes)
            if job.channel("answer").strip() != best.strip():
                job.reset_channel("answer")
                job.add("answer", best)
            first_line = (f"{QWEN.model} asked which one; {outcome_note('chosen', 1)}"
                          if chosen else f"{QWEN.model} answered; no review")
            job.finish(status="done", phase="done",
                       note=f"{first_line}; {second_note}" if second_note else first_line)
            return
        note_review_error("")

        try:
            best, outcome = negotiate(job, chat, draft, notes)
            job.review_text = chat.last_answer[:MERGE_PASTE_MAX]
        except HTTPException as e:
            # The same rule half-way through: a reviewer that dies mid-negotiation leaves the
            # script that stands, which is the last merged one rather than nothing.
            print(f"[job] {job.id} negotiation stopped: {e.detail}", flush=True)
            note_review_error(str(e.detail))
            job.review_text = f"[the review stopped early: {e.detail}]"
            job.add("review", job.review_text)
            outcome = "stopped"
            best = script_that_stands(job, best)

        # The second reader's rounds: the same protocol, over the script the first two settled
        # on. Its brief was already read (that thread started with the draft). A reader that
        # cannot be reached at all leaves the script that stands untouched -- said out loud
        # rather than passed off as a chain that ran.
        second_note = ""
        if chat2 is not None:
            seeder2.join()
            if seed2_error:
                detail = seed2_error["detail"]
                print(f"[job] {job.id} seed2 failed: {detail}", flush=True)
                peers.note_failure(detail)
                second_note = f"{ZAI.model} did not read {SECOND_BRIEF_NAME} ({detail})"
            else:
                try:
                    best, second_outcome = negotiate(job, chat2, best, notes, SECOND_ROUNDS,
                                                     "peer2", "agree2", SECOND_TOKENS)
                    job.second_text = chat2.last_answer[:MERGE_PASTE_MAX]
                    second_note = second_outcome_note(second_outcome)
                    print(f"[job] {job.id} second reader: {second_note}", flush=True)
                    peers.note_failure("")
                except HTTPException as e:
                    # A reader that dies half-way through leaves the script that stood, which is
                    # the last merged one rather than nothing.
                    print(f"[job] {job.id} the second reader stopped: {e.detail}", flush=True)
                    peers.note_failure(str(e.detail))
                    best = script_that_stands(job, best)
                    job.second_text = f"[the second reader stopped early: {e.detail}]"
                    second_note = (f"{ZAI.model} stopped early ({e.detail}); the script that "
                                   "stood before it ships")

        # What ships is the script the two settled on, and a question is not one: if the last
        # answer offers choices and asks which is preferred, that is answered here with the
        # option that has the most lines. If the answer channel is empty, or holds a merge that
        # was thrown away, it is refilled from the one that stands.
        best, chosen = settle_choice(job, best, notes)
        if chosen:
            outcome = "chosen"
        if job.channel("answer").strip() != best.strip():
            job.reset_channel("answer")
            job.add("answer", best)
        note_error("")
        settled = outcome_note(outcome, len(job.phases))
        job.finish(status="done", phase="done",
                   note=f"{settled}; {second_note}" if second_note else settled)
        print(f"[job] {job.id} done in {job.report()['elapsed']:g}s, {len(best)} chars, "
              f"{len(job.phases)} model call(s)", flush=True)
    except HTTPException as e:
        # Printed as well as sent: the page shows it once, the log keeps it.
        print(f"[job] {job.id} failed: {e.detail}", flush=True)
        note_error(str(e.detail))
        job.finish(status="error", phase="error", error=str(e.detail))
    except httpx.HTTPError as e:
        detail = upstream_error(e, QWEN).detail
        print(f"[job] {job.id} failed: {detail}", flush=True)
        note_error(detail)
        job.finish(status="error", phase="error", error=detail)
    except Exception as e:  # a bug here must never leave a reader waiting forever
        print(f"[job] {job.id} crashed: {e.__class__.__name__}: {e}", flush=True)
        note_error(f"{e.__class__.__name__}: {e}")
        job.finish(status="error", phase="error", error=f"{e.__class__.__name__}: {e}")
    finally:
        slot_give()


def start_job(messages: list, temperature: Optional[float], review: Optional[bool] = None,
              request: Optional[Request] = None) -> Job:
    """Address the newest question, then set the work going on its own thread."""
    if not CONFIGURED:
        raise HTTPException(503, NOT_CONFIGURED)
    ip = client_ip(request)
    if not rate_ok(ip):
        raise HTTPException(429, f"too many requests from {ip}; {RATE_LIMIT} per minute")
    turns = greet(trim_messages(messages))
    want = review_enabled() if review is None else bool(review and review_enabled())
    note = f"{QWEN.model} drafting"
    job = Job(turns, temperature, note, want)
    register(job)
    chain = [QWEN_MODEL]
    if want:
        chain.append(REVIEWER.model)
        if second_enabled():
            chain.append(ZAI.model)
    print(f"[job] {job.id} on {' + '.join(chain)}: {len(turns)} turns, asking about "
          f"{last_user_text(turns).strip()[:60]!r}", flush=True)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


# --- the conversation endpoints --------------------------------------------------------

class GenReq(BaseModel):
    """One-shot request (client.lua and older callers): a prompt, no history."""

    prompt: str
    temperature: Optional[float] = 0.7
    review: Optional[bool] = None


class ChatReq(BaseModel):
    """One turn of a conversation.

    `messages` is the whole conversation the caller is keeping, oldest first. The newest user
    turn gets the greeting in front of it, and the list is what the model sees, so it answers
    in the same chat it has been answering in. `review: false` skips the reviewer for one turn.
    """

    messages: list
    temperature: Optional[float] = 0.7
    review: Optional[bool] = None


def job_summary(job: Job) -> dict:
    """A finished job as one object: the answer, plus what the reviewer said about the draft."""
    return {
        "job": job.id,
        "text": job.text(),
        "code": job.text(),
        "draft": job.channel("draft"),
        "peer": job.channel("peer"),
        "peer2": job.channel("peer2"),
        "review": job.review_text,
        "second_review": job.second_text,
        "model": QWEN_MODEL,
        "reviewer": REVIEWER.model if job.want_review and review_enabled() else "",
        "second": ZAI.model if job.want_review and second_enabled() else "",
        "thinking": QWEN_THINKING,
        "phases": job.phases,
        "elapsed": job.report()["elapsed"],
    }


@app.post("/chat/stream")
def chat_stream(req: ChatReq, request: Request):
    """Start one turn and hand back its job id immediately.

    Nothing is generated on this request, so it cannot hang and be dropped: the whole chain
    runs on the job's thread and the page watches /chat/stream/{job} instead. This is the
    endpoint the page uses, so it is not key-gated -- only rate limited.
    """
    messages = clean_messages(req.messages)
    job = start_job(messages, req.temperature, req.review, request)
    return {"job": job.id, "model": QWEN_MODEL,
            # Both readers, so a caller can see what the chain will be before watching it.
            "reviewer": REVIEWER.model if job.want_review else "",
            "second": ZAI.model if (job.want_review and second_enabled()) else "",
            "thinking": QWEN_THINKING, "turns": len(job.messages),
            # null rather than 0: there is no ceiling on this turn unless one is configured.
            "timeout": CHAT_TIMEOUT or None}


@app.post("/chat")
def chat(req: ChatReq, _: None = Depends(require_key)):
    """The same chain, blocking -- for callers that cannot follow a stream (client.lua)."""
    job = start_job(clean_messages(req.messages), req.temperature, req.review)
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
    sleeping phone drops, so the wait is punctuated with heartbeats -- and a chain runs three
    model calls, so there is a lot of waiting to punctuate.
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
                         "draft": job.channel("draft"), "peer": job.channel("peer"),
                         "peer2": job.channel("peer2"), "review": job.review_text,
                         "second_review": job.second_text,
                         "second": ZAI.model if (job.want_review and second_enabled()) else "",
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
    of one silent wait while three model calls happen.
    """
    job = lookup(job_id)
    body = job_summary(job) if job.status in ("done", "error") else {
        "job": job.id,
        "phase": job.phase,
        "note": job.note,
        "draft": job.channel("draft"),
        "peer": job.channel("peer"),
        "review": job.review_text,
        "model": QWEN_MODEL,
        "reviewer": REVIEWER.model if job.want_review and review_enabled() else "",
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
    negotiation. Set `CHAT_TIMEOUT` to a number and the total becomes a draft plus two calls per
    round plus the brief, with a little slack on top.
    """
    if CHAT_TIMEOUT <= 0:
        return 0.0
    if not review_enabled() or NEGOTIATE_ROUNDS <= 0:
        return CHAT_TIMEOUT + 30
    return CHAT_TIMEOUT * (2 + 2 * NEGOTIATE_ROUNDS) + 30


@app.post("/generate")
def generate(req: GenReq, _: None = Depends(require_key)):
    """Block until the whole chain is done -- this is the path `client.lua` uses."""
    job = start_job([{"role": "user", "content": req.prompt}], req.temperature, req.review)
    job.wait(job_wait())
    if job.status == "error":
        raise HTTPException(502, job.error)
    return job_summary(job)


@app.post("/generate/stream")
def start_stream(req: GenReq, _: None = Depends(require_key)):
    """The one-shot flow as a job, for callers that stream but keep no history."""
    job = start_job([{"role": "user", "content": req.prompt}], req.temperature, req.review)
    return {"job": job.id, "model": QWEN_MODEL, "timeout": CHAT_TIMEOUT or None}


# --- the OpenAI-compatible surface -----------------------------------------------------

def relay_stream(body: dict):
    """Pass qwen-api's SSE through untouched.

    Nothing is rewritten on this path, which is what keeps the rest of qwen-api working
    through the bridge: reasoning_content, tool calls, web-search annotations and the hidden
    continuation metadata all survive, so an OpenAI client that manages its own history keeps
    working exactly as it would against Qwen itself. No reviewer runs here: this surface is a
    passthrough, and a caller that wants the chain uses /chat.
    """
    with httpx.Client(timeout=client_timeout(CHAT_TIMEOUT),
                      follow_redirects=True) as c:
        with c.stream("POST", QWEN.endpoint(), json=body, headers=QWEN.headers()) as r:
            if r.status_code >= 400:
                yield sse_error(failure_reason(r.status_code, r.read().decode("utf-8", "replace"), QWEN))
                return
            if "event-stream" not in r.headers.get("content-type", ""):
                raw = r.read().decode("utf-8", "replace")
                text = message_text(raw)
                if not text:
                    yield sse_error(failure_reason(200, raw, QWEN))
                    return
                yield sse({"id": "chatcmpl-bridge", "object": "chat.completion.chunk",
                           "model": QWEN_MODEL,
                           "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
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
    web_search_options, reasoning_effort, temperature, stream -- passes through.
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
    """The models this bridge uses, and what else the proxy serves, for reference."""
    ids = [QWEN_MODEL]
    if review_enabled():
        ids.append(REVIEWER.model)
    if second_enabled():
        ids.append(ZAI.model)
    for mid in list_models():
        if mid not in ids:
            ids.append(mid)
    owners = {REVIEWER.model: "deepseek", ZAI.model: "zai"}
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": mid, "object": "model", "created": now,
         "owned_by": owners.get(mid, "qwen")} for mid in ids
    ]}


# --- the page ---------------------------------------------------------------------------

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
    chips = "".join([
        chip(True, "api", "online"),
        chip(state["bridge"], "bridge",
             state["last_error"] or (state["provider_label"] if state["bridge"]
                                     else "QWEN_TOKEN is not set")),
        chip(state["token_ok"], "token", state["token_detail"]),
        chip(state["reviewer"]["ok"], "reviewer",
             state["reviewer"]["detail"] or state["reviewer"]["model"]),
        # The second reader is a third account with a third thing that can be switched off, so it
        # gets its own chip rather than failing silently inside the chain.
        chip(state["second"]["ok"], "glm",
             state["second"]["detail"] or state["second"]["model"]),
        chip(True, "model", state["model"]),
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
            .replace("__REVIEWER__", html.escape(state["reviewer"]["model"] if state["reviewer"]
                                                 and state["reviewer"].get("model") else "reviewer"))
            .replace("__SECOND__", html.escape(state["second"]["model"] or "the second reader"))
            .replace("__SECOND_ON__", "true" if state["second"].get("on") else "false")
            .replace("__REVIEW_ON__", "true" if state["review"] else "false")
            # The brief goes out on its own before anything is asked, so the page can put its
            # bubble up first instead of waiting for it to appear after the draft's.
            .replace("__SEED_ON__", "true" if state["reviewer"].get("seed") else "false")
            .replace("__GREETING__", html.escape(GREETING))
    )


async def snapshot() -> dict:
    # Always reports rather than raising, so the platform healthcheck only depends on the API
    # being up; token, reviewer and model readiness come back in the body. The checks are
    # network calls, so they run off the event loop: /health is polled every few seconds and
    # must never hold up a turn.
    state, rev, second = await asyncio.gather(asyncio.to_thread(token_state),
                                             asyncio.to_thread(reviewer_state),
                                             asyncio.to_thread(second_state))
    body = {
        "status": "ok",
        "bridge": CONFIGURED,
        "endpoint": QWEN_URL,
        "provider_label": QWEN.label(),
        "model": QWEN_MODEL,
        "thinking": QWEN_THINKING,
        "greeting": GREETING,
        "token_ok": state["ok"],
        "token_detail": state["detail"],
        "review": review_enabled(),
        "reviewer": {**rev, "model": REVIEWER.model, "endpoint": REVIEW_URL,
                     "configured": REVIEWER.configured, "thinking": REVIEW_THINKING,
                     "brief_chars": len(BRIEF), "rounds": NEGOTIATE_ROUNDS,
                     "seed": SEED_BRIEF},
        "second": second,
        "limits": {"per_minute": RATE_LIMIT, "concurrent": MAX_CONCURRENT,
                   "running": running_now()},
        "last_error": last_error(),
        "api_key_required": bool(CALLER_KEY),
    }
    if not CONFIGURED:
        body["status"] = "degraded"
        body["error"] = NOT_CONFIGURED
    elif not review_enabled():
        body["status"] = "partial"
    return body


@app.get("/health")
async def health():
    return await snapshot()
