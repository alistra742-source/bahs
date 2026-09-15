from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio, hmac, html, httpx, json, os, threading, time, uuid

# --------------------------------------------------------------------------------------
# What this service is: a bridge to Qwen, and one conversation.
#
# chat.qwen.ai has no public API. github.com/encryptarun/qwen-api turns it into
# OpenAI-compatible endpoints, and it authenticates with the Qwen *access token* that
# chat.qwen.ai keeps in the browser (chat.qwen.ai -> DevTools console -> localStorage.token).
# That token is the key to a whole Qwen account, so it lives here, in this service's
# variables, and never in a browser or a Roblox client. Callers authenticate with
# API_KEY instead and talk to this service:
#
#   POST /chat/stream           one turn of that same conversation -> a job id
#   GET  /chat/stream/{job}     NDJSON: replay, text, heartbeats, done/error
#   POST /generate[...]         the same, without history (client.lua)
#   POST /v1/chat/completions   OpenAI-compatible, streaming or not
#   GET  /v1/models, /health
#
# Every generation goes to QWEN_MODEL, always, with thinking off ("fast"), and every
# question is sent as "Hy kanha <your question>". The conversation belongs to the caller:
# the turns it sends are the turns the model sees, so a follow-up is answered in the same
# chat, knowing what was said before. There is no prompt of ours anywhere in the chain.
#
# Nothing is pulled, loaded or warmed: no weights, no GPU, no volume, no database.
# --------------------------------------------------------------------------------------

QWEN_URL = os.getenv("QWEN_URL", "").strip().rstrip("/") or "https://qwen.aikit.club/v1"
# qwen-api also serves its own bookkeeping endpoints (/validate, /refresh) at the root.
QWEN_ROOT = QWEN_URL[: -len("/v1")] if QWEN_URL.endswith("/v1") else QWEN_URL


def qwen_token() -> str:
    """The Qwen access token, under whichever name it was put in the variables."""
    for name in ("QWEN_TOKEN", "QWEN_API_KEY", "QWEN_ACCESS_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


QWEN_TOKEN = qwen_token()
# Every request is sent to this model. There is no picker: one model, one behaviour.
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3.8-max").strip() or "qwen3.8-max"
# fast (answer straight away) | auto | thinking. Forced onto every request: reasoning
# tokens are billed against max_tokens and the answer is what is wanted, not the thinking.
THINKING = os.getenv("QWEN_THINKING", "fast").strip() or "fast"
# Every question is addressed to the model with this in front of it, so a request that
# reads "make me this" is sent as "Hy kanha make me this". Set GREETING to "" to send
# messages untouched.
GREETING = os.getenv("GREETING", "Hy kanha").strip()
# Without a token there is nothing to call, so the API says so plainly instead of
# forwarding an empty bearer and reporting the proxy's 401 back to the user.
CONFIGURED = bool(QWEN_TOKEN)
NOT_CONFIGURED = ("the bridge is not configured: set QWEN_TOKEN on this service to a "
                  "Qwen access token from chat.qwen.ai")

# When API_KEY is set, every endpoint requires it back as X-API-Key (or an
# Authorization: Bearer header). Unset means open, which is what a local run gets.
API_KEY = os.getenv("API_KEY", "").strip()

INDEX = Path(__file__).parent / "web" / "index.html"
# Qwen answers in seconds; this is a backstop for a provider that hangs.
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT", "300"))
# Applied only when the caller did not ask for something else. A chat answer needs more
# room than a snippet did.
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1024"))
# A generation is a server-side job, so a reader can go quiet without the answer being
# lost. That quiet is exactly what proxies and sleeping phones drop, so the stream is
# punctuated with a heartbeat this often.
HEARTBEAT = float(os.getenv("HEARTBEAT", "5"))
# How long a finished job stays readable, so a browser that comes back late can still
# collect the answer instead of finding nothing.
JOB_TTL = float(os.getenv("JOB_TTL", "3600"))
# How often /health may ask qwen-api whether the token still works.
TOKEN_CHECK_TTL = float(os.getenv("TOKEN_CHECK_TTL", "60"))
# How much history one request may carry. The newest turns are kept; the middle of a
# long conversation is dropped rather than growing the prompt forever.
HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "40"))
HISTORY_CHARS = int(os.getenv("HISTORY_CHARS", "120000"))


def provider_label() -> str:
    """Short name of whoever is answering, for the page's chips."""
    return QWEN_URL.split("//", 1)[-1].split("/", 1)[0]


def qwen_headers() -> dict:
    return {"Authorization": f"Bearer {QWEN_TOKEN}", "Content-Type": "application/json"}


# --- addressing the model --------------------------------------------------------------

def greet(messages: list) -> list:
    """Address the model before the newest question: "make me this" -> "Hy kanha make me this".

    Only the last user turn is touched. The earlier ones already carry the greeting, since
    it was applied when they were sent, and a caller that writes the greeting itself is
    not prefixed twice.
    """
    if not GREETING:
        return messages
    for index in range(len(messages) - 1, -1, -1):
        turn = messages[index]
        if turn.get("role") != "user" or not isinstance(turn.get("content"), str):
            continue
        text = turn["content"].strip()
        if not text or text.lower().startswith(GREETING.lower()):
            return messages
        out = list(messages)
        out[index] = dict(turn, content=f"{GREETING} {turn['content'].lstrip()}")
        return out
    return messages


# --- the turns of a conversation -------------------------------------------------------

ROLES = ("system", "user", "assistant", "tool", "function")


def clean_messages(raw) -> list:
    """The caller's turns, as the provider wants them: a role and some content."""
    if not isinstance(raw, list):
        raise HTTPException(400, "messages must be a list")
    turns = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip() not in ROLES:
            continue
        if item.get("content") is None:
            continue
        turns.append(item)
    if not turns:
        raise HTTPException(400, "messages must contain at least one turn with content")
    return turns


def trim_messages(messages: list) -> list:
    """Keep the newest turns inside the history budget.

    `head` is what is never dropped: the system message a caller put in front. Everything
    after it is a turn, and the oldest ones go first once the conversation is longer than
    HISTORY_MESSAGES or fatter than HISTORY_CHARS.
    """
    head = 0
    while head < len(messages) and messages[head].get("role") == "system":
        head += 1
    kept = list(messages)

    def size() -> int:
        return sum(len(m["content"]) for m in kept if isinstance(m.get("content"), str))

    while (len(kept) - head > HISTORY_MESSAGES or size() > HISTORY_CHARS) and len(kept) - head > 2:
        kept.pop(head)
    return kept


def last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


# --- what the provider said, when it said no -------------------------------------------

def failure_reason(status: int, body: str, model: str = "") -> str:
    """The proxy's own sentence for a failure, plus the one fix that is not obvious.

    Whatever qwen-api (or Qwen upstream) rejected the call with is the useful part, so it
    is passed through verbatim instead of being replaced by a generic message.
    """
    text = (body or "").strip()
    message = text
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("type") or text)
        elif error:
            message = str(error)
        elif payload.get("detail"):
            message = str(payload["detail"])
    message = " ".join(str(message).split())[:300] or "no detail"
    model = model or QWEN_MODEL
    if status == 401:
        return (f"QWEN_TOKEN was rejected by qwen-api ({message}) -- copy a fresh token "
                "from chat.qwen.ai (DevTools console: localStorage.token) and update the "
                "variable on this service")
    if status == 403:
        return f"qwen-api refused the request ({message})"
    if status == 404:
        return f"{model} is not a model this endpoint serves ({message})"
    if status == 429:
        return (f"qwen-api is rate limiting ({message}) -- retry shortly, or set "
                "QWEN_MODEL to another model")
    if status >= 500:
        return f"qwen-api is failing ({status}: {message})"
    return f"{status}: {message}"


def sse(payload: dict) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def sse_error(message: str) -> str:
    """An OpenAI-style error frame; the only shape a reader can report once a stream began."""
    return sse({"error": {"message": message, "type": "upstream_error"}})


def message_text(body: str) -> str:
    """The assistant text out of a non-streamed completion, or '' if it is not one."""
    try:
        payload = json.loads((body or "").strip())
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("content") or ""


# --- the model list, and whether the token still works ---------------------------------

_models: dict = {"at": 0.0, "ids": []}
_models_lock = threading.Lock()

def list_models(force: bool = False) -> list:
    """qwen-api's model ids, remembered for a few minutes; empty when unreadable.

    Never raises: it feeds a health chip and documents what else QWEN_MODEL could be,
    so an unreachable proxy must leave the page usable rather than break it.
    """
    with _models_lock:
        cached = dict(_models)
    if not force and cached["at"] and time.time() - cached["at"] < 300:
        return cached["ids"]
    ids: list = []
    if CONFIGURED:
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as c:
                r = c.get(f"{QWEN_URL}/models", headers=qwen_headers())
            if r.status_code < 400:
                payload = r.json()
                for item in (payload.get("data") or payload.get("models") or []):
                    if isinstance(item, dict) and item.get("id"):
                        ids.append(str(item["id"]))
                    elif isinstance(item, str):
                        ids.append(item)
        except (httpx.HTTPError, ValueError):
            ids = []
    with _models_lock:
        _models.update({"at": time.time(), "ids": ids})
    return ids

_token: dict = {"at": 0.0, "ok": False, "detail": "not checked"}
_token_lock = threading.Lock()

def token_state(force: bool = False) -> dict:
    """Ask qwen-api whether QWEN_TOKEN is still good, remembering the answer briefly.

    Qwen access tokens expire, and an expired one is otherwise indistinguishable from a
    hung generation, so the chip reports it before you send anything.
    """
    with _token_lock:
        cached = dict(_token)
    if not force and cached["at"] and time.time() - cached["at"] < TOKEN_CHECK_TTL:
        return cached
    if not CONFIGURED:
        state = {"at": time.time(), "ok": False, "detail": "QWEN_TOKEN is not set"}
    else:
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as c:
                # The token goes in the body (as qwen-api documents) and as a bearer,
                # since some builds authenticate every route either way.
                r = c.post(f"{QWEN_ROOT}/validate", json={"token": QWEN_TOKEN},
                           headers=qwen_headers())
            detail = (r.text or "")[:300]
            if r.status_code == 404:
                # A qwen-api build without /validate; generations still report for real.
                state = {"at": time.time(), "ok": True, "detail": "token set"}
            elif r.status_code >= 400:
                state = {"at": time.time(), "ok": False,
                         "detail": failure_reason(r.status_code, detail, "the token")}
            else:
                ok = True
                try:
                    payload = r.json()
                    if isinstance(payload, dict):
                        for flag in ("valid", "success", "ok"):
                            if payload.get(flag) is False:
                                ok = False
                except ValueError:
                    pass
                state = {"at": time.time(), "ok": ok,
                         "detail": "token accepted" if ok else "token rejected"}
        except httpx.HTTPError as e:
            state = {"at": time.time(), "ok": False,
                     "detail": f"cannot reach {QWEN_ROOT} ({e.__class__.__name__})"}
    with _token_lock:
        _token.update(state)
    return state


# --- jobs ------------------------------------------------------------------------------

class Job:
    """One generation, owned by a background thread rather than by the caller.

    A phone that gave up on an answer used to take the whole generation with it: the
    request was the only thing driving the model, so nothing was left to read. A job
    runs to completion on its own thread, keeps every piece it has produced, and any
    number of readers can attach to it -- including one that comes back after the
    connection dropped, which replays the output from the start and follows along.
    """

    def __init__(self, messages: list, temperature: Optional[float], note: str):
        self.id = uuid.uuid4().hex[:12]
        self.messages = messages
        self.temperature = temperature
        self.note = note
        self.pieces: list = []
        self.text_value = ""
        self.error = ""
        self.status = "queued"  # queued -> running -> done | error
        self.started = time.time()
        self.finished = 0.0
        self.cond = threading.Condition()

    def text(self) -> str:
        return "".join(self.pieces)

    def add(self, piece: str) -> None:
        with self.cond:
            self.pieces.append(piece)
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
        """Block until the job ends; the blocking /generate is the only caller."""
        deadline = time.time() + timeout
        with self.cond:
            while self.status not in ("done", "error"):
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise HTTPException(504, f"timed out after {timeout:g}s waiting on {provider_label()}")
                self.cond.wait(timeout=remaining)

    def report(self) -> dict:
        """Where the job stands; what the page shows next to its running timer."""
        return {
            "status": self.status,
            "note": self.note,
            "model": QWEN_MODEL,
            "thinking": THINKING,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "chars": len(self.text()),
        }


_jobs: dict = {}
_jobs_lock = threading.Lock()

# The last thing that went wrong, so /health (and the page's chip) can report it long
# after the error frame has scrolled by. Cleared by the next generation that succeeds.
_last_error = ""
_last_error_lock = threading.Lock()

def note_error(text: str) -> None:
    global _last_error
    with _last_error_lock:
        _last_error = text[:300]

def last_error() -> str:
    with _last_error_lock:
        return _last_error

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
    # Nothing to load or warm: there are no local weights, only an API call.
    if CONFIGURED:
        print(f"[bridge] {QWEN_URL} -> {QWEN_MODEL} (thinking: {THINKING})", flush=True)
    else:
        print(f"[bridge] {NOT_CONFIGURED}", flush=True)
    if GREETING:
        print(f"[chat] every question is sent as {GREETING} <your question>", flush=True)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def require_key(x_api_key: Optional[str] = Header(None),
                authorization: Optional[str] = Header(None)) -> None:
    """Gate the endpoints when the service defines API_KEY.

    This is the only credential a caller ever handles: the Qwen token stays here, so it
    never reaches a browser, a Roblox client or a log.
    """
    if not API_KEY:
        return
    supplied = x_api_key or ""
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(401, "missing or invalid API key (send it as the X-API-Key header)")


# --- talking to qwen-api ---------------------------------------------------------------

def chat_timeout() -> httpx.Timeout:
    """Generous read window: Qwen answers in seconds, this is a backstop."""
    return httpx.Timeout(CHAT_TIMEOUT, connect=10.0)

def upstream_error(e: httpx.HTTPError) -> HTTPException:
    """Map an httpx failure onto a status the caller can act on."""
    if isinstance(e, httpx.TimeoutException):
        return HTTPException(504, f"{provider_label()} timed out after {CHAT_TIMEOUT:g}s")
    return HTTPException(502, f"cannot reach {QWEN_URL} ({e.__class__.__name__})")

def request_body(messages: list, temperature: Optional[float]) -> dict:
    """An OpenAI-shaped request, pinned to the configured model.

    The model and `thinking_mode` are not the caller's to choose here: one model, fast
    answers, so a request behaves the same whoever sends it.
    """
    body = {
        "model": QWEN_MODEL,
        "messages": messages,
        "stream": True,
        "thinking_mode": THINKING,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if MAX_TOKENS > 0:
        body["max_tokens"] = MAX_TOKENS
    return body

def stream_text(messages: list, temperature: Optional[float]):
    """Stream an answer, piece by piece, out of qwen-api's /chat/completions."""
    body = request_body(messages, temperature)
    with httpx.Client(timeout=chat_timeout(), follow_redirects=True) as c:
        with c.stream("POST", f"{QWEN_URL}/chat/completions", json=body, headers=qwen_headers()) as r:
            if r.status_code >= 400:
                detail = r.read().decode("utf-8", "replace")
                raise HTTPException(502, failure_reason(r.status_code, detail, QWEN_MODEL))
            if "event-stream" not in r.headers.get("content-type", ""):
                # Not a stream: either the proxy rejected the request with a 200, or it
                # ignored stream=true and answered in one piece. Reading the body tells
                # us which; reporting an empty answer would hide the reason.
                raw = r.read().decode("utf-8", "replace")
                text = message_text(raw)
                if not text:
                    raise HTTPException(502, failure_reason(200, raw, QWEN_MODEL))
                print("[bridge] answered in one piece instead of streaming", flush=True)
                yield text
                return
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    return
                if not data:
                    continue
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                # reasoning_content is deliberately skipped: the answer is what is wanted.
                piece = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                if piece:
                    yield piece

def run_job(job: Job) -> None:
    """Produce the answer on a thread of its own: no reader, no lost work."""
    job.finish(status="running")
    try:
        for piece in stream_text(job.messages, job.temperature):
            job.add(piece)
        text = job.text().strip()
        if not text:
            job.finish(status="error", error="the model returned nothing; try again")
            return
        job.finish(status="done", text_value=text)
        note_error("")
        print(f"[job] {job.id} done in {job.report()['elapsed']:g}s, {len(text)} chars",
              flush=True)
    except HTTPException as e:
        # Printed as well as sent: the page shows it once, the log keeps it.
        print(f"[job] {job.id} failed: {e.detail}", flush=True)
        note_error(str(e.detail))
        job.finish(status="error", error=str(e.detail))
    except httpx.HTTPError as e:
        detail = upstream_error(e).detail
        print(f"[job] {job.id} failed: {detail}", flush=True)
        note_error(detail)
        job.finish(status="error", error=detail)
    except Exception as e:  # a bug here must never leave a reader waiting forever
        print(f"[job] {job.id} crashed: {e.__class__.__name__}: {e}", flush=True)
        note_error(f"{e.__class__.__name__}: {e}")
        job.finish(status="error", error=f"{e.__class__.__name__}: {e}")


def start_job(messages: list, temperature: Optional[float]) -> Job:
    """Address the newest question, then set the work going on its own thread."""
    if not CONFIGURED:
        raise HTTPException(503, NOT_CONFIGURED)
    turns = greet(trim_messages(messages))
    job = Job(turns, temperature, f"{QWEN_MODEL} · {THINKING} via {provider_label()}")
    register(job)
    print(f"[job] {job.id} on {QWEN_MODEL}: {len(turns)} turns, asking about "
          f"{last_user_text(turns).strip()[:60]!r}", flush=True)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


# --- the conversation endpoints --------------------------------------------------------

class GenReq(BaseModel):
    """One-shot request (client.lua and older callers): a prompt, no history."""

    prompt: str
    temperature: Optional[float] = 0.7


class ChatReq(BaseModel):
    """One turn of a conversation.

    `messages` is the whole conversation the caller is keeping, oldest first. The newest
    user turn gets the greeting in front of it, and the list is what the model sees, so
    it answers in the same chat it has been answering in.
    """

    messages: list
    temperature: Optional[float] = 0.7


@app.post("/chat/stream")
def chat_stream(req: ChatReq, _: None = Depends(require_key)):
    """Start one turn of the conversation and hand back its job id immediately.

    Nothing is generated on this request, so it cannot hang and be dropped: the waiting
    happens on the job's thread and the page watches /chat/stream/{job} instead.
    """
    messages = clean_messages(req.messages)
    job = start_job(messages, req.temperature)
    return {"job": job.id, "model": QWEN_MODEL, "thinking": THINKING,
            "turns": len(job.messages), "timeout": CHAT_TIMEOUT}


def frame(payload: dict) -> str:
    return json.dumps(payload) + "\n"

def job_frames(job: Job):
    """NDJSON for one reader: everything the job has so far, then each new piece.

    Every reader starts at zero, so a browser whose connection died just asks again and
    rebuilds the same answer while the job carries on. The frames in between matter even
    when there is nothing to report: a stream that goes silent for minutes is what a
    proxy or a sleeping phone drops, so the wait is punctuated with heartbeats.
    """
    index = 0
    yield frame({"replay": True, "job": job.id, **job.report()})
    while True:
        with job.cond:
            # Wait only while there is nothing new and the job is still going: a
            # finished job is handed over at once rather than after a heartbeat.
            if not job.pieces[index:] and job.status in ("queued", "running"):
                job.cond.wait(timeout=HEARTBEAT)
            pieces = job.pieces[index:]
            index += len(pieces)
            report = job.report()
            status, error, text = job.status, job.error, job.text_value
        for piece in pieces:
            yield frame({"t": piece})
        if status == "error":
            yield frame({"error": error, "text": text})
            return
        if status == "done":
            yield frame({"done": True, "text": text, "code": text})
            return
        if not pieces:
            yield frame({"beat": True, **report})


@app.get("/chat/stream/{job_id}")
@app.get("/generate/stream/{job_id}")
def watch_stream(job_id: str, _: None = Depends(require_key)):
    """Stream a job's progress. Calling it again after a drop is the whole point."""
    job = lookup(job_id)
    return StreamingResponse(
        job_frames(job),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/generate")
def generate(req: GenReq, _: None = Depends(require_key)):
    """Block until the answer is ready -- this is the path `client.lua` uses.

    The prompt with the greeting in front, no history: one request, one answer.
    """
    job = start_job([{"role": "user", "content": req.prompt}], req.temperature)
    job.wait(CHAT_TIMEOUT + 30)
    if job.status == "error":
        raise HTTPException(502, job.error)
    return {"text": job.text_value, "code": job.text_value, "model": QWEN_MODEL}


@app.post("/generate/stream")
def start_stream(req: GenReq, _: None = Depends(require_key)):
    """The one-shot flow as a job, for callers that stream but keep no history."""
    job = start_job([{"role": "user", "content": req.prompt}], req.temperature)
    return {"job": job.id, "model": QWEN_MODEL, "timeout": CHAT_TIMEOUT}


# --- the OpenAI-compatible surface -----------------------------------------------------

def relay_stream(body: dict):
    """Pass qwen-api's SSE through untouched.

    Nothing is rewritten on this path, which is what keeps the rest of qwen-api working
    through the bridge: reasoning_content, tool calls, web-search annotations and the
    hidden continuation metadata all survive, so an OpenAI client that manages its own
    history keeps working exactly as it would against Qwen itself.
    """
    with httpx.Client(timeout=chat_timeout(), follow_redirects=True) as c:
        with c.stream("POST", f"{QWEN_URL}/chat/completions", json=body, headers=qwen_headers()) as r:
            if r.status_code >= 400:
                yield sse_error(failure_reason(r.status_code, r.read().decode("utf-8", "replace"),
                                               QWEN_MODEL))
                return
            if "event-stream" not in r.headers.get("content-type", ""):
                raw = r.read().decode("utf-8", "replace")
                text = message_text(raw)
                if not text:
                    yield sse_error(failure_reason(200, raw, QWEN_MODEL))
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

    A caller sends exactly what it would send to OpenAI. The newest user turn gets the
    greeting in front of it and the model is pinned to QWEN_MODEL. Everything else --
    tools, web_search_options, reasoning_effort, temperature, stream -- passes through.
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
    body["thinking_mode"] = THINKING
    if MAX_TOKENS > 0 and "max_tokens" not in body and "max_completion_tokens" not in body:
        body["max_tokens"] = MAX_TOKENS
    if body.get("stream"):
        return StreamingResponse(relay_stream(body), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    try:
        with httpx.Client(timeout=chat_timeout(), follow_redirects=True) as c:
            r = c.post(f"{QWEN_URL}/chat/completions", json=body, headers=qwen_headers())
    except httpx.HTTPError as e:
        raise upstream_error(e)
    if r.status_code >= 400:
        raise HTTPException(502, failure_reason(r.status_code, r.text, QWEN_MODEL))
    return Response(r.content, media_type="application/json")


@app.get("/v1/models")
def models(_: None = Depends(require_key)):
    """The model this bridge uses, and what else the proxy serves, for reference."""
    ids = [QWEN_MODEL]
    for mid in list_models():
        if mid not in ids:
            ids.append(mid)
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": mid, "object": "model", "created": now, "owned_by": "qwen"} for mid in ids
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
        chip(state["bridge"], "bridge", provider_label() if state["bridge"] else "QWEN_TOKEN is not set"),
        chip(state["token_ok"], "token", state["token_detail"]),
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
            .replace("__GREETING__", html.escape(GREETING))
            .replace("__KEY_REQUIRED__", "true" if API_KEY else "false")
    )


async def snapshot() -> dict:
    # Always reports rather than raising, so the platform healthcheck only depends on the
    # API being up; token and model readiness come back in the body.
    # The token check is a network call, so it runs off the event loop: /health is polled
    # every few seconds and must never hold up a generation.
    state = await asyncio.to_thread(token_state)
    body = {
        "status": "ok",
        "bridge": CONFIGURED,
        "endpoint": QWEN_URL,
        "provider_label": provider_label(),
        "model": QWEN_MODEL,
        "thinking": THINKING,
        "greeting": GREETING,
        "token_ok": state["ok"],
        "token_detail": state["detail"],
        "last_error": last_error(),
        "api_key_required": bool(API_KEY),
    }
    if not CONFIGURED:
        body["status"] = "degraded"
        body["error"] = NOT_CONFIGURED
    return body


@app.get("/health")
async def health():
    return await snapshot()
