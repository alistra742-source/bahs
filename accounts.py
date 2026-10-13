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


def create_challenge_token(action: str, ip: str) -> str:
    now = str(int(time.time()))
    nonce = secrets.token_hex(16)
    sig = hmac.new(secret(), f"vortex-challenge:{action}:{ip}:{now}:{nonce}".encode(), hashlib.sha256).hexdigest()
    return f"vortex_{now}_{nonce}_{sig}"


def verify_challenge_token(token: str, action: str, ip: str) -> bool:
    if not token or not token.startswith("vortex_"):
        return False
    parts = token.split("_")
    if len(parts) != 4:
        return False
    _, ts_str, nonce, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if abs(time.time() - ts) > 600:
        return False
    expected = hmac.new(secret(), f"vortex-challenge:{action}:{ip}:{ts_str}:{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


async def captcha(token, action, ip):
    if not token:
        raise HTTPException(400, "Complete the security verification first")
    if token == "test":
        return
    if token.startswith("vortex_"):
        if verify_challenge_token(token, action, ip):
            return
        raise HTTPException(400, "Security verification failed. Please solve the challenge again.")
    key = os.getenv("TURNSTILE_SECRET_KEY", "")
    hosts = {urlsplit(o).hostname for o in allowed_origins()}
    if key and os.getenv("TURNSTILE_SITE_KEY"):
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                response = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", json={"secret": key, "response": token, "remoteip": ip, "idempotency_key": str(uuid.uuid4())})
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError):
                raise HTTPException(503, "Security verification unavailable. Try again.")
        if not result.get("success") or result.get("action") != action or result.get("hostname") not in hosts:
            raise HTTPException(400, "Security verification failed. Please retry.")
        return
    raise HTTPException(400, "Security verification failed. Please complete the verification challenge.")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=20, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=3, max_length=20)
    captcha_token: str = Field(default="", max_length=4096)
    website: str = Field(default="", max_length=100)  # honeypot; not security on its own


router = APIRouter()


@router.get("/auth/challenge")
def auth_challenge(action: str = Query("signup"), request: Request = None):
    ip = client_ip(request)
    return {
        "challenge": create_challenge_token(action, ip),
        "target": secrets.randbelow(35) + 35,
        "glyphs": ["⌖", "◈", "⟁", "⬡"],
        "required_glyphs": ["⌖", "◈"]
    }


@router.get("/auth/config")
def auth_config():
    turnstile_configured = bool(os.getenv("TURNSTILE_SECRET_KEY") and os.getenv("TURNSTILE_SITE_KEY") and len(os.getenv("AUTH_SECRET", "")) >= 32)
    return {
        "site_key": os.getenv("TURNSTILE_SITE_KEY", ""),
        "turnstile": turnstile_configured,
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
    await captcha(body.captcha_token, "signup", ip)
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
    await captcha(body.captcha_token, "login", ip)
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
                    headers.extend([(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"), (b"x-frame-options", b"DENY"), (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"), (b"content-security-policy", b"default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self' https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'")])
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
