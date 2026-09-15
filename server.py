from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path
import hmac, html, httpx, json, os, threading, time
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
INDEX = Path(__file__).parent / "web" / "index.html"
# Inference without a GPU is slow, so the defaults lean on repeated use: the weights
# stay resident between requests instead of reloading, and answers are length-capped.
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT", "600"))
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "30m")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "512"))
# A smaller context window means less prompt to process on every request, and the
# rules below plus a couple of examples fit comfortably inside this.
NUM_CTX = int(os.getenv("NUM_CTX", "2048"))
# Ollama serves one request per model, so a 5 minute warm-up on a starved container
# delays the first real request by 5 minutes. Off is the better trade there.
WARM_MODEL = os.getenv("WARM_MODEL", "on").strip().lower() not in ("0", "off", "false", "no")

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
    the service out permanently.
    """
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
    token instead of making the first user wait for it. On a container too small to
    run the model at a usable speed this just hogs the single slot, so WARM_MODEL=off
    skips it.
    """
    if not WARM_MODEL:
        print("[model] warm-up skipped (WARM_MODEL=off)", flush=True)
        return
    try:
        with httpx.Client(timeout=None, follow_redirects=True) as c:
            c.post(f"{OLLAMA}/api/chat", json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "hi"}],
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
    return [r[0] for r in fetchall("SELECT fail_reason FROM scripts WHERE success = 0 AND fail_reason != '' ORDER BY id DESC LIMIT 5")]

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

def build_system(fails: list, examples: list) -> str:
    system = RULES
    if fails:
        system += "\n\nWhat went wrong on earlier attempts (do not repeat it):\n" + "\n".join(f"- {f}" for f in fails[:3])
    if examples:
        system += "\n\nScripts that already worked for this user:\n"
        for ex in examples:
            # Truncated hard: prompt tokens are seconds on CPU inference.
            system += f"\nRequest: {ex[1]}\nCode:\n{ex[2][:800]}\n"
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

def ollama_failure(e: httpx.HTTPError) -> HTTPException:
    """Map an httpx failure onto a status the caller can act on.

    httpx timeouts often carry no message at all, so that case is named explicitly
    rather than reported as an empty reason.
    """
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        detail = (e.response.text or str(e) or repr(e))[:300]
        return HTTPException(503 if code == 404 else 502, f"ollama ({MODEL}): {detail}")
    if isinstance(e, httpx.TimeoutException):
        return HTTPException(504, f"ollama ({MODEL}) timed out after {CHAT_TIMEOUT:g}s at {OLLAMA}")
    return HTTPException(502, f"cannot reach ollama at {OLLAMA}: {e.__class__.__name__}")

def chat_timeout() -> httpx.Timeout:
    """Generous read window: the first token waits on a model load, the rest on CPU."""
    return httpx.Timeout(CHAT_TIMEOUT, connect=10.0)

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
        chip(state["ollama"], "ollama", "reachable" if state["ollama"]
             else str(state.get("error", "unreachable"))),
        chip(state["model_ready"], "model", f"{MODEL} ready" if state["model_ready"]
             else f"{MODEL} pulling"),
    ])
    try:
        page = INDEX.read_text(encoding="utf-8")
    except OSError:
        # The API stays usable if the static page was not copied into the image.
        return HTMLResponse("<h1>bahs</h1><p>web/index.html is missing; use /health and /docs.</p>")
    return HTMLResponse(
        page.replace("__CHIPS__", chips)
            .replace("__MODEL__", html.escape(MODEL))
            .replace("__KEY_REQUIRED__", "true" if API_KEY else "false")
    )

@app.post("/generate")
async def generate(req: GenReq, _: None = Depends(require_key)):
    try:
        fails = get_failures()
        examples = get_examples(req.prompt)
    except (psycopg.Error, RuntimeError) as e:
        raise db_unavailable(e)

    system = build_system(fails, examples)
    claim_slot()
    try:
        async with httpx.AsyncClient(timeout=chat_timeout(), follow_redirects=True) as c:
            r = await c.post(f"{OLLAMA}/api/chat", json=chat_payload(system, req.prompt, req.temperature))
            r.raise_for_status()
            code = strip_fences(r.json()["message"]["content"])
    except httpx.HTTPError as e:
        raise ollama_failure(e)
    finally:
        release_slot()
    return {"id": store_script(req.prompt, code), "code": code}

def frame(payload: dict) -> str:
    return json.dumps(payload) + "\n"

@app.post("/generate/stream")
async def generate_stream(req: GenReq, _: None = Depends(require_key)):
    """Same as /generate, but streams the tokens as NDJSON so the page shows progress.

    Waiting 100s for a whole answer is what made this feel broken; sending each piece
    as it is produced means the first line shows up in seconds.
    """
    try:
        fails = get_failures()
        examples = get_examples(req.prompt)
    except (psycopg.Error, RuntimeError) as e:
        raise db_unavailable(e)
    payload = chat_payload(build_system(fails, examples), req.prompt, req.temperature)
    payload["stream"] = True
    claim_slot()

    async def body():
        chunks = []
        try:
            # Say what the wait is for: a cold model load and a stuck request look
            # identical from the browser otherwise.
            loaded = await model_loaded()
            yield frame({"status": "model ready" if loaded else f"loading {MODEL} into RAM"})

            async with httpx.AsyncClient(timeout=chat_timeout(), follow_redirects=True) as c:
                async with c.stream("POST", f"{OLLAMA}/api/chat", json=payload) as r:
                    if r.status_code >= 400:
                        text = (await r.aread()).decode("utf-8", "replace").strip()
                        # 404 here almost always means the model is still being pulled.
                        yield frame({"error": f"ollama ({MODEL}) {r.status_code}: {text or 'no detail'}"})
                        return
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except ValueError:
                            continue
                        piece = (data.get("message") or {}).get("content") or ""
                        if piece:
                            chunks.append(piece)
                            yield frame({"t": piece})
                        if data.get("done"):
                            break
        except httpx.TimeoutException:
            yield frame({"error": f"ollama ({MODEL}) timed out after {CHAT_TIMEOUT:g}s; it may still be loading"})
            return
        except httpx.HTTPError as e:
            yield frame({"error": f"cannot reach ollama at {OLLAMA}: {e.__class__.__name__}"})
            return
        finally:
            release_slot()

        code = strip_fences("".join(chunks))
        try:
            script_id = store_script(req.prompt, code)
        except Exception as e:
            # Any failure here still gets a frame: a stream that just stops would leave
            # the page waiting on a script that already arrived.
            detail = e.detail if isinstance(e, HTTPException) else f"{e.__class__.__name__}: {e}"
            yield frame({"error": f"generated, but not saved: {detail}"})
            return
        # The final frame carries the cleaned code, since the stream included the fences.
        yield frame({"done": True, "id": script_id, "code": code})

    return StreamingResponse(
        body(),
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
    body = {"status": "ok", "database": False, "ollama": False, "model": MODEL,
            "model_ready": False, "api_key_required": bool(API_KEY)}
    try:
        fetchone("SELECT 1")
        body["database"] = True
    except (psycopg.Error, RuntimeError) as e:
        body["database_error"] = str(e)
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
