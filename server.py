from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio, hmac, html, httpx, json, os, threading, time, uuid
import psycopg

def normalise_ollama_url(raw: str) -> str:
    """Tidy an OLLAMA_URL: drop trailing slashes and give a bare host a scheme.

    A trailing slash plus our paths produced "//api/tags", which Railway's edge
    answered with a 307 back to "/api/tags" instead of proxying it, and a bare host
    (no scheme) is not a URL httpx will touch at all.
    """
    url = (raw or "").strip().rstrip("/")
    if url and "://" not in url:
        local = url.startswith(("localhost", "127.0.0.1", "0.0.0.0")) or ".railway.internal" in url
        url = ("http://" if local else "https://") + url
    return url or "http://localhost:11434"

OLLAMA = normalise_ollama_url(os.getenv("OLLAMA_URL", "http://localhost:11434"))
MODEL = os.getenv("MODEL", "qwen2.5-coder:3b")
# Railway's Postgres plugin injects DATABASE_URL; POSTGRES_URL is the older name.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
# When API_KEY is set on the service, /generate and /feedback require it back as the
# X-API-Key header (or an Authorization: Bearer token). Unset means open, so local runs
# and a fresh deploy work before the variable exists.
API_KEY = os.getenv("API_KEY", "")
# Hosted inference: the way out of CPU-only inference. A 3B model on a container short
# of RAM spends ~20 minutes evaluating a prompt before it writes a character, and no
# API-side change fixes that. Set INFERENCE_URL and INFERENCE_KEY to any
# OpenAI-compatible /chat/completions endpoint (SambaNova, Groq, OpenRouter, OpenAI) and
# jobs stream from there instead — the Ollama service is then not touched at all.
# Unset, nothing changes: Ollama over the private network stays the default.
INFERENCE_URL = os.getenv("INFERENCE_URL", "").strip().rstrip("/")
INFERENCE_KEY = os.getenv("INFERENCE_KEY", "").strip()
INFERENCE_MODEL = os.getenv("INFERENCE_MODEL", "DeepSeek-V3.1").strip()
HOSTED = bool(INFERENCE_URL and INFERENCE_KEY)

def active_model() -> str:
    """The model that answers right now: the hosted one, or the local Ollama."""
    return INFERENCE_MODEL if HOSTED else MODEL

def endpoint() -> str:
    """Where inference actually happens, for error messages."""
    return INFERENCE_URL if HOSTED else OLLAMA

INDEX = Path(__file__).parent / "web" / "index.html"
# Inference without a GPU is slow, so the defaults lean on repeated use: the weights
# stay resident between requests instead of reloading, and answers are length-capped.
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT", "600"))
# A day, not 30 minutes: unloading the weights costs a ~2 GB reload that on a slow
# container is minutes, and this box runs one model for one user. Ollama still evicts
# the model by itself if it needs the memory.
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "24h")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "512"))
# A smaller context window means less prompt to process on every request, and the
# rules below plus a couple of examples fit comfortably inside this.
NUM_CTX = int(os.getenv("NUM_CTX", "2048"))
# Ollama serves one request per model, so a 5 minute warm-up on a starved container
# delays the first real request by 5 minutes. Off is the better trade there.
WARM_MODEL = os.getenv("WARM_MODEL", "on").strip().lower() not in ("0", "off", "false", "no")
# A generation is a server-side job, so a reader can go quiet for minutes without the
# answer being lost. That quiet is exactly what proxies and sleeping phones drop, so
# the stream is punctuated with a heartbeat this often.
HEARTBEAT = float(os.getenv("HEARTBEAT", "5"))
# How long a finished job stays readable, so a browser that comes back late can still
# collect the answer instead of finding nothing.
JOB_TTL = float(os.getenv("JOB_TTL", "3600"))

def ollama_models() -> list:
    """Model names Ollama currently holds, or [] when it cannot be reached."""
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as c:
            r = c.get(f"{OLLAMA}/api/tags")
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", [])]
    except httpx.HTTPError:
        return []

_busy_until = 0.0
_busy_lock = threading.Lock()

def claim_slot() -> None:
    """Refuse a second generation instead of queueing it behind the first.

    Ollama serves one request per model, so an extra request merely waits — which looks
    exactly like a hang. The claim expires on its own so a dropped stream can never lock
    the service out permanently. A hosted endpoint answers in parallel, so nothing is
    claimed there.
    """
    if HOSTED:
        return
    global _busy_until
    with _busy_lock:
        now = time.time()
        if now < _busy_until:
            raise HTTPException(409, "already generating a script; wait for that one to finish")
        _busy_until = now + CHAT_TIMEOUT + 30

def release_slot() -> None:
    global _busy_until
    with _busy_lock:
        _busy_until = 0.0

class Job:
    """One generation, owned by a background thread rather than by the caller.

    A phone that gave up on a slow answer used to take the whole generation with it:
    the request was the only thing driving Ollama, so nothing was left to read. A job
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
                    raise HTTPException(504, f"timed out after {timeout:g}s waiting on {endpoint()}")
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

async def model_loaded() -> bool:
    """Whether Ollama currently holds MODEL in RAM (as opposed to loading it)."""
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as c:
            r = await c.get(f"{OLLAMA}/api/ps")
            r.raise_for_status()
            return MODEL in [m.get("name") or m.get("model") for m in r.json().get("models", [])]
    except httpx.HTTPError:
        return False

def warm_model() -> None:
    """Load the weights once so the first real request is not the slow one.

    Loading a 3B model is the slowest part of a cold request, so ask for a single
    token instead of making the first user wait for it. The prompt is the real system
    prompt, so the same prefix lands in Ollama's prompt cache: the first genuine
    request after a restart then only has to evaluate its own few words rather than
    the rules again. On a container too small to run the model at a usable speed this
    just hogs the single slot, so WARM_MODEL=off skips it.
    """
    if not WARM_MODEL:
        print("[model] warm-up skipped (WARM_MODEL=off)", flush=True)
        return
    try:
        with httpx.Client(timeout=None, follow_redirects=True) as c:
            c.post(f"{OLLAMA}/api/chat", json={
                "model": MODEL,
                "messages": [{"role": "system", "content": RULES},
                             {"role": "user", "content": "hi"}],
                "stream": False,
                "keep_alive": KEEP_ALIVE,
                # Same context size as real requests, so the cache is ready for them.
                "options": {"num_predict": 1, "num_ctx": NUM_CTX},
            })
        print(f"[model] {MODEL} warmed and held for {KEEP_ALIVE}", flush=True)
    except httpx.HTTPError as e:
        print(f"[model] warm-up skipped ({e.__class__.__name__})", flush=True)

def ensure_model(attempts: int = 40, delay: float = 15.0) -> None:
    """Pull MODEL into the Ollama service while it is missing.

    The Ollama service is the stock `ollama/ollama` image with a volume attached, so
    nothing pulls the model there. The API is what knows the model name, so it pulls
    it here; the weights then live in the Ollama service's volume.
    """
    for attempt in range(1, attempts + 1):
        if MODEL in ollama_models():
            print(f"[model] {MODEL} is in the ollama volume", flush=True)
            warm_model()
            return
        try:
            print(f"[model] pulling {MODEL} (attempt {attempt})", flush=True)
            with httpx.Client(timeout=None, follow_redirects=True) as c:
                with c.stream("POST", f"{OLLAMA}/api/pull", json={"model": MODEL}) as r:
                    r.raise_for_status()
                    for _ in r.iter_lines():
                        pass
            print(f"[model] {MODEL} pulled", flush=True)
            warm_model()
            return
        except httpx.HTTPStatusError as e:
            # 4xx means Ollama refused it (unknown tag, for example): retrying cannot help.
            print(f"[model] ollama refused {MODEL}: {e}", flush=True)
            return
        except httpx.HTTPError as e:
            print(f"[model] ollama not ready yet ({e}); retrying in {delay:g}s", flush=True)
            time.sleep(delay)
    print(f"[model] gave up on {MODEL}; /generate returns 503 until it is present", flush=True)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Nothing to pull or warm when a hosted endpoint does the work.
    if not HOSTED:
        # On a thread so the API answers /health and / while the weights download.
        threading.Thread(target=ensure_model, daemon=True).start()
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

# Dense on purpose: prompt tokens cost as much CPU time as generated ones. The rules
# stay constant and the examples/mistakes are appended last, so Ollama's prompt cache
# can reuse the prefix between requests.
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

def chat_payload(system: str, prompt: str, temperature: Optional[float]) -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "temperature": temperature,
            "num_predict": MAX_TOKENS,
            "num_ctx": NUM_CTX,
        },
    }

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
    name, where = active_model(), endpoint()
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        detail = (e.response.text or str(e) or repr(e))[:300]
        return HTTPException(503 if code == 404 else 502, f"{name}: {detail}")
    if isinstance(e, httpx.TimeoutException):
        return HTTPException(504, f"{name} timed out after {CHAT_TIMEOUT:g}s at {where}")
    return HTTPException(502, f"cannot reach {where}: {e.__class__.__name__}")

def chat_timeout() -> httpx.Timeout:
    """Generous read window: the first token waits on a model load, the rest on CPU."""
    return httpx.Timeout(CHAT_TIMEOUT, connect=10.0)

def ollama_stream(payload: dict):
    """Yield Ollama's answer a piece at a time, raising what the caller can act on."""
    with httpx.Client(timeout=chat_timeout(), follow_redirects=True) as c:
        with c.stream("POST", f"{OLLAMA}/api/chat", json=payload) as r:
            if r.status_code >= 400:
                detail = r.read().decode("utf-8", "replace").strip()
                # 404 here almost always means the model is still being pulled.
                raise HTTPException(503 if r.status_code == 404 else 502,
                                    f"ollama ({MODEL}) {r.status_code}: {detail or 'no detail'}")
            for line in r.iter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get("error"):
                    raise HTTPException(502, f"ollama ({MODEL}): {data['error']}")
                piece = (data.get("message") or {}).get("content") or ""
                if piece:
                    yield piece
                if data.get("done"):
                    return

def hosted_stream(system: str, prompt: str, temperature: Optional[float]):
    """Stream from an OpenAI-compatible /chat/completions endpoint.

    Same shape as `ollama_stream`, so the job machinery does not care which provider is
    behind it; the frames are the same NDJSON either way.
    """
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

def answer_stream(system: str, prompt: str, temperature: Optional[float]):
    """Yield the answer in pieces, from the hosted endpoint when one is configured."""
    if HOSTED:
        return hosted_stream(system, prompt, temperature)
    payload = chat_payload(system, prompt, temperature)
    payload["stream"] = True
    return ollama_stream(payload)

def run_job(job: Job) -> None:
    """Produce the script on a thread of its own: no reader, no lost work."""
    job.finish(status="running")
    try:
        for piece in answer_stream(job.system, job.prompt, job.temperature):
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
    except HTTPException as e:
        job.finish(status="error", error=str(e.detail))
    except httpx.HTTPError as e:
        job.finish(status="error", error=inference_failure(e).detail)
    except Exception as e:  # a bug here must never leave a reader waiting forever
        job.finish(status="error", error=f"{e.__class__.__name__}: {e}")
    finally:
        release_slot()

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
        chip(state["ollama"], "ollama",
             "not used -- hosted inference" if HOSTED
             else "reachable" if state["ollama"] else str(state.get("error", "unreachable"))),
        chip(state["model_ready"], "model", f"{active_model()} ready" if state["model_ready"]
             else f"{active_model()} pulling"),
    ])
    try:
        page = INDEX.read_text(encoding="utf-8")
    except OSError:
        # The API stays usable if the static page was not copied into the image.
        return HTMLResponse("<h1>bahs</h1><p>web/index.html is missing; use /health and /docs.</p>")
    return HTMLResponse(
        page.replace("__CHIPS__", chips)
            .replace("__MODEL__", html.escape(active_model()))
            .replace("__KEY_REQUIRED__", "true" if API_KEY else "false")
    )

def start_job(prompt: str, temperature: Optional[float], note: str) -> Job:
    """Look up what the model should know, then set the work going on its own thread."""
    try:
        fails = get_failures()
        examples = get_examples(prompt)
    except (psycopg.Error, RuntimeError) as e:
        raise db_unavailable(e)
    claim_slot()
    job = Job(build_system(fails, examples), prompt, temperature, note)
    register(job)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job

async def wait_note() -> str:
    """What the wait is for: a cold model load and a stuck request look identical otherwise."""
    if HOSTED:
        return f"{INFERENCE_MODEL} via hosted inference"
    return "model ready" if await model_loaded() else f"loading {MODEL} into RAM"

@app.post("/generate")
async def generate(req: GenReq, _: None = Depends(require_key)):
    """Block until the script is ready — this is the path `client.lua` uses.

    It runs the same job the streaming flow runs, so both go through whichever provider
    is configured, and then waits for that job to finish.
    """
    job = start_job(req.prompt, req.temperature, await wait_note())
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
    job = start_job(req.prompt, req.temperature, await wait_note())
    return {"job": job.id, "model": active_model(), "timeout": CHAT_TIMEOUT}

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
    # Always reports rather than raising, so the platform healthcheck only depends
    # on the API being up; database and ollama readiness come back in the body.
    body = {"status": "ok", "database": False, "ollama": False, "model": active_model(),
            "provider": "hosted" if HOSTED else "ollama",
            "model_ready": False, "api_key_required": bool(API_KEY)}
    try:
        fetchone("SELECT 1")
        body["database"] = True
    except (psycopg.Error, RuntimeError) as e:
        body["database_error"] = str(e)
    if HOSTED:
        # Nothing local to pull or hold: the configured endpoint is the whole story.
        body["ollama"] = True
        body["model_ready"] = True
        return body
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as c:
            r = await c.get(f"{OLLAMA}/api/tags")
            r.raise_for_status()
            models = [m.get("name") for m in r.json().get("models", [])]
            body["ollama"] = True
            body["models"] = models
            body["model_ready"] = MODEL in models
    except httpx.HTTPError as e:
        body["status"] = "degraded"
        body["error"] = str(e)
    return body

@app.get("/health")
async def health():
    return await snapshot()
