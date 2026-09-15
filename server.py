from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import html, httpx, os, threading
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

# Status page served at / so the service domain shows something useful instead of
# raw JSON. Placeholders are replaced in root(), which avoids escaping the CSS braces.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>bahs - Roblox Lua generator</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0b0d12;color:#e6e8ef;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body::before{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(900px 500px at 12% -10%,rgba(47,129,247,.16),transparent 60%),radial-gradient(700px 420px at 100% 0,rgba(63,185,80,.10),transparent 55%)}
.wrap{position:relative;max-width:820px;margin:0 auto;padding:56px 24px 64px}
.brand{display:flex;align-items:center;gap:12px;font-size:26px;font-weight:650;letter-spacing:-.02em}
.logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#2f81f7,#3fb950);display:grid;place-items:center;font:600 16px ui-monospace,monospace;color:#0b0d12}
.sub{margin-top:10px;color:#9aa4b8}
.sub code,footer code{background:#151a23;border:1px solid #222b38;border-radius:6px;padding:2px 6px;font:12px ui-monospace,monospace;color:#c9d3e4}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:28px 0 8px}
.card{background:#11151d;border:1px solid #202836;border-radius:12px;padding:14px 16px}
.card.ok{border-color:#1f4d2c;background:linear-gradient(180deg,#101a14,#11151d)}
.card.bad{border-color:#4d2420;background:linear-gradient(180deg,#1a1311,#11151d)}
.row{display:flex;align-items:center;gap:8px}
.led{width:8px;height:8px;border-radius:50%;background:#3fb950;box-shadow:0 0 10px rgba(63,185,80,.55);flex:none}
.card.bad .led{background:#f85149;box-shadow:0 0 10px rgba(248,81,73,.55)}
.name{font:600 13px ui-monospace,monospace;text-transform:uppercase;letter-spacing:.08em;color:#cbd5e5}
.detail{margin-top:6px;font:12px/1.5 ui-monospace,monospace;color:#8c96ab;overflow-wrap:anywhere}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:#8c96ab;margin:32px 0 12px}
ul{list-style:none;margin:0;padding:0;border:1px solid #202836;border-radius:12px;overflow:hidden}
li{display:flex;gap:12px;align-items:baseline;padding:11px 16px;background:#11151d;border-top:1px solid #1a212c}
li:first-child{border-top:0}
.verb{font:600 11px ui-monospace,monospace;color:#3fb950;min-width:44px}
.path{font:12px ui-monospace,monospace;color:#79c0ff;min-width:92px}
.desc{color:#9aa4b8;font-size:13px}
pre{margin:0;background:#0e131b;border:1px solid #202836;border-radius:12px;padding:16px;overflow:auto;font:12px/1.7 ui-monospace,monospace;color:#c9d3e4}
pre .k{color:#79c0ff}
footer{margin-top:36px;padding-top:18px;border-top:1px solid #1a212c;color:#69738a;font-size:12px}
footer a{color:#79c0ff;text-decoration:none}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand"><span class="logo">b</span> bahs</div>
  <div class="sub">Roblox Lua generator API &middot; Ollama <code>__MODEL__</code> &middot; __STATUS__</div>
  <section class="cards">__CARDS__</section>
  <h2>Endpoints</h2>
  <ul>
    <li><span class="verb">GET</span><span class="path">/health</span><span class="desc">Postgres and Ollama status, always 200</span></li>
    <li><span class="verb">POST</span><span class="path">/generate</span><span class="desc">{"prompt": "...", "temperature": 0.7} &rarr; {"id", "code"}</span></li>
    <li><span class="verb">POST</span><span class="path">/feedback</span><span class="desc">{"script_id": 1, "worked": true, "notes": ""} feeds later prompts</span></li>
    <li><span class="verb">GET</span><span class="path">/docs</span><span class="desc">interactive OpenAPI docs</span></li>
  </ul>
  <h2>Quickstart</h2>
  <pre>curl -X POST <span class="k">"$API/generate"</span> -H <span class="k">'Content-Type: application/json'</span> \
  -d <span class="k">'{"prompt":"kill aura with a toggle key"}'</span></pre>
  <footer>Live status, refreshes every 10s &middot; send requests from the Roblox executor client in <code>client.lua</code>.</footer>
</div>
</body>
</html>
"""

def pill(ok: bool, name: str, detail: str) -> str:
    return (
        f'<div class="card {"ok" if ok else "bad"}"><div class="row">'
        f'<span class="led"></span><span class="name">{name}</span></div>'
        f'<div class="detail">{html.escape(detail)}</div></div>'
    )

@app.get("/", response_class=HTMLResponse)
async def root():
    state = await snapshot()
    cards = "".join([
        pill(True, "api", "uvicorn online"),
        pill(state["database"], "postgres", "connected" if state["database"]
             else str(state.get("database_error", "not configured (add DATABASE_URL)"))),
        pill(state["ollama"], "ollama", "reachable on loopback" if state["ollama"]
             else str(state.get("error", "unreachable"))),
        pill(state["model_ready"], "model", f"{MODEL} ready" if state["model_ready"]
             else f"{MODEL} pulling or missing"),
    ])
    return HTMLResponse(
        PAGE.replace("__CARDS__", cards)
            .replace("__MODEL__", html.escape(MODEL))
            .replace("__STATUS__", html.escape(state["status"]))
    )

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

async def snapshot() -> dict:
    # Always reports rather than raising, so the platform healthcheck only depends
    # on the API being up; database and ollama readiness come back in the body.
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

@app.get("/health")
async def health():
    return await snapshot()
