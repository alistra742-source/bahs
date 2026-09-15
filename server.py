from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio, hmac, html, httpx, json, os, threading, time, uuid

try:  # the bridge runs without a database; Postgres only remembers what worked
    import psycopg
except ImportError:  # pragma: no cover - depends on the image
    psycopg = None

# What a failed database call can raise, with psycopg possibly absent from the image.
DB_ERRORS = (RuntimeError, psycopg.Error) if psycopg is not None else (RuntimeError,)

# --------------------------------------------------------------------------------------
# What this service is: a bridge.
#
# chat.qwen.ai has no public API. github.com/encryptarun/qwen-api turns it into
# OpenAI-compatible endpoints, and it authenticates with the Qwen *access token* that
# chat.qwen.ai keeps in the browser (chat.qwen.ai -> DevTools console -> localStorage.token).
# That token is the key to a whole Qwen account, so it lives here, in this service's
# variables, and never in a browser or a Roblox client. Callers authenticate with
# API_KEY instead, and talk to this service:
#
#   GET  /v1/models              the models the bridge can reach
#   POST /v1/chat/completions    anything that speaks OpenAI, streaming or not
#   POST /generate, /generate/stream[/{job}]   the Luau script flows
#
# Nothing is pulled, loaded or warmed: no weights, no GPU, no volume, no Ollama service.
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
# qwen3.8-max is the flagship and the strongest at code; qwen3-coder-plus is the
# code-specialised one, qwen3.5-omni-plus takes audio/image input.
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3.8-max").strip() or "qwen3.8-max"
# fast (answer straight away) | auto (reason when it helps) | thinking (always reason).
# Reasoning tokens count towards MAX_TOKENS, so raise MAX_TOKENS if you switch.
THINKING = os.getenv("QWEN_THINKING", "fast").strip() or "fast"
# Without a token there is nothing to call, so the API says so plainly instead of
# forwarding an empty bearer and reporting the proxy's 401 back to the user.
CONFIGURED = bool(QWEN_TOKEN)
NOT_CONFIGURED = ("the bridge is not configured: set QWEN_TOKEN on this service to a "
                  "Qwen access token from chat.qwen.ai")

# When API_KEY is set, every model endpoint requires it back as X-API-Key (or an
# Authorization: Bearer header). Unset means open, which is what a local run gets.
API_KEY = os.getenv("API_KEY", "").strip()

INDEX = Path(__file__).parent / "web" / "index.html"
# A hosted model answers in seconds; this is a backstop for a provider that hangs.
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT", "300"))
# Applied only when the caller did not ask for something else.
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "512"))
# A generation is a server-side job, so a reader can go quiet without the answer being
# lost. That quiet is exactly what proxies and sleeping phones drop, so the stream is
# punctuated with a heartbeat this often.
HEARTBEAT = float(os.getenv("HEARTBEAT", "5"))
# How long a finished job stays readable, so a browser that comes back late can still
# collect the answer instead of finding nothing.
JOB_TTL = float(os.getenv("JOB_TTL", "3600"))
# How often /health may ask qwen-api whether the token still works.
TOKEN_CHECK_TTL = float(os.getenv("TOKEN_CHECK_TTL", "60"))

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""


def provider_label() -> str:
    """Short name of whoever is answering, for the page's chips."""
    return QWEN_URL.split("//", 1)[-1].split("/", 1)[0]


def qwen_headers() -> dict:
    return {"Authorization": f"Bearer {QWEN_TOKEN}", "Content-Type": "application/json"}


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

    Never raises: it feeds a dropdown and a health chip, so an unreachable proxy must
    leave the page usable rather than break it.
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
    hung generation, so the page's chip reports it before you press Generate.
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

    def __init__(self, system: str, prompt: str, temperature: Optional[float],
                 note: str, model: str):
        self.id = uuid.uuid4().hex[:12]
        self.system = system
        self.prompt = prompt
        self.temperature = temperature
        self.note = note
        self.model = model
        self.pieces: list = []
        self.code = ""
        self.script_id = None
        self.saved = True
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
            "model": self.model,
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
        raise HTTPException(404, "unknown or expired job; start a new generation")
    return job


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Nothing to load or warm: there are no local weights, only an API call.
    if CONFIGURED:
        print(f"[bridge] {QWEN_URL} -> {QWEN_MODEL} (thinking: {THINKING})", flush=True)
    else:
        print(f"[bridge] {NOT_CONFIGURED}", flush=True)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def require_key(x_api_key: Optional[str] = Header(None),
                authorization: Optional[str] = Header(None)) -> None:
    """Gate the model endpoints when the service defines API_KEY.

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


# --- Postgres: optional memory of what worked -----------------------------------------

SCHEMA = """CREATE TABLE IF NOT EXISTS scripts (
    id SERIAL PRIMARY KEY,
    prompt TEXT,
    code TEXT,
    success INTEGER DEFAULT -1,
    fail_reason TEXT DEFAULT ''
)"""

_conn: Optional["psycopg.Connection"] = None
_conn_lock = threading.Lock()
_db_warned = False

def _dsn() -> str:
    # libpq understands the postgres:// scheme, psycopg is happier with postgresql://.
    if DATABASE_URL.startswith("postgres://"):
        return "postgresql://" + DATABASE_URL[len("postgres://"):]
    return DATABASE_URL

def db() -> "psycopg.Connection":
    """Open the Postgres connection on first use, and reconnect if it dropped.

    Connecting lazily keeps the API up while Postgres is still booting and makes the
    container stateless, so the service needs no volume of its own.
    """
    global _conn
    with _conn_lock:
        if _conn is not None and not _conn.closed:
            return _conn
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _conn = psycopg.connect(_dsn(), autocommit=True)
        with _conn.cursor() as cur:
            cur.execute(SCHEMA)
        print("[db] connected to postgres", flush=True)
        return _conn

def db_ready() -> bool:
    """Whether the optional parts (examples, feedback) are available."""
    return bool(DATABASE_URL) and psycopg is not None

def db_outage(e: Exception) -> None:
    """A database problem is never fatal here: the bridge answers without it."""
    global _db_warned
    if not _db_warned:
        _db_warned = True
        print(f"[db] unavailable, continuing without examples: {e}", flush=True)

def fetchall(sql: str, params: tuple = ()) -> list:
    with db().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()

def fetchone(sql: str, params: tuple = ()):
    with db().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


class GenReq(BaseModel):
    prompt: str
    temperature: Optional[float] = 0.7
    model: Optional[str] = ""

class FeedbackReq(BaseModel):
    script_id: int
    worked: bool
    notes: Optional[str] = ""

def get_examples(prompt: str, limit: int = 2):
    keywords = set(prompt.lower().split())
    rows = fetchall("SELECT id, prompt, code FROM scripts WHERE success = 1 ORDER BY id DESC LIMIT 50")
    scored = sorted(rows, key=lambda r: len(keywords & set(r[1].lower().split())), reverse=True)
    return [r for r in scored[:limit] if len(keywords & set(r[1].lower().split())) > 0]

def get_failures():
    """Notes from scripts marked broken, minus the page's own placeholder note.

    "did not work in the executor" is what the broken button sends with no detail, so
    it says nothing to the model while still costing prompt tokens.
    """
    rows = fetchall("SELECT fail_reason FROM scripts WHERE success = 0 AND fail_reason != '' ORDER BY id DESC LIMIT 5")
    return [r[0] for r in rows if r[0].strip().lower() != "did not work in the executor"]

def store_script(prompt: str, code: str) -> Optional[int]:
    """Save the script for later reuse; None when there is nowhere to save it."""
    try:
        return fetchone("INSERT INTO scripts (prompt, code) VALUES (%s, %s) RETURNING id", (prompt, code))[0]
    except DB_ERRORS as e:
        db_outage(e)
        return None


# Dense on purpose: the rules are sent with every request, so they are kept tight, and
# they stay constant with the examples/mistakes appended last.
RULES = """You are a senior Roblox Luau developer. Reply with the script only, no prose and no markdown fences.

- Write Luau, not Lua 5.1: task.wait, task.spawn, task.delay. Never wait/spawn/delay.
- Cache services at the top with game:GetService("..."); never index game.Players style.
- Guard what can be nil with :FindFirstChild, and give :WaitForChild a timeout.
- Wrap yielding calls and the risky body of the script in pcall, and check the result.
- Keep every RunService connection and created Instance in a local, and disconnect or
  destroy them when the feature is toggled off. Leave nothing leaking.
- Prefer Humanoid:MoveTo, CFrame, Raycast and TweenService over hacky workarounds.
- Client-side executor: loadstring, request and getgenv() are available.
- Continuous features get a keybind toggle via UserInputService.InputBegan, ignoring
  gameProcessedEvent, and must survive being toggled on and off.
- Ambiguous request: pick the most common Roblox interpretation and build it.
- Complete and runnable as-is: no stubs, no "rest of the code here", as short as the
  feature allows."""

# The optional parts of the prompt have a hard character budget rather than a per-example
# cap, so past feedback can never grow the prompt past what the request is worth.
EXAMPLE_CHARS = int(os.getenv("EXAMPLE_CHARS", "500"))

def build_system(fails: list, examples: list) -> str:
    system = RULES
    if fails:
        system += "\n\nAvoid:\n" + "\n".join(f"- {f[:120]}" for f in fails[:2])
    budget = EXAMPLE_CHARS
    for ex in examples:
        if budget <= 0:
            break
        head = ex[2][:budget]
        budget -= len(head)
        system += f"\n\n{ex[1]}\n{head}"
    return system

def strip_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        lines = code.split("\n")
        code = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return code.strip()

def chat_timeout() -> httpx.Timeout:
    """Generous read window: a hosted model answers in seconds, this is a backstop."""
    return httpx.Timeout(CHAT_TIMEOUT, connect=10.0)

def upstream_error(e: httpx.HTTPError) -> HTTPException:
    """Map an httpx failure onto a status the caller can act on."""
    if isinstance(e, httpx.TimeoutException):
        return HTTPException(504, f"{provider_label()} timed out after {CHAT_TIMEOUT:g}s")
    return HTTPException(502, f"cannot reach {QWEN_URL} ({e.__class__.__name__})")

def chat_body(model: str, messages: list, temperature: Optional[float], stream: bool) -> dict:
    """An OpenAI-shaped request for qwen-api, which is an OpenAI-shaped endpoint."""
    body = {
        "model": model or QWEN_MODEL,
        "messages": messages,
        "stream": stream,
        # Qwen's own switch: fast answers straight away, thinking returns its reasoning
        # as reasoning_content (which we do not put in the script).
        "thinking_mode": THINKING,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if MAX_TOKENS > 0:
        body["max_tokens"] = MAX_TOKENS
    return body

def stream_text(model: str, system: str, prompt: str, temperature: Optional[float]):
    """Stream a script, piece by piece, out of qwen-api's /chat/completions."""
    body = chat_body(model, [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}], temperature, True)
    with httpx.Client(timeout=chat_timeout(), follow_redirects=True) as c:
        with c.stream("POST", f"{QWEN_URL}/chat/completions", json=body, headers=qwen_headers()) as r:
            if r.status_code >= 400:
                detail = r.read().decode("utf-8", "replace")
                raise HTTPException(502, failure_reason(r.status_code, detail, model))
            if "event-stream" not in r.headers.get("content-type", ""):
                # Not a stream: either the proxy rejected the request with a 200, or it
                # ignored stream=true and answered in one piece. Reading the body tells
                # us which; reporting an empty answer would hide the reason.
                raw = r.read().decode("utf-8", "replace")
                text = message_text(raw)
                if not text:
                    raise HTTPException(502, failure_reason(200, raw, model))
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
                # reasoning_content is deliberately skipped: the answer is the script.
                piece = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                if piece:
                    yield piece

def run_job(job: Job) -> None:
    """Produce the script on a thread of its own: no reader, no lost work."""
    job.finish(status="running")
    try:
        for piece in stream_text(job.model, job.system, job.prompt, job.temperature):
            job.add(piece)
        code = strip_fences(job.text())
        if not code:
            job.finish(status="error", error="the model returned nothing; try again")
            return
        script_id = store_script(job.prompt, code)
        job.finish(status="done", code=code, script_id=script_id, saved=script_id is not None)
        note_error("")
        note = "saved" if script_id is not None else "no database, not saved"
        print(f"[job] {job.id} done in {job.report()['elapsed']:g}s, "
              f"{len(code)} chars ({note})", flush=True)
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


# --- the bridge ------------------------------------------------------------------------

def model_override(model: Optional[str]) -> str:
    """Let a caller pick from /v1/models, but only a sane id."""
    picked = (model or "").strip()
    if not picked:
        return QWEN_MODEL
    if len(picked) > 80 or any(c.isspace() or c in "\"'\\/" for c in picked):
        raise HTTPException(400, f"bad model id: {picked[:40]!r}")
    return picked

def upstream_failure(response: httpx.Response, model: str) -> HTTPException:
    detail = response.read().decode("utf-8", "replace")
    status = 502 if response.status_code in (401, 403, 404, 429) else 502
    return HTTPException(status, failure_reason(response.status_code, detail, model))

def relay_stream(body: dict):
    """Pass qwen-api's SSE through untouched.

    Nothing is rewritten on this path, which is what keeps the rest of qwen-api working
    through the bridge: reasoning_content, tool calls, web-search annotations and the
    hidden continuation metadata all survive, so follow-up turns continue the same
    upstream chat.
    """
    model = body.get("model") or QWEN_MODEL
    with httpx.Client(timeout=chat_timeout(), follow_redirects=True) as c:
        with c.stream("POST", f"{QWEN_URL}/chat/completions", json=body, headers=qwen_headers()) as r:
            if r.status_code >= 400:
                yield sse_error(failure_reason(r.status_code, r.read().decode("utf-8", "replace"), model))
                return
            if "event-stream" not in r.headers.get("content-type", ""):
                raw = r.read().decode("utf-8", "replace")
                text = message_text(raw)
                if not text:
                    yield sse_error(failure_reason(200, raw, model))
                    return
                yield sse({"id": "chatcmpl-bridge", "object": "chat.completion.chunk",
                           "model": model,
                           "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                        "finish_reason": None}]})
                yield "data: [DONE]\n\n"
                return
            for chunk in r.iter_bytes():
                yield chunk


@app.post("/v1/chat/completions")
def chat_completions(payload: dict = Body(...), _: None = Depends(require_key)):
    """The bridge itself: OpenAI-compatible, token injected, streaming preserved.

    A caller sends exactly what it would send to OpenAI -- only `model` is optional --
    and this service adds the Qwen credential. Anything the caller set (tools,
    web_search_options, reasoning_effort, thinking_mode, temperature, stream) is passed
    through as-is.
    """
    if not CONFIGURED:
        raise HTTPException(503, NOT_CONFIGURED)
    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise HTTPException(400, "messages must be a non-empty list")
    body = dict(payload)
    body["model"] = model_override(payload.get("model"))
    body.setdefault("thinking_mode", THINKING)
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
        raise upstream_failure(r, body["model"])
    return Response(r.content, media_type="application/json")


@app.get("/v1/models")
def models(_: None = Depends(require_key)):
    """The model ids this bridge can reach, plus the default when the list is unreadable."""
    ids = list_models() or [QWEN_MODEL]
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": mid, "object": "model", "created": now, "owned_by": "qwen"} for mid in ids
    ]}


# --- the Luau script flow, which is what the site and client.lua use -------------------

@app.post("/generate")
def generate(req: GenReq, _: None = Depends(require_key)):
    """Block until the script is ready -- this is the path `client.lua` uses.

    It runs the same job the streaming flow runs, so both go through the same bridge,
    and then waits for that job to finish.
    """
    job = start_job(req)
    job.wait(CHAT_TIMEOUT + 30)
    if job.status == "error":
        raise HTTPException(502, job.error)
    return {"id": job.script_id, "code": job.code, "model": job.model}

def frame(payload: dict) -> str:
    return json.dumps(payload) + "\n"

def job_frames(job: Job):
    """NDJSON for one reader: everything the job has so far, then each new piece.

    Every reader starts at zero, so a browser whose connection died just asks again and
    rebuilds the same output while the job carries on. The frames in between matter even
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
            status, error, code, script_id = job.status, job.error, job.code, job.script_id

        for piece in pieces:
            yield frame({"t": piece})
        if status == "error":
            # Sent with the script when it was generated but the job still failed.
            yield frame({"error": error, "code": code})
            return
        if status == "done":
            yield frame({"done": True, "id": script_id, "code": code})
            return
        if not pieces:
            yield frame({"beat": True, **report})

@app.post("/generate/stream")
def start_stream(req: GenReq, _: None = Depends(require_key)):
    """Start the generation and hand back its job id immediately.

    Nothing is generated on this request, so it cannot hang and be dropped: the waiting
    happens on the job's thread, and the page watches /generate/stream/{job} instead.
    """
    job = start_job(req)
    return {"job": job.id, "model": job.model, "timeout": CHAT_TIMEOUT}

@app.get("/generate/stream/{job_id}")
def watch_stream(job_id: str, _: None = Depends(require_key)):
    """Stream a job's progress. Calling it again after a drop is the whole point."""
    job = lookup(job_id)
    return StreamingResponse(
        job_frames(job),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

@app.post("/feedback")
def feedback(req: FeedbackReq, _: None = Depends(require_key)):
    if not db_ready():
        return {"status": "skipped", "reason": "no database configured"}
    try:
        fetchone(
            "UPDATE scripts SET success = %s, fail_reason = %s WHERE id = %s RETURNING id",
            (1 if req.worked else 0, "" if req.worked else req.notes, req.script_id),
        )
    except DB_ERRORS as e:
        db_outage(e)
        return {"status": "skipped", "reason": str(e)[:200]}
    return {"status": "ok"}

def start_job(req: GenReq) -> Job:
    """Gather what the model should know, then set the work going on its own thread."""
    if not CONFIGURED:
        raise HTTPException(503, NOT_CONFIGURED)
    fails: list = []
    examples: list = []
    if db_ready():
        try:
            fails, examples = get_failures(), get_examples(req.prompt)
        except DB_ERRORS as e:
            db_outage(e)
    model = model_override(req.model)
    job = Job(build_system(fails, examples), req.prompt, req.temperature,
              f"{model} via {provider_label()}", model)
    register(job)
    print(f"[job] {job.id} started on {model}: {req.prompt.strip()[:60]!r}", flush=True)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


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
        chip(state["database"], "postgres", "connected" if state["database"]
             else "not configured (feedback is off)"),
        chip(True, "model", state["model"]),
    ])
    try:
        page = INDEX.read_text(encoding="utf-8")
    except OSError:
        # The API stays usable if the static page was not copied into the image.
        return HTMLResponse("<h1>bahs</h1><p>web/index.html is missing; use /health and /docs.</p>")
    return HTMLResponse(
        page.replace("__CHIPS__", chips)
            .replace("__MODEL__", html.escape(state["model"]))
            .replace("__KEY_REQUIRED__", "true" if API_KEY else "false")
    )

async def snapshot() -> dict:
    # Always reports rather than raising, so the platform healthcheck only depends on the
    # API being up; database, token and model readiness come back in the body.
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
        "token_ok": state["ok"],
        "token_detail": state["detail"],
        "database": False,
        "last_error": last_error(),
        "api_key_required": bool(API_KEY),
    }
    if not CONFIGURED:
        body["status"] = "degraded"
        body["error"] = NOT_CONFIGURED
    if db_ready():
        try:
            fetchone("SELECT 1")
            body["database"] = True
        except DB_ERRORS as e:
            body["database_error"] = str(e)[:200]
    return body

@app.get("/health")
async def health():
    return await snapshot()
