from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx, os, threading
import psycopg

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("MODEL", "qwen2.5-coder:3b")
# Railway's Postgres plugin injects DATABASE_URL; POSTGRES_URL is the older name.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""

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

@app.get("/")
async def root():
    return {"service": "roblox-lua-generator", "model": MODEL, "ollama": OLLAMA, "endpoints": ["/generate", "/feedback", "/health"]}

@app.post("/generate")
async def generate(req: GenReq):
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
async def feedback(req: FeedbackReq):
    try:
        fetchone(
            "UPDATE scripts SET success = %s, fail_reason = %s WHERE id = %s RETURNING id",
            (1 if req.worked else 0, "" if req.worked else req.notes, req.script_id),
        )
    except (psycopg.Error, RuntimeError) as e:
        raise db_unavailable(e)
    return {"status": "ok"}

@app.get("/health")
async def health():
    # Always 200 so the platform healthcheck only depends on the API being up;
    # the database and ollama readiness are reported in the body.
    body = {"status": "ok", "database": False, "ollama": False, "model": MODEL, "model_ready": False}
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
