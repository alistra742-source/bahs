from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path
import hmac, html, httpx, os, threading, time
import psycopg

OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("MODEL", "qwen2.5-coder:3b")
# Railway's Postgres plugin injects DATABASE_URL; POSTGRES_URL is the older name.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
# When API_KEY is set on the service, /generate and /feedback require it back as the
# X-API-Key header (or an Authorization: Bearer token). Unset means open, so local runs
# and a fresh deploy work before the variable exists.
API_KEY = os.getenv("API_KEY", "")
INDEX = Path(__file__).parent / "web" / "index.html"

def ollama_models() -> list:
    """Model names Ollama currently holds, or [] when it cannot be reached."""
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{OLLAMA}/api/tags")
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", [])]
    except httpx.HTTPError:
        return []

def ensure_model(attempts: int = 40, delay: float = 15.0) -> None:
    """Pull MODEL into the Ollama service while it is missing.

    The Ollama service is the stock `ollama/ollama` image with a volume attached, so
    nothing pulls the model there. The API is what knows the model name, so it pulls
    it here; the weights then live in the Ollama service's volume.
    """
    for attempt in range(1, attempts + 1):
        if MODEL in ollama_models():
            print(f"[model] {MODEL} is in the ollama volume", flush=True)
            return
        try:
            print(f"[model] pulling {MODEL} (attempt {attempt})", flush=True)
            with httpx.Client(timeout=None) as c:
                with c.stream("POST", f"{OLLAMA}/api/pull", json={"model": MODEL}) as r:
                    r.raise_for_status()
                    for _ in r.iter_lines():
                        pass
            print(f"[model] {MODEL} pulled", flush=True)
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

def get_examples(prompt: str, limit: int = 3):
    keywords = set(prompt.lower().split())
    rows = fetchall("SELECT id, prompt, code FROM scripts WHERE success = 1 ORDER BY id DESC LIMIT 50")
    scored = sorted(rows, key=lambda r: len(keywords & set(r[1].lower().split())), reverse=True)
    return [r for r in scored[:limit] if len(keywords & set(r[1].lower().split())) > 0]

def get_failures():
    return [r[0] for r in fetchall("SELECT fail_reason FROM scripts WHERE success = 0 AND fail_reason != '' ORDER BY id DESC LIMIT 10")]

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

    system = "You are a Roblox Lua scripting assistant. Output only valid Lua code with no markdown fences, no explanation."
    if fails:
        system += "\n\nAvoid these mistakes:\n" + "\n".join(f"- {f}" for f in fails)
    if examples:
        system += "\n\nSuccessful examples:\n"
        for ex in examples:
            system += f"\nRequest: {ex[1]}\nCode:\n{ex[2]}\n"
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{OLLAMA}/api/chat", json={
                "model": MODEL,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": req.prompt}],
                "stream": False,
                "options": {"temperature": req.temperature}
            })
            r.raise_for_status()
            code = r.json()["message"]["content"].strip()
            if code.startswith("```"):
                lines = code.split("\n")
                code = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            try:
                row = fetchone("INSERT INTO scripts (prompt, code) VALUES (%s, %s) RETURNING id", (req.prompt, code))
            except (psycopg.Error, RuntimeError) as e:
                raise db_unavailable(e)
            return {"id": row[0], "code": code}
    except httpx.HTTPStatusError as e:
        # 404 here almost always means the model is still being pulled.
        detail = e.response.text[:300]
        raise HTTPException(503 if e.response.status_code == 404 else 502, f"ollama ({MODEL}): {detail}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"cannot reach ollama at {OLLAMA}: {e}")

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
        async with httpx.AsyncClient(timeout=5) as c:
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
