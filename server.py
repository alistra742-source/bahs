from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio, hmac, html, httpx, json, os, threading, time, uuid
import psycopg

# Railway's Postgres plugin injects DATABASE_URL; POSTGRES_URL is the older name.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
# When API_KEY is set on the service, /generate and /feedback require it back as the
# X-API-Key header (or an Authorization: Bearer token). Unset means open, so local runs
# and a fresh deploy work before the variable exists.
API_KEY = os.getenv("API_KEY", "")
# The model runs on Hugging Face's Inference Providers router: one OpenAI-compatible
# /chat/completions endpoint in front of hosted models. This service holds no weights,
# so there is nothing to pull, load, warm or queue behind — that was the whole reason a
# script used to take minutes. INFERENCE_URL moves it to another compatible provider.
HF_ROUTER = "https://router.huggingface.co/v1"

def inference_key() -> str:
    """The Hugging Face token, under whichever name it was put in the variables."""
    for name in ("HF_API", "INFERENCE_KEY", "HF_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""

INFERENCE_KEY = inference_key()
INFERENCE_URL = os.getenv("INFERENCE_URL", "").strip().rstrip("/") or HF_ROUTER
INFERENCE_MODEL = os.getenv("INFERENCE_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct").strip()
# Every request needs a token, so without one the API says so plainly instead of
# sending an empty bearer to Hugging Face and reporting its 401 back to the user.
CONFIGURED = bool(INFERENCE_KEY)
NOT_CONFIGURED = ("inference is not configured: set HF_API on this service to a Hugging "
                  "Face token with the Inference Providers permission")

def provider_label() -> str:
    """Short name of whoever is answering, for the page's chips."""
    return INFERENCE_URL.split("//", 1)[-1].split("/", 1)[0]

INDEX = Path(__file__).parent / "web" / "index.html"
# How long one request may take end to end. A hosted model answers in seconds, so this
# is only a backstop for a provider that hangs.
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT", "600"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "512"))
# A generation is a server-side job, so a reader can go quiet without the answer being
# lost. That quiet is exactly what proxies and sleeping phones drop, so the stream is
# punctuated with a heartbeat this often.
HEARTBEAT = float(os.getenv("HEARTBEAT", "5"))
# How long a finished job stays readable, so a browser that comes back late can still
# collect the answer instead of finding nothing.
JOB_TTL = float(os.getenv("JOB_TTL", "3600"))

class Job:
    """One generation, owned by a background thread rather than by the caller.

    A phone that gave up on an answer used to take the whole generation with it: the
    request was the only thing driving the model, so nothing was left to read. A job
    runs to completion on its own thread, keeps every piece it has produced, and any
    number of readers can attach to it — including one that comes back after the
    connection dropped, which replays the output from the start and follows along.
    """

    def __init__(self, system: str, prompt: str, temperature: Optional[float], note: str):
        self.id = uuid.uuid4().hex[:12]
        self.system = system
        self.prompt = prompt
        self.temperature = temperature
        self.note = note
        self.pieces: list = []
        self.code = ""
        self.script_id = None
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
                    raise HTTPException(504, f"timed out after {timeout:g}s waiting on {INFERENCE_URL}")
                self.cond.wait(timeout=remaining)

    def report(self) -> dict:
        """Where the job stands; what the page shows next to its running timer."""
        return {
            "status": self.status,
            "note": self.note,
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
        print(f"[model] inference at {INFERENCE_URL} using {INFERENCE_MODEL}", flush=True)
    else:
        print(f"[model] {NOT_CONFIGURED}", flush=True)
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def require_key(x_api_key: Optional[str] = Header(None), authorization: Optional[str] = Header(None)) -> None:
    """Gate the model endpoints when the service defines API_KEY."""
    if not API_KEY:
        return
    supplied = x_api_key or ""
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(401, "missing or invalid API key (send it as the X-API-Key header)")

SCHEMA = """CREATE TABLE IF NOT EXISTS scripts (
    id SERIAL PRIMARY KEY,
    prompt TEXT,
    code TEXT,
    success INTEGER DEFAULT -1,
    fail_reason TEXT DEFAULT ''
)"""

_conn: Optional["psycopg.Connection"] = None
_conn_lock = threading.Lock()

def _dsn() -> str:
    # libpq understands the postgres:// scheme, psycopg is happier with postgresql://.
    if DATABASE_URL.startswith("postgres://"):
        return "postgresql://" + DATABASE_URL[len("postgres://"):]
    return DATABASE_URL

def db() -> "psycopg.Connection":
    """Open the Postgres connection on first use, and reconnect if it dropped.

    Connecting lazily keeps the API up while Postgres is still booting and makes
    the container stateless, so the service needs no volume of its own.
    """
    global _conn
    with _conn_lock:
        if _conn is not None and not _conn.closed:
            return _conn
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set (add the Postgres plugin)")
        _conn = psycopg.connect(_dsn(), autocommit=True)
        with _conn.cursor() as cur:
            cur.execute(SCHEMA)
        print("[db] connected to postgres", flush=True)
        return _conn

def db_unavailable(error: Exception) -> HTTPException:
    return HTTPException(503, f"database unavailable: {error}")

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

# Dense on purpose: the rules are sent with every request, so they are kept tight, and
# the rules stay constant with the examples/mistakes appended last.
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

# Prompt tokens cost the same CPU time as generated ones, so the optional parts have a
# hard budget rather than a per-example cap: two examples at 800 characters each was a
# second copy of the rules being evaluated on every single request.
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

def store_script(prompt: str, code: str) -> int:
    try:
        row = fetchone("INSERT INTO scripts (prompt, code) VALUES (%s, %s) RETURNING id", (prompt, code))
    except (psycopg.Error, RuntimeError) as e:
        raise db_unavailable(e)
    return row[0]

def inference_failure(e: httpx.HTTPError) -> HTTPException:
    """Map an httpx failure onto a status the caller can act on, whoever served it.

    httpx timeouts often carry no message at all, so that case is named explicitly
    rather than reported as an empty reason.
    """
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        detail = (e.response.text or str(e) or repr(e))[:300]
        return HTTPException(503 if code == 404 else 502, f"{INFERENCE_MODEL}: {detail}")
    if isinstance(e, httpx.TimeoutException):
        return HTTPException(504, f"{INFERENCE_MODEL} timed out after {CHAT_TIMEOUT:g}s at {INFERENCE_URL}")
    return HTTPException(502, f"cannot reach {INFERENCE_URL}: {e.__class__.__name__}")

def chat_timeout() -> httpx.Timeout:
    """Generous read window: a hosted model answers in seconds, this is a backstop."""
    return httpx.Timeout(CHAT_TIMEOUT, connect=10.0)

def hosted_stream(system: str, prompt: str, temperature: Optional[float]):
    """Stream a Hugging Face (OpenAI-compatible) /chat/completions answer piece by piece."""
    body = {
        "model": INFERENCE_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {INFERENCE_KEY}", "Content-Type": "application/json"}
    with httpx.Client(timeout=chat_timeout(), follow_redirects=True) as c:
        with c.stream("POST", f"{INFERENCE_URL}/chat/completions", json=body, headers=headers) as r:
            if r.status_code >= 400:
                detail = r.read().decode("utf-8", "replace").strip()
                raise HTTPException(429 if r.status_code == 429 else 502,
                                    f"inference ({INFERENCE_MODEL}) {r.status_code}: {detail or 'no detail'}")
            if "event-stream" not in r.headers.get("content-type", ""):
                # Not a stream: either the provider rejected the request with a 200, or it
                # ignored stream=true and answered in one piece. Reading the body tells us
                # which; reporting an empty answer would hide the reason.
                yield from single_response(r.read().decode("utf-8", "replace"))
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
                piece = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                if piece:
                    yield piece

def single_response(body: str):
    """Handle a non-streamed answer: use its text, or fail with the provider's reason."""
    detail = (body or "").strip()
    try:
        payload = json.loads(detail)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        choices = payload.get("choices") or []
        text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
        if text:
            print("[model] provider answered in one piece instead of streaming", flush=True)
            yield text
            return
    raise HTTPException(502, f"inference ({INFERENCE_MODEL}) sent no stream: {detail[:300] or 'empty body'}")

def run_job(job: Job) -> None:
    """Produce the script on a thread of its own: no reader, no lost work."""
    job.finish(status="running")
    try:
        for piece in hosted_stream(job.system, job.prompt, job.temperature):
            job.add(piece)
        code = strip_fences(job.text())
        if not code:
            job.finish(status="error", error="the model returned nothing; try again")
            return
        try:
            script_id = store_script(job.prompt, code)
        except HTTPException as e:
            # The script did arrive, so it is still shown even though it was not stored.
            job.finish(status="error", error=f"generated, but not saved: {e.detail}", code=code)
            return
        job.finish(status="done", code=code, script_id=script_id)
        note_error("")
        print(f"[job] {job.id} done in {job.report()['elapsed']:g}s, {len(code)} chars", flush=True)
    except HTTPException as e:
        # Printed as well as sent: the page shows it once, the log keeps it.
        print(f"[job] {job.id} failed: {e.detail}", flush=True)
        note_error(str(e.detail))
        job.finish(status="error", error=str(e.detail))
    except httpx.HTTPError as e:
        detail = inference_failure(e).detail
        print(f"[job] {job.id} failed: {detail}", flush=True)
        note_error(detail)
        job.finish(status="error", error=detail)
    except Exception as e:  # a bug here must never leave a reader waiting forever
        print(f"[job] {job.id} crashed: {e.__class__.__name__}: {e}", flush=True)
        note_error(f"{e.__class__.__name__}: {e}")
        job.finish(status="error", error=f"{e.__class__.__name__}: {e}")

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
        chip(state["database"], "postgres", "connected" if state["database"]
             else str(state.get("database_error", "not configured"))),
        chip(state["inference"], "inference",
             provider_label() if state["inference"] else "HF_API is not set"),
        chip(state["model_ready"], "model", f"{INFERENCE_MODEL} ready" if state["model_ready"]
             else "waiting for HF_API"),
    ])
    try:
        page = INDEX.read_text(encoding="utf-8")
    except OSError:
        # The API stays usable if the static page was not copied into the image.
        return HTMLResponse("<h1>bahs</h1><p>web/index.html is missing; use /health and /docs.</p>")
    return HTMLResponse(
        page.replace("__CHIPS__", chips)
            .replace("__MODEL__", html.escape(INFERENCE_MODEL))
            .replace("__KEY_REQUIRED__", "true" if API_KEY else "false")
    )

def start_job(prompt: str, temperature: Optional[float], note: str) -> Job:
    """Look up what the model should know, then set the work going on its own thread."""
    if not CONFIGURED:
        raise HTTPException(503, NOT_CONFIGURED)
    try:
        fails = get_failures()
        examples = get_examples(prompt)
    except (psycopg.Error, RuntimeError) as e:
        raise db_unavailable(e)
    job = Job(build_system(fails, examples), prompt, temperature, note)
    register(job)
    print(f"[job] {job.id} started via {INFERENCE_MODEL}: {prompt.strip()[:60]!r}", flush=True)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job

def wait_note() -> str:
    """What the wait is for: which model is answering, and where."""
    return f"{INFERENCE_MODEL} via {provider_label()}"

@app.post("/generate")
async def generate(req: GenReq, _: None = Depends(require_key)):
    """Block until the script is ready — this is the path `client.lua` uses.

    It runs the same job the streaming flow runs, so both go through whichever provider
    is configured, and then waits for that job to finish.
    """
    job = start_job(req.prompt, req.temperature, wait_note())
    await asyncio.to_thread(job.wait, CHAT_TIMEOUT + 30)
    if job.status == "error":
        raise HTTPException(502, job.error)
    return {"id": job.script_id, "code": job.code}

def frame(payload: dict) -> str:
    return json.dumps(payload) + "\n"

def job_frames(job: Job):
    """NDJSON for one reader: everything the job has so far, then each new piece.

    Every reader starts at zero, so a browser whose connection died just asks again and
    rebuilds the same output while the job carries on. The frames in between matter even
    when there is nothing to report: a stream that goes silent for the minutes a cold
    model takes is what a proxy or a sleeping phone drops, so the wait is punctuated
    with heartbeats.
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
            # Sent with the script when it was generated but could not be stored.
            yield frame({"error": error, "code": code})
            return
        if status == "done":
            yield frame({"done": True, "id": script_id, "code": code})
            return
        if not pieces:
            yield frame({"beat": True, **report})

@app.post("/generate/stream")
async def start_stream(req: GenReq, _: None = Depends(require_key)):
    """Start the generation and hand back its job id immediately.

    Nothing is generated on this request, so it cannot hang and be dropped: the waiting
    happens on the job's thread, and the page watches /generate/stream/{job} instead.
    """
    job = start_job(req.prompt, req.temperature, wait_note())
    return {"job": job.id, "model": INFERENCE_MODEL, "timeout": CHAT_TIMEOUT}

@app.get("/generate/stream/{job_id}")
async def watch_stream(job_id: str, _: None = Depends(require_key)):
    """Stream a job's progress. Calling it again after a drop is the whole point."""
    job = lookup(job_id)
    return StreamingResponse(
        job_frames(job),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

@app.post("/feedback")
async def feedback(req: FeedbackReq, _: None = Depends(require_key)):
    try:
        fetchone(
            "UPDATE scripts SET success = %s, fail_reason = %s WHERE id = %s RETURNING id",
            (1 if req.worked else 0, "" if req.worked else req.notes, req.script_id),
        )
    except (psycopg.Error, RuntimeError) as e:
        raise db_unavailable(e)
    return {"status": "ok"}

async def snapshot() -> dict:
    # Always reports rather than raising, so the platform healthcheck only depends on
    # the API being up; database and inference readiness come back in the body.
    body = {"status": "ok", "database": False, "model": INFERENCE_MODEL,
            "inference": CONFIGURED, "provider_label": provider_label(), "endpoint": INFERENCE_URL,
            "last_error": last_error(), "model_ready": CONFIGURED, "api_key_required": bool(API_KEY)}
    try:
        fetchone("SELECT 1")
        body["database"] = True
    except (psycopg.Error, RuntimeError) as e:
        body["database_error"] = str(e)
    if not CONFIGURED:
        body["status"] = "degraded"
        body["error"] = NOT_CONFIGURED
    return body

@app.get("/health")
async def health():
    return await snapshot()
