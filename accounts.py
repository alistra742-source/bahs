"""First-party accounts. PostgreSQL in production; SQLite only for local development.

IP/device evidence limits abuse, not identity. No fingerprinting, wallet seeds,
passwords, session tokens, or raw IP addresses are stored in public assets/logs.
"""
import asyncio
import contextvars
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
import base64
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg.rows import dict_row
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

CURRENT_USER = contextvars.ContextVar("account", default=None)
PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
COOKIE = "bahs_session"
DEVICE_COOKIE = "bahs_device"
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL, trial_until DOUBLE PRECISION NOT NULL,
 paid_until DOUBLE PRECISION NOT NULL DEFAULT 0, lifetime INTEGER NOT NULL DEFAULT 0,
 settings TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sessions (
 digest TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), csrf TEXT NOT NULL,
 expires DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS trial_claims (
 signal TEXT PRIMARY KEY, claimed DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_windows (
 key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS account_hits (
 user_id TEXT NOT NULL REFERENCES users(id), platform TEXT NOT NULL, username TEXT NOT NULL,
 data TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(user_id,platform,username)
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), plan TEXT NOT NULL,
 currency TEXT NOT NULL, price TEXT NOT NULL, created DOUBLE PRECISION NOT NULL,
 expires DOUBLE PRECISION NOT NULL, watch_until DOUBLE PRECISION NOT NULL,
 provider_id TEXT UNIQUE, address TEXT NOT NULL DEFAULT '', amount TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'creating', granted INTEGER NOT NULL DEFAULT 0,
 poll_after DOUBLE PRECISION NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payment_receipts (
 order_id TEXT PRIMARY KEY REFERENCES orders(id), first_seen DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS issued_addresses (
 network TEXT NOT NULL, address TEXT NOT NULL, order_id TEXT NOT NULL UNIQUE,
 issued DOUBLE PRECISION NOT NULL, PRIMARY KEY(network,address)
);
CREATE INDEX IF NOT EXISTS order_user ON orders(user_id,created);
CREATE INDEX IF NOT EXISTS session_expiry ON sessions(expires);
CREATE TABLE IF NOT EXISTS challenges (
 id TEXT PRIMARY KEY, action TEXT NOT NULL, word TEXT NOT NULL,
 target INTEGER NOT NULL, expires DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS challenge_expiry ON challenges(expires);
"""


class Store:
    def __init__(self, url=None):
        self.url = url if url is not None else os.getenv("DATABASE_URL", "")
        self.path = os.getenv("ACCOUNT_DB_PATH", "data/accounts.sqlite3")
        self.pg = self.url.startswith(("postgres://", "postgresql://"))
        if self.url and not self.pg:
            raise RuntimeError("DATABASE_URL must be a PostgreSQL connection URL")
        if os.getenv("APP_ENV") == "production" and not self.pg:
            raise RuntimeError("Production requires DATABASE_URL")

    @contextmanager
    def transaction(self):
        if self.pg:
            with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10) as conn:
                yield Database(conn, True)
        else:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                yield Database(conn, False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self):
        with self.transaction() as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
        if not self.pg:
            os.chmod(self.path, 0o600)


class Database:
    def __init__(self, conn, pg):
        self.conn, self.pg = conn, pg

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s") if self.pg else sql, params)

    def one(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, params=()):
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def lock(self, key):
        if self.pg:
            digest = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
            self.execute("SELECT pg_advisory_xact_lock(?)", (digest,))


store = Store()


def secret():
    value = os.getenv("AUTH_SECRET", "")
    if len(value) < 32:
        raise HTTPException(503, "Account service requires a stable AUTH_SECRET (at least 32 characters)")
    return value.encode()


def digest(value):
    return hmac.new(secret(), value.encode(), hashlib.sha256).hexdigest()


def origin():
    value = os.getenv("APP_ORIGIN", "").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        raise HTTPException(503, "Set APP_ORIGIN to the exact public origin")
    if os.getenv("APP_ENV") == "production" and parsed.scheme != "https":
        raise HTTPException(503, "Production requires HTTPS")
    return value


def allowed_origins():
    return {origin(), *[o.rstrip("/") for o in os.getenv("AUTH_ALLOWED_ORIGINS", "").split(",") if o]}


def check_origin(request):
    if request.headers.get("origin") not in allowed_origins():
        raise HTTPException(403, "Origin not permitted")


def client_ip(request):
    # Never trust arbitrary client-supplied X-Forwarded-For. Uvicorn proxy header
    # rewriting must be disabled so request.client remains the socket peer.
    peer = request.client.host if request.client else "127.0.0.1"
    try:
        address = ipaddress.ip_address(peer)
        networks = [ipaddress.ip_network(n.strip()) for n in os.getenv("TRUSTED_PROXY_CIDRS", "").split(",") if n.strip()]
        if any(address in n for n in networks):
            chain = request.headers.get("x-forwarded-for", "").split(",")
            for entry in reversed(chain):
                address = ipaddress.ip_address(entry.strip())
                if not any(address in n for n in networks):
                    break
        return str(address)
    except ValueError:
        raise HTTPException(400, "Invalid network address")


def device_id(request):
    cookie = request.cookies.get(DEVICE_COOKIE, "")
    parts = cookie.split(".")
    if len(parts) == 2 and re.fullmatch(r"[a-f0-9]{64}", parts[0]) and hmac.compare_digest(digest("device:" + parts[0]), parts[1]):
        return parts[0]
    return secrets.token_hex(32)


def rate_limit(key, maximum, seconds):
    now = time.time()
    with store.transaction() as db:
        db.lock("rate:" + key)
        row = db.one("SELECT * FROM rate_windows WHERE key=?", (key,))
        if row and row["expires"] > now:
            if row["count"] >= maximum:
                raise HTTPException(429, "Too many attempts. Try again later.", headers={"Retry-After": str(int(row["expires"] - now) + 1)})
            db.execute("UPDATE rate_windows SET count=count+1 WHERE key=?", (key,))
        else:
            db.execute("INSERT INTO rate_windows(key,count,expires) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=1,expires=excluded.expires", (key, now + seconds))
        db.execute("DELETE FROM rate_windows WHERE expires<?", (now - 86400,))


def public_user(user):
    now = time.time()
    end = max(user["trial_until"], user["paid_until"])
    return {
        "id": user["id"], "username": user["username"],
        "access": bool(user["lifetime"] or end > now),
        "plan": "forever" if user["lifetime"] else "monthly" if user["paid_until"] > now else "trial" if user["trial_until"] > now else "expired",
        "trial_until": user["trial_until"], "paid_until": user["paid_until"],
        "remaining_seconds": max(0, int(end - now)), "lifetime": bool(user["lifetime"]),
        "priority_support": bool(user["lifetime"]),
        "admin": user["username"] in os.getenv("ADMIN_USERS", "").lower().split(","),
    }


def authenticate(token):
    if not token or len(token) > 100:
        return None
    with store.transaction() as db:
        row = db.one("SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.digest=? AND s.expires>?", (digest("session:" + token), time.time()))
    return row


def principal():
    user = CURRENT_USER.get()
    if user is None:
        raise HTTPException(401, "Log in to continue")
    return user


def owner_id():
    return principal()["id"]


def access_deadline(uid):
    with store.transaction() as db:
        user = db.one("SELECT trial_until,paid_until,lifetime FROM users WHERE id=?", (uid,))
    if not user:
        return 0
    return float("inf") if user["lifetime"] else max(user["trial_until"], user["paid_until"])


def make_session(user, device):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store.transaction() as db:
        db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (digest("session:" + token), user["id"], csrf, time.time() + 7 * 86400))
    response = JSONResponse({"user": public_user(user), "csrf": csrf})
    secure = origin().startswith("https://")
    response.set_cookie(COOKIE, token, secure=secure, httponly=True, samesite="strict", max_age=7 * 86400, path="/")
    response.set_cookie(DEVICE_COOKIE, device + "." + digest("device:" + device), secure=secure, httponly=True, samesite="strict", max_age=365 * 86400, path="/")
    response.headers["Cache-Control"] = "private, no-store"
    return response


# Human check: drag a chip onto a target, then type a distorted word. Challenges are
# stored in the database (not process memory), so they survive restarts and work across
# instances. Each one is bound to an action, expires quickly and is consumed on the first
# attempt, so a solved answer cannot be replayed.
CHALLENGE_TTL = 600  # generous: slow solvers and proxy hops must not be punished
CHALLENGE_CAP = 5000
DRAG_TOLERANCE = 4
WORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I/L/O/0/1: easy to misread, still typable


def _word_image(word: str) -> str:
    rnd = secrets.SystemRandom()
    w, h = 260, 90
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
             f'<rect width="{w}" height="{h}" fill="#0e0e13"/>']
    for _ in range(8):
        parts.append(f'<line x1="{rnd.randint(0, w)}" y1="{rnd.randint(0, h)}" x2="{rnd.randint(0, w)}" y2="{rnd.randint(0, h)}" stroke="#5a5a68" stroke-width="{rnd.uniform(1, 2.5):.1f}"/>')
    for _ in range(70):
        parts.append(f'<circle cx="{rnd.randint(0, w)}" cy="{rnd.randint(0, h)}" r="1.2" fill="#4a4a58"/>')
    x = 16
    for ch in word:
        y, rot, size, skew = rnd.randint(50, 68), rnd.randint(-28, 28), rnd.randint(30, 38), rnd.randint(-18, 18)
        fill = rnd.choice(["#f2f2f4", "#c97a45", "#d8d8de"])
        parts.append(f'<text x="{x}" y="{y}" font-family="monospace" font-weight="700" font-size="{size}" fill="{fill}" transform="rotate({rot} {x} {y}) skewX({skew})">{ch}</text>')
        x += rnd.randint(32, 38)
    parts.append("</svg>")
    return "data:image/svg+xml;base64," + base64.b64encode("".join(parts).encode()).decode()


def new_challenge(action: str) -> dict:
    cid = secrets.token_urlsafe(24)
    word = "".join(secrets.choice(WORD_ALPHABET) for _ in range(6))
    target = secrets.randbelow(71) + 15
    now = time.time()
    with store.transaction() as db:
        db.execute("DELETE FROM challenges WHERE expires<?", (now,))
        if db.one("SELECT COUNT(*) AS n FROM challenges")["n"] >= CHALLENGE_CAP:
            raise HTTPException(429, "Too many verification requests. Try again shortly.")
        db.execute("INSERT INTO challenges(id,action,word,target,expires) VALUES(?,?,?,?,?)",
                   (cid, action, word, target, now + CHALLENGE_TTL))
    return {"id": cid, "target": target, "tolerance": DRAG_TOLERANCE, "word_image": _word_image(word)}


def captcha(body, action: str) -> None:
    # Consume the challenge first: a wrong answer or a replay both use it up.
    with store.transaction() as db:
        entry = db.one("DELETE FROM challenges WHERE id=? RETURNING action,word,target,expires", (body.challenge_id,))
    # No IP binding: the requesting IP can differ between the challenge GET and the
    # POST behind a proxy, which made valid answers fail as "expired".
    if not entry or entry["expires"] < time.time() or entry["action"] != action:
        raise HTTPException(400, "Verification expired. Please try again.")
    drag_ok = 0 <= body.drag <= 100 and abs(body.drag - entry["target"]) <= DRAG_TOLERANCE
    word_ok = hmac.compare_digest(body.word.strip().upper().encode(), entry["word"].encode())
    if not (drag_ok and word_ok):
        raise HTTPException(400, "Verification failed. Please try again.")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=20, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=3, max_length=20)
    challenge_id: str = Field(default="", max_length=64)
    drag: int = Field(default=-1, ge=-1, le=100)
    word: str = Field(default="", max_length=16)
    website: str = Field(default="", max_length=100)  # honeypot; not security on its own


router = APIRouter()


@router.get("/auth/challenge")
def auth_challenge(action: str = Query("signup", pattern="^(signup|login)$"), request: Request = None):
    return new_challenge(action)


@router.get("/auth/config")
def auth_config():
    return {
        "ready": True,
        "trial_seconds": 60,
        "payment_ready": bool(os.getenv("NOWPAYMENTS_ENABLED") == "1" and os.getenv("NOWPAYMENTS_API_KEY") and os.getenv("NOWPAYMENTS_IPN_SECRET")),
        "crypto": ["SOL", "LTC", "ETH", "BTC", "USDT", "USDC"]
    }


@router.post("/auth/signup")
async def signup(body: Credentials, request: Request):
    check_origin(request)
    ip, device = client_ip(request), device_id(request)
    await asyncio.to_thread(rate_limit, "signup:" + digest(ip), 5, 3600)
    if body.website:
        raise HTTPException(400, "Security verification failed")
    await asyncio.to_thread(captcha, body, "signup")
    username = body.username.lower()
    if username in {u.strip() for u in os.getenv("ADMIN_USERS", "").lower().split(",") if u.strip()}:
        raise HTTPException(409, "Account cannot be created with that username")
    await asyncio.to_thread(rate_limit, "signup-global", 60, 60)
    password_hash = await asyncio.to_thread(PASSWORDS.hash, body.password)
    def create():
        signals = sorted({digest("trial-ip:" + ip), digest("trial-device:" + device)})
        now, uid = time.time(), uuid.uuid4().hex
        with store.transaction() as db:
            for s in signals:
                db.lock("trial:" + s)
            db.lock("username:" + username)
            if db.one("SELECT id FROM users WHERE username=?", (username,)):
                raise HTTPException(409, "Account cannot be created with that username")
            used = any(db.one("SELECT signal FROM trial_claims WHERE signal=?", (s,)) for s in signals)
            until = 0 if used else now + 60
            db.execute("INSERT INTO users(id,username,password_hash,created,trial_until) VALUES(?,?,?,?,?)", (uid, username, password_hash, now, until))
            # Record both new signals even when one was already claimed. Clearing
            # cookies or changing IP alone therefore never creates a new trial.
            for s in signals:
                db.execute("INSERT INTO trial_claims VALUES(?,?) ON CONFLICT(signal) DO NOTHING", (s, now))
            return db.one("SELECT * FROM users WHERE id=?", (uid,))
    user = await asyncio.to_thread(create)
    return await asyncio.to_thread(make_session, user, device)


@router.post("/auth/login")
async def login(body: Credentials, request: Request):
    check_origin(request)
    ip = client_ip(request)
    await asyncio.to_thread(rate_limit, "login-ip:" + digest(ip), 15, 900)
    await asyncio.to_thread(rate_limit, "login-name:" + digest(body.username.lower()), 15, 900)
    if body.website:
        raise HTTPException(400, "Security verification failed")
    await asyncio.to_thread(captcha, body, "login")
    await asyncio.to_thread(rate_limit, "login-global", 120, 60)
    def verify():
        with store.transaction() as db:
            user = db.one("SELECT * FROM users WHERE username=?", (body.username.lower(),))
        # An unknown user still incurs Argon2 work, limiting username timing leaks.
        encoded = user["password_hash"] if user else DUMMY_HASH
        try:
            PASSWORDS.verify(encoded, body.password)
        except (VerificationError, InvalidHashError):
            raise HTTPException(401, "Invalid username or password")
        if not user:
            raise HTTPException(401, "Invalid username or password")
        if PASSWORDS.check_needs_rehash(encoded):
            with store.transaction() as db:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (PASSWORDS.hash(body.password), user["id"]))
        return user
    user = await asyncio.to_thread(verify)
    return await asyncio.to_thread(make_session, user, device_id(request))


DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))


@router.get("/auth/status")
def session_status(request: Request):
    """Optional own-session introspection avoids expected 401s on public pages."""
    user = authenticate(request.cookies.get(COOKIE, ""))
    response = JSONResponse({"user": public_user(user) if user else None, "csrf": user["csrf"] if user else ""})
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/auth/me")
def me():
    user = principal()
    return {"user": public_user(user), "csrf": user["csrf"]}


@router.post("/auth/logout")
def logout(request: Request):
    with store.transaction() as db:
        db.execute("DELETE FROM sessions WHERE digest=?", (digest("session:" + request.cookies.get(COOKIE, "")),))
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


class AccountMiddleware:
    """ASGI (not BaseHTTPMiddleware): context follows streaming and detached tasks."""
    PUBLIC = {"/", "/health", "/info", "/stats", "/docs", "/menu", "/app.js", "/app.css", "/favicon.ico", "/auth/config", "/auth/status", "/auth/challenge", "/auth/signup", "/auth/login", "/billing/plans", "/billing/webhook"}
    PAGES = {"/console", "/hits/", "/platforms", "/proxies/", "/alerts", "/settings/", "/buy", "/account"}
    ADMIN = {"/proxies/upload", "/proxies/clear"}
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        path = scope["path"]
        user, token = None, None
        try:
            # Bound request bodies before Pydantic/JSON parses them. Chunked
            # bodies get the same cap as Content-Length bodies.
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                chunks, size = [], 0
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    data = message.get("body", b"")
                    size += len(data)
                    if size > 1024 * 1024:
                        raise HTTPException(413, "Request body is too large")
                    chunks.append(data)
                    if not message.get("more_body"):
                        break
                body = b"".join(chunks)
                supplied = False
                original_receive = receive
                async def limited_receive():
                    nonlocal supplied
                    if not supplied:
                        supplied = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await original_receive()
                receive = limited_receive
            public = path in self.PUBLIC or path.startswith("/ui/") or path in self.PAGES
            if not public:
                user = await asyncio.to_thread(authenticate, request.cookies.get(COOKIE, ""))
                if user is None:
                    raise HTTPException(401, "Log in to continue")
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    check_origin(request)
                    if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), user["csrf"]):
                        raise HTTPException(403, "Invalid request token")
                view = public_user(user)
                if (path in self.ADMIN or path == "/proxies" and request.method != "GET") and not view["admin"]:
                    raise HTTPException(403, "Administrator access required")
                if path in {"/scan", "/snipe", "/generate"} and not view["access"]:
                    raise HTTPException(402, "Trial ended. Choose a plan to continue.")
                await asyncio.to_thread(rate_limit, "api:" + user["id"], 120, 60)
            token = CURRENT_USER.set(user)
            async def secure_send(message):
                if message["type"] == "http.response.start":
                    headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in {b"cache-control"}] if user else message.get("headers", [])
                    if user:
                        headers.append((b"cache-control", b"private, no-store"))
                    headers.extend([(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"), (b"x-frame-options", b"DENY"), (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"), (b"content-security-policy", b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'")])
                    if os.getenv("APP_ENV") == "production":
                        headers.append((b"strict-transport-security", b"max-age=31536000"))
                    message = {**message, "headers": headers}
                await send(message)
            await self.app(scope, receive, secure_send)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers={"Cache-Control": "private, no-store", **(exc.headers or {})})
            await response(scope, receive, send)
        finally:
            if token is not None:
                CURRENT_USER.reset(token)
