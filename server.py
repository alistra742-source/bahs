from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx, sqlite3, os
from pathlib import Path

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("MODEL", "qwen2.5-coder:3b")

SCHEMA = """CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt TEXT,
    code TEXT,
    success INTEGER DEFAULT -1,
    fail_reason TEXT DEFAULT ''
)"""

def open_db() -> sqlite3.Connection:
    """Prefer the mounted volume, fall back to the app directory if it is absent."""
    candidates = [Path(os.getenv("DB_PATH", "/data/learning.db")), Path(__file__).with_name("learning.db")]
    last_error: Optional[Exception] = None
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, check_same_thread=False)
            connection.execute(SCHEMA)
            connection.commit()
            print(f"[db] using {path}", flush=True)
            return connection
        except (sqlite3.Error, OSError) as e:
            last_error = e
    raise RuntimeError(f"could not open sqlite database: {last_error}")

conn = open_db()

class GenReq(BaseModel):
    prompt: str
    temperature: Optional[float] = 0.7

class FeedbackReq(BaseModel):
    script_id: int
    worked: bool
    notes: Optional[str] = ""

def get_examples(prompt: str, limit: int = 3):
    keywords = set(prompt.lower().split())
    rows = conn.execute("SELECT * FROM scripts WHERE success=1 ORDER BY id DESC LIMIT 50").fetchall()
    scored = sorted(rows, key=lambda r: len(keywords & set(r[1].lower().split())), reverse=True)
    return [r for r in scored[:limit] if len(keywords & set(r[1].lower().split())) > 0]

def get_failures():
    return [r[0] for r in conn.execute("SELECT fail_reason FROM scripts WHERE success=0 AND fail_reason!='' ORDER BY id DESC LIMIT 10").fetchall()]

@app.get("/")
async def root():
    return {"service": "roblox-lua-generator", "model": MODEL, "ollama": OLLAMA, "endpoints": ["/generate", "/feedback", "/health"]}

@app.post("/generate")
async def generate(req: GenReq):
    system = "You are a Roblox Lua scripting assistant. Output only valid Lua code with no markdown fences, no explanation."
    fails = get_failures()
    if fails:
        system += "\n\nAvoid these mistakes:\n" + "\n".join(f"- {f}" for f in fails)
    examples = get_examples(req.prompt)
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
            cur = conn.execute("INSERT INTO scripts (prompt, code) VALUES (?, ?)", (req.prompt, code))
            conn.commit()
            return {"id": cur.lastrowid, "code": code}
    except httpx.HTTPStatusError as e:
        # 404 here almost always means the model is still being pulled.
        detail = e.response.text[:300]
        raise HTTPException(503 if e.response.status_code == 404 else 502, f"ollama ({MODEL}): {detail}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"cannot reach ollama at {OLLAMA}: {e}")

@app.post("/feedback")
async def feedback(req: FeedbackReq):
    conn.execute("UPDATE scripts SET success=?, fail_reason=? WHERE id=?", (1 if req.worked else 0, "" if req.worked else req.notes, req.script_id))
    conn.commit()
    return {"status": "ok"}

@app.get("/health")
async def health():
    # Always 200 so the platform healthcheck only depends on the API being up;
    # ollama readiness is reported in the body.
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{OLLAMA}/api/tags")
            r.raise_for_status()
            models = [m.get("name") for m in r.json().get("models", [])]
            return {"status": "ok", "ollama": True, "model": MODEL, "models": models, "model_ready": MODEL in models}
    except httpx.HTTPError as e:
        return {"status": "degraded", "ollama": False, "model": MODEL, "error": str(e)}
