from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path
from collections import defaultdict, deque
import asyncio, hmac, html, httpx, json, os, re, threading, time, uuid

# --------------------------------------------------------------------------------------
# What this service is: two models, in a chain, behind one API.
#
#   you -- ask --> bahs -- draft -->                    Qwen      (qwen-api)
#                      \
#                       `- review -->                    DeepSeek  (V4 Flash, thinking off)
#                           \
#                            `- refine, same chat as the draft --> Qwen
#
# chat.qwen.ai has no public API. github.com/encryptarun/qwen-api turns it into
# OpenAI-compatible endpoints using the Qwen *access token* from the browser
# (chat.qwen.ai -> DevTools console -> localStorage.token). That token is the key to a
# whole Qwen account, so it lives here and never in a page or a Roblox script.
#
# The reviewer is DeepSeek V4 Flash (the model behind minitoolai.com, which is a web
# front-end to DeepSeek's own API) -- so this talks to api.deepseek.com directly, with
# thinking switched off, and it is never the caller's to choose.
#
#   POST /chat/stream           one turn -> a job id, the whole chain runs in the job
#   GET  /chat/stream/{job}     NDJSON: replay, text, phase changes, heartbeats, done
#   POST /chat                  the same chain, blocking (client.lua)
#   POST /generate[...]         one prompt, no history, same chain
#   POST /v1/chat/completions   OpenAI-compatible passthrough, pinned to Qwen
#   GET  /v1/models, /health
#
# Every question is sent as "Hy kanha <your question>" (GREETING), always to QWEN_MODEL
# with thinking off. The conversation belongs to the caller: the turns it sends are the
# turns the model sees, so a follow-up is answered in the same chat it has been answering
# in. The review and the refine instruction are internal turns -- they are never part of
# what the user's next question carries.
#
# Nothing is pulled, loaded or warmed: no weights, no GPU, no volume, no database. The one
# file this service reads is Send.txt, the brief handed to the reviewer before anything
# else (REVIEW_BRIEF).
# --------------------------------------------------------------------------------------


def env(*names: str, default: str = "") -> str:
    """The first of these variables that is set, so an old name keeps working."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


# --- the two providers ------------------------------------------------------------------

class Provider:
    """One OpenAI-compatible endpoint, plus whatever it calls "answer without thinking".

    Both sides speak the same request and response shape, so the only per-provider
    knowledge is where it lives, what it calls the model, and how it spells "no thinking".
    """

    def __init__(self, name: str, url: str, key: str, model: str, dialect: dict,
                 timeout: float, extra: Optional[dict] = None, shape: str = "openai"):
        self.name = name
        self.url = url.rstrip("/")
        self.key = key
        self.model = model
        self.dialect = dialect
        self.timeout = timeout
        self.extra = extra or {}
        self.shape = shape

    @property
    def configured(self) -> bool:
        return bool(self.key and self.url and self.model)

    def label(self) -> str:
        """Short host name, for the page's chips."""
        return self.url.split("//", 1)[-1].split("/", 1)[0]

    def endpoint(self) -> str:
        if self.url.endswith("/chat/completions"):
            return self.url
        return f"{self.url}/chat/completions"

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    def request(self, messages: list, temperature: Optional[float], max_tokens: int,
                stream: bool = True) -> dict:
        """An OpenAI-shaped body, pinned to this provider's model and thinking mode.

        The model is not the caller's to choose: one model per role, so a request behaves
        the same whoever sends it.
        """
        body = {
            "model": self.model,
            "messages": fold(self.shape, messages),
            "stream": stream,
            **self.dialect,
            **self.extra,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens > 0:
            body["max_tokens"] = max_tokens
        return body


def fold(shape: str, messages: list) -> list:
    """One prompt for an endpoint that has no system role (the web-chat bridges).

    The brief has to come before anything else, so the turns are concatenated in the order
    they were built -- system first -- rather than being dropped or reordered. An
    OpenAI-shaped endpoint gets the turns untouched.
    """
    if shape != "web":
        return messages
    parts = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        parts.append(content.strip())
    return [{"role": "user", "content": "\n\n".join(parts)}]


def qwen_token() -> str:
    """The Qwen access token, under whichever name it was put in the variables."""
    return env("QWEN_TOKEN", "QWEN_API_KEY", "QWEN_ACCESS_TOKEN")


QWEN_URL = env("QWEN_URL", default="https://qwen.aikit.club/v1")
# qwen-api also serves its own bookkeeping endpoints (/validate) at the root.
QWEN_ROOT = QWEN_URL[: -len("/v1")] if QWEN_URL.endswith("/v1") else QWEN_URL

QWEN_TOKEN = qwen_token()
# Every generation is sent to this model. There is no picker: one model, one behaviour.
QWEN_MODEL = env("QWEN_MODEL", default="qwen3.8-max")
# fast (answer straight away) | auto | thinking. Forced onto every request: reasoning
# tokens are billed against max_tokens and the answer is what is wanted, not the thinking.
QWEN_THINKING = env("QWEN_THINKING", default="fast")
# Qwen answers in seconds; this is a backstop for a provider that hangs.
CHAT_TIMEOUT = float(env("CHAT_TIMEOUT", default="300"))

QWEN = Provider(
    "qwen", QWEN_URL, QWEN_TOKEN, QWEN_MODEL,
    {"thinking_mode": QWEN_THINKING}, CHAT_TIMEOUT,
)

# --- the reviewer ------------------------------------------------------------------------
#
# DeepSeek V4 Flash, thinking off, search off. It is only ever sent a script to criticise,
# never the user's conversation, so it never needs to be the model the user chose.
#
# Two things about "chat.deepseek.com" that decide the shape of this section:
#   * chat.deepseek.com is a web app with no public API. Calling it directly needs a
#     signed-in browser session that has passed its AWS WAF human-check (a cf_clearance
#     cookie) and a proof of work solved per request by executing DeepSeek's own wasm. A
#     server cannot clear that check, so this service does not pretend to try.
#   * DeepSeek's *API* is the thing that takes a token, and it is the same models. A key
#     from platform.deepseek.com goes in DEEPSEEK_TOKEN and everything works from here.
#
# If you would rather go through the web chat, it has to be through a bridge that sits in
# front of it and speaks OpenAI (a browser-running deployment of xtekky/deepseek4free or
# sums001/Deepseek-API do exactly that): point REVIEW_URL at it, paste its token into
# DEEPSEEK_TOKEN, and set REVIEW_SHAPE=web so the toggles are sent in the shape it expects.
REVIEW_URL = env("REVIEW_URL", "DEEPSEEK_URL", default="https://api.deepseek.com")
REVIEW_KEY = env("DEEPSEEK_TOKEN", "REVIEW_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY")
REVIEW_MODEL = env("REVIEW_MODEL", "DEEPSEEK_MODEL", default="deepseek-v4-flash")
# openai (an OpenAI-shaped endpoint, including api.deepseek.com) | web (a bridge in front of
# chat.deepseek.com: no system role, and the toggles are plain booleans)
REVIEW_SHAPE = env("REVIEW_SHAPE", default="openai").lower()
# Thinking is enabled by default on DeepSeek V4, so it is switched off explicitly, and search
# is never switched on anywhere in this service: a review has to be cheap, quick, and about
# the script in front of it rather than about the web.
REVIEW_THINKING = env("REVIEW_THINKING", default="off").lower()
SEARCH_OFF = True
REVIEW_TEMPERATURE = float(env("REVIEW_TEMPERATURE", default="0.2"))
REVIEW_MAX_TOKENS = int(env("REVIEW_MAX_TOKENS", default="2048"))
# Most reviewers will list anything. A cap keeps the refine instruction — and the cost of
# applying it — bounded.
REVIEW_ITEMS = int(env("REVIEW_ITEMS", default="8"))
# The script sent for review is bounded too: a 1M-token context is not a reason to use it.
REVIEW_SCRIPT_MAX = int(env("REVIEW_SCRIPT_MAX", default="24000"))
# How much of the brief in front of the review is sent. Send.txt is yours; this is the
# ceiling on it, so a stray huge file cannot become the whole prompt.
REVIEW_BRIEF_MAX = int(env("REVIEW_BRIEF_MAX", default="60000"))
# What the script is supposed to run in. Handed to the reviewer so its complaints are about
# this runtime and not about a general-purpose script.
TARGET_RUNTIME = env("TARGET_RUNTIME", default="Roblox Luau (a Roblox script, run in Studio or an executor)")

REVIEW_EXTRA: dict = {}
try:
    _extra = json.loads(env("REVIEW_EXTRA", default="{}") or "{}")
    if isinstance(_extra, dict):
        REVIEW_EXTRA = _extra
except ValueError:
    print("[bridge] REVIEW_EXTRA is not valid JSON; ignoring it", flush=True)

def reviewer_dialect() -> dict:
    """DeepSeek's off-switches, in whichever shape the endpoint expects.

    The API takes `thinking: {"type": "disabled"}`. A bridge in front of the web chat takes
    booleans. Search is set to false in both, and is never set true anywhere in this file.
    """
    thinking_on = REVIEW_THINKING in ("on", "thinking", "enabled", "slow")
    if REVIEW_SHAPE == "web":
        return {"thinking": thinking_on, "search": not SEARCH_OFF,
                "thinking_enabled": thinking_on, "search_enabled": not SEARCH_OFF}
    return {"thinking": {"type": "enabled" if thinking_on else "disabled"}}


REVIEWER = Provider(
    "deepseek", REVIEW_URL, REVIEW_KEY, REVIEW_MODEL, reviewer_dialect(),
    float(env("REVIEW_TIMEOUT", default="180")),
    REVIEW_EXTRA,
    REVIEW_SHAPE,
)

# on (always) | off (never) | auto (only when a reviewer key is set)
PIPELINE = env("PIPELINE", default="auto").lower()


def review_enabled() -> bool:
    """Whether a question goes through the reviewer as well."""
    if PIPELINE in ("off", "0", "false", "no"):
        return False
    if PIPELINE in ("on", "1", "true", "yes", "always"):
        return REVIEWER.configured
    return REVIEWER.configured


# --- the tokens one call may use ---------------------------------------------------------
#
# Three calls, three ceilings. The draft and the rewrite produce whole scripts, so they get
# room; the review produces a list, so it does not. A draft that stops at its ceiling is
# reported as a failure instead of being shipped, because everything after it would be built
# on a cut-off script.
MAX_TOKENS = int(env("MAX_TOKENS", default="4096"))
DRAFT_TOKENS = int(env("DRAFT_TOKENS", default=str(MAX_TOKENS)))
REFINE_TOKENS = int(env("REFINE_TOKENS", default="8192"))
# How much of a review is pasted into the refine instruction when it came back as prose.
REVIEW_PASTE_MAX = int(env("REVIEW_PASTE_MAX", default="4000"))

# --- jobs, history and limits ------------------------------------------------------------
#
# How much of a long chat one request may carry. The newest turns are kept; the middle of a
# conversation is dropped rather than growing the prompt forever.
HISTORY_MESSAGES = int(env("HISTORY_MESSAGES", default="40"))
HISTORY_CHARS = int(env("HISTORY_CHARS", default="120000"))
# A generation is a server-side job, so a reader can go quiet without the answer being lost.
# That quiet is exactly what proxies and sleeping phones drop, so the stream is punctuated with
# a heartbeat this often.
HEARTBEAT = float(env("HEARTBEAT", default="5"))
# How long a finished job stays readable, so a browser that comes back late can still collect
# the answer instead of finding nothing.
JOB_TTL = float(env("JOB_TTL", default="3600"))
# How often /health may ask a provider whether its key still works.
TOKEN_CHECK_TTL = float(env("TOKEN_CHECK_TTL", default="60"))
# Requests per minute per IP on the endpoints the page uses, and how many chains may run at
# once. The page has no login by design, so these are what stand between the URL and the two
# accounts behind it. RATE_LIMIT 0 disables the per-IP window.
RATE_LIMIT = int(env("RATE_LIMIT", default="30"))
MAX_CONCURRENT = int(env("MAX_CONCURRENT", default="4"))
# The key callers must send on /v1, /chat and /generate: API_KEY when it is set, otherwise the
# Qwen token itself -- one secret to keep. "" leaves those endpoints open as well.
API_KEY = env("API_KEY")
CALLER_KEY = API_KEY or QWEN_TOKEN

INDEX = Path(__file__).parent / "web" / "index.html"


# --- the brief handed to the reviewer ----------------------------------------------------

# Which file is the brief. The repo has two -- send.txt and Send.txt -- because they differ
# only in the case of one letter, which is a trap: a clone on a case-insensitive filesystem
# can only hold one of them. The lowercase one is the newer, so it is the one used, and the
# other is the fallback rather than being silently ignored. Either can be forced with
# REVIEW_BRIEF, and whichever is in use is named on /health as reviewer.brief.
_BRIEF_HERE = Path(__file__).parent
BRIEF_CHOICES = [_BRIEF_HERE / name for name in ("send.txt", "Send.txt")]
_BRIEF_ASKED = env("REVIEW_BRIEF", default="")
BRIEF_PATH = (Path(_BRIEF_ASKED) if _BRIEF_ASKED
              else next((p for p in BRIEF_CHOICES if p.exists()), BRIEF_CHOICES[-1]))


def load_brief() -> str:
    """Send.txt: what the reviewer is told before it is asked anything.

    Read once at boot. A missing file is not an error -- the built-in rubric below still
    makes the review usable -- but it is reported on /health so a file that did not make it
    into the image is visible rather than silent.
    """
    try:
        text = BRIEF_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    if len(text) > REVIEW_BRIEF_MAX:
        text = text[:REVIEW_BRIEF_MAX] + "\n\n[... brief truncated ...]"
    return text


BRIEF = load_brief()

# Appended to the brief (or used alone), because the review has to come back in a shape the
# refine step can act on. Prose reviews are what make a chain like this useless.
RUBRIC = f"""You are the reviewer in a two-model chain. Another model wrote the script below in \
answer to a request; your review is the only thing standing between it and the user.

Judge only whether it will actually work in {TARGET_RUNTIME}. Ignore style, naming and \
formatting. Report a point only when you can name the concrete failure it causes.

Answer in exactly this format, and nothing else:

VERDICT: OK

if there is nothing that would stop it working, or

VERDICT: ISSUES
1. <what is wrong> | where: <the function or line> | why it fails: <what actually goes wrong> | fix: <the exact change to make>
2. ...

At most {REVIEW_ITEMS} points, most serious first. Rules for this format:
- One point per line, starting with its number. No blank lines between points.
- The four parts are separated by " | ". Keep each part to one sentence.
- Look for: API names and properties that do not exist in Roblox, deprecated globals that \
no longer run, server/client confusion, a yield where none can happen, a loop that never \
ends, an event that is connected twice, and anything that throws on the first line.
- Do not praise. Do not summarise. Do not rewrite the script. Do not explain what you like.
- If the script is a fragment or the request was conversational rather than a request for \
code, answer VERDICT: OK."""


# --- addressing the model ---------------------------------------------------------------

# Every question is addressed to the model with this in front of it, so a request that reads
# "make me this" is sent as "Hy kanha make me this". Set GREETING to "" to send messages
# untouched.
GREETING = env("GREETING", default="Hy kanha")

# Without a token there is nothing to call, so the API says so plainly instead of forwarding
# an empty bearer and reporting the proxy's 401 back to the user.
CONFIGURED = bool(QWEN_TOKEN)
NOT_CONFIGURED = ("the bridge is not configured: set QWEN_TOKEN on this service to a "
                  "Qwen access token from chat.qwen.ai")


def greet(messages: list) -> list:
    """Address the model before the newest question: "make me this" -> "Hy kanha make me this".

    Only the last user turn is touched. The earlier ones already carry the greeting, since it
    was applied when they were sent, and a caller that writes the greeting itself is not
    prefixed twice. Internal turns (the refine instruction) never go through this.
    """
    if not GREETING:
        return messages
    for index in range(len(messages) - 1, -1, -1):
        turn = messages[index]
        if turn.get("role") != "user" or not isinstance(turn.get("content"), str):
            continue
        text = turn["content"].strip()
        if not text or text.lower().startswith(GREETING.lower()):
            return messages
        out = list(messages)
        out[index] = dict(turn, content=f"{GREETING} {turn['content'].lstrip()}")
        return out
    return messages


# --- the turns of a conversation --------------------------------------------------------

ROLES = ("system", "user", "assistant", "tool", "function")


def clean_messages(raw) -> list:
    """The caller's turns, as the provider wants them: a role and some content."""
    if not isinstance(raw, list):
        raise HTTPException(400, "messages must be a list")
    turns = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip() not in ROLES:
            continue
        if item.get("content") is None:
            continue
        turns.append(item)
    if not turns:
        raise HTTPException(400, "messages must contain at least one turn with content")
    return turns


def trim_messages(messages: list) -> list:
    """Keep the newest turns inside the history budget.

    `head` is what is never dropped: the system message a caller put in front. Everything
    after it is a turn, and the oldest ones go first once the conversation is longer than
    HISTORY_MESSAGES or fatter than HISTORY_CHARS. The draft and the refine instruction are
    appended after this, so the turn being worked on is never the one that gets dropped.
    """
    head = 0
    while head < len(messages) and messages[head].get("role") == "system":
        head += 1
    kept = list(messages)

    def size() -> int:
        return sum(len(m["content"]) for m in kept if isinstance(m.get("content"), str))

    while (len(kept) - head > HISTORY_MESSAGES or size() > HISTORY_CHARS) and len(kept) - head > 2:
        kept.pop(head)
    return kept


def last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def without_greeting(text: str) -> str:
    """The question as it was typed, without the greeting the model was addressed with.

    Quoting it back to the reviewer with "Hy kanha" in front of it adds nothing and reads like
    part of the request, so the reviewer is shown the question the user actually asked.
    """
    stripped = (text or "").strip()
    if GREETING and stripped.lower().startswith(GREETING.lower()):
        return stripped[len(GREETING):].lstrip()
    return stripped


def asked_for(messages: list, count: int = 3) -> list:
    """The newest user turns -- the request the draft is being judged against."""
    out = [m["content"] for m in messages
           if m.get("role") == "user" and isinstance(m.get("content"), str)]
    return out[-count:]


# --- secrets never leave for the reviewer ------------------------------------------------

SECRET_PATTERNS = [
    re.compile(r"hf_[A-Za-z0-9]{12,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)\b\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
]


def redact(text: str) -> tuple:
    """Mask credentials before a script goes to a second company.

    A generated script often carries a webhook, a token or an asset key. The reviewer does
    not need any of them to say whether the code works, and the second provider is one more
    place they would end up. Returns the masked text and how many things were masked.
    """
    count = 0
    for pattern in SECRET_PATTERNS:
        text, found = pattern.subn("<redacted>", text)
        count += found
    return text, count


# --- reading the draft -------------------------------------------------------------------

FENCE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*\n(.*?)\n?```\s*$", re.S)


def strip_fences(text: str) -> str:
    """A whole answer wrapped in one ``` block is unwrapped; anything else is left alone."""
    match = FENCE.match(text or "")
    return match.group(1).strip() if match else (text or "").strip()


def structural_notes(text: str, finish: Optional[str]) -> tuple:
    """What can be checked without running the script, and whether it is worth shipping.

    This is deliberately a *structural* check, not a verdict on correctness: it catches the
    failures that make everything after it pointless -- an empty answer, one cut off by the
    token ceiling, an unterminated fence -- and hands the rest to the reviewer as evidence
    instead of pretending a text model can verify code.
    """
    code = (text or "").strip()
    notes = []
    if not code:
        notes.append("the answer is empty")
    if finish == "length":
        notes.append("the answer was cut off by the token ceiling (finish_reason=length)")
    if finish == "content_filter":
        notes.append("the provider stopped it with a content filter")
    if code.count("```") % 2:
        notes.append("a markdown code fence is left open")
    usable = bool(code) and finish not in ("length", "content_filter")
    return notes, usable


# --- what the provider said, when it said no -------------------------------------------

def failure_reason(status: int, body: str, provider: Provider) -> str:
    """The provider's own sentence for a failure, plus the one fix that is not obvious.

    Whatever the endpoint rejected the call with is the useful part, so it is passed through
    verbatim instead of being replaced by a generic message.
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
    model = provider.model
    who = provider.name
    if status == 401:
        if who != "qwen":
            return (f"{who} rejected the key ({message}) -- put an API key from "
                    f"platform.deepseek.com in DEEPSEEK_TOKEN on this service (a "
                    "chat.deepseek.com session token is not an API key)")
        return (f"QWEN_TOKEN was rejected by qwen-api ({message}) -- copy a fresh token from "
                "chat.qwen.ai (DevTools console: localStorage.token) and update the variable")
    if status == 403:
        return f"{who} refused the request ({message})"
    if status == 404:
        return f"{model} is not a model {who} serves ({message})"
    if status == 429:
        return (f"{who} is rate limiting ({message}) -- retry shortly, or point REVIEW_MODEL/"
                "QWEN_MODEL at another model")
    if status >= 500:
        return f"{who} is failing ({status}: {message})"
    return f"{who} {status}: {message}"


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


# --- the model list, and whether the tokens still work ----------------------------------

_models: dict = {"at": 0.0, "ids": []}
_models_lock = threading.Lock()


def list_models(force: bool = False) -> list:
    """qwen-api's model ids, remembered for a few minutes; empty when unreadable.

    Never raises: it feeds a health chip and documents what else QWEN_MODEL could be, so an
    unreachable proxy must leave the page usable rather than break it.
    """
    with _models_lock:
        cached = dict(_models)
    if not force and cached["at"] and time.time() - cached["at"] < 300:
        return cached["ids"]
    ids: list = []
    if CONFIGURED:
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as c:
                r = c.get(f"{QWEN_URL}/models", headers=QWEN.headers())
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

    Qwen access tokens expire, and an expired one is otherwise indistinguishable from a hung
    generation, so the chip reports it before you send anything.
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
                # The token goes in the body (as qwen-api documents) and as a bearer, since
                # some builds authenticate every route either way.
                r = c.post(f"{QWEN_ROOT}/validate", json={"token": QWEN_TOKEN},
                           headers=QWEN.headers())
            detail = (r.text or "")[:300]
            if r.status_code == 404:
                # A qwen-api build without /validate; generations still report for real.
                state = {"at": time.time(), "ok": True, "detail": "token set"}
            elif r.status_code >= 400:
                state = {"at": time.time(), "ok": False,
                         "detail": failure_reason(r.status_code, detail, QWEN)}
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


_reviewer: dict = {"at": 0.0, "ok": False, "detail": "not checked"}
_reviewer_lock = threading.Lock()


def _reviewer_probe(force: bool = False) -> dict:
    """Whether the reviewer key works and the model exists, remembered briefly.

    A retired model id and a rejected key look exactly alike from the page (the chain just
    never refines), so the reviewer is checked the same way the Qwen token is.
    """
    with _reviewer_lock:
        cached = dict(_reviewer)
    if not force and cached["at"] and time.time() - cached["at"] < TOKEN_CHECK_TTL:
        return cached
    if not REVIEWER.configured:
        state = {"at": time.time(), "ok": False, "detail": "no reviewer key set"}
    else:
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as c:
                r = c.get(f"{REVIEWER.url}/models", headers=REVIEWER.headers())
            if r.status_code == 404:
                state = {"at": time.time(), "ok": True, "detail": "key set"}
            elif r.status_code >= 400:
                state = {"at": time.time(), "ok": False,
                         "detail": failure_reason(r.status_code, r.text[:300], REVIEWER)}
            else:
                seen: list = []
                try:
                    payload = r.json()
                    for item in (payload.get("data") or []):
                        if isinstance(item, dict) and item.get("id"):
                            seen.append(str(item["id"]))
                        elif isinstance(item, str):
                            seen.append(item)
                except ValueError:
                    pass
                if not seen:
                    state = {"at": time.time(), "ok": True, "detail": "key set"}
                elif REVIEWER.model in seen:
                    state = {"at": time.time(), "ok": True, "detail": "key set, model served"}
                else:
                    near = [m for m in seen if "flash" in m.lower() or REVIEWER.model.split("-")[0] in m]
                    hint = near[0] if near else (seen[0] if seen else "")
                    state = {"at": time.time(), "ok": False,
                             "detail": (f"{REVIEWER.model} is not served"
                                        + (f" -- try {hint}" if hint else ""))}
        except httpx.HTTPError as e:
            state = {"at": time.time(), "ok": False,
                     "detail": f"cannot reach {REVIEWER.url} ({e.__class__.__name__})"}
    with _reviewer_lock:
        _reviewer.update(state)
    return state


def reviewer_state(force: bool = False) -> dict:
    """Whether the reviewer can be relied on right now.

    The key working is only half of it: a review that just failed (down, rate limited, out of
    credits) is the more useful answer, and it is the failure that would otherwise look like a
    reviewer with nothing to say -- the chain still ships the draft either way. Keeping it
    here rather than in the caller means every report of the reviewer's state carries it.
    """
    state = _reviewer_probe(force)
    note = review_note()
    if note and state["ok"]:
        state = {"at": state["at"], "ok": False, "detail": note}
    return {**state, "shape": REVIEW_SHAPE, "search": not SEARCH_OFF,
            "last_note": note, "brief_chars": len(BRIEF), "brief": BRIEF_PATH.name,
            "items": REVIEW_ITEMS}


# --- jobs ------------------------------------------------------------------------------

class Job:
    """One turn, owned by a background thread rather than by the caller.

    A phone that gave up on an answer used to take the whole generation with it: the request
    was the only thing driving the model, so nothing was left to read. A job runs to
    completion on its own thread, keeps every piece it has produced, and any number of
    readers can attach to it -- including one that comes back after the connection dropped,
    which replays the output from the start and follows along.

    Text arrives on three channels, because there are three things to show: the draft, the
    reviewer's list, and the answer that comes back after it.
    """

    def __init__(self, messages: list, temperature: Optional[float], note: str, review: bool):
        self.id = uuid.uuid4().hex[:12]
        self.messages = messages
        self.temperature = temperature
        self.note = note
        self.want_review = review
        self.pieces: list = []          # (channel, piece)
        self.buffers: dict = {"draft": [], "review": [], "answer": []}
        self.error = ""
        self.status = "queued"          # queued -> running -> done | error
        self.phase = "queued"           # queued | draft | check | review | refine | done
        self.phases: list = []          # one record per model call
        self.review_text = ""
        self.started = time.time()
        self.finished = 0.0
        self.cond = threading.Condition()

    def channel(self, name: str) -> str:
        return "".join(self.buffers.get(name, []))

    def text(self) -> str:
        """What the caller asked for: the refined answer, or the draft when there is none."""
        return self.channel("answer") or self.channel("draft")

    def add(self, channel: str, piece: str) -> None:
        with self.cond:
            self.pieces.append((channel, piece))
            self.buffers.setdefault(channel, []).append(piece)
            self.cond.notify_all()

    def reset_channel(self, channel: str) -> None:
        """Throw away what a channel has produced so far.

        The one caller is the regression guard: a rewrite that came back cut off has already
        been streamed to whoever is reading, and it has to be replaced by the draft rather
        than shown above it. The reset is itself a piece, so a reader that attaches later
        replays the same sequence and ends up with the same text.
        """
        with self.cond:
            self.pieces.append((channel, None))
            self.buffers[channel] = []
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
        """Block until the job ends; the blocking endpoints are the only callers."""
        deadline = time.time() + timeout
        with self.cond:
            while self.status not in ("done", "error"):
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise HTTPException(504, f"timed out after {timeout:g}s waiting on the chain")
                self.cond.wait(timeout=remaining)

    def report(self) -> dict:
        """Where the job stands; what the page shows next to its running timer."""
        return {
            "status": self.status,
            "phase": self.phase,
            "note": self.note,
            "model": QWEN_MODEL,
            "reviewer": REVIEWER.model if self.want_review else "",
            "thinking": QWEN_THINKING,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "chars": len(self.text()),
        }


_jobs: dict = {}
_jobs_lock = threading.Lock()

# The last thing that went wrong, so /health (and the page's chip) can report it long after
# the error frame has scrolled by. Cleared by the next turn that succeeds.
_last_error = ""
_last_error_lock = threading.Lock()


def note_error(text: str) -> None:
    global _last_error
    with _last_error_lock:
        _last_error = text[:300]


def last_error() -> str:
    with _last_error_lock:
        return _last_error


# A reviewer that fails is not a failed turn -- the draft still goes out -- but it is not
# nothing either: the review silently not happening looks exactly like a reviewer with
# nothing to say. So it gets its own note, shown on the reviewer chip until a review works.
_review_note = ""
_review_note_lock = threading.Lock()


def note_review_error(text: str) -> None:
    global _review_note
    with _review_note_lock:
        _review_note = text[:300]


def review_note() -> str:
    with _review_note_lock:
        return _review_note


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
        raise HTTPException(404, "unknown or expired job; send the turn again")
    return job


# --- how many people can do this at once -------------------------------------------------
#
# The page needs no login, so the URL is the only thing standing between a stranger and your
# Qwen account plus your DeepSeek credits. A key was the other option; this is what has to
# carry it instead: a per-IP window, and a ceiling on chains running at the same time.

_hits: dict = defaultdict(deque)
_hits_lock = threading.Lock()
_running = {"now": 0}
_running_lock = threading.Lock()


def client_ip(request: Optional[Request]) -> str:
    if request is None:
        return "?"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def rate_ok(ip: str) -> bool:
    """A sliding window per IP; RATE_LIMIT per minute, 0 disables it."""
    if RATE_LIMIT <= 0:
        return True
    now = time.time()
    with _hits_lock:
        window = _hits[ip]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= RATE_LIMIT:
            return False
        window.append(now)
        if len(_hits) > 5000:  # never let the bookkeeping itself become the leak
            for key in [k for k, v in _hits.items() if not v][:1000]:
                _hits.pop(key, None)
    return True


def slot_take() -> bool:
    with _running_lock:
        if _running["now"] >= MAX_CONCURRENT:
            return False
        _running["now"] += 1
        return True


def slot_give() -> None:
    with _running_lock:
        _running["now"] = max(0, _running["now"] - 1)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Nothing to load or warm: there are no local weights, only API calls.
    if CONFIGURED:
        print(f"[bridge] {QWEN_URL} -> {QWEN_MODEL} (thinking: {QWEN_THINKING})", flush=True)
    else:
        print(f"[bridge] {NOT_CONFIGURED}", flush=True)
    if review_enabled():
        print(f"[review] {REVIEWER.url} -> {REVIEWER.model} (thinking: {REVIEW_THINKING}), "
              f"brief {len(BRIEF)} chars from {BRIEF_PATH if BRIEF else 'the built-in rubric'}",
              flush=True)
    else:
        print("[review] no reviewer configured; answers are sent as the model writes them",
              flush=True)
    if GREETING:
        print(f"[chat] every question is sent as {GREETING} <your question>", flush=True)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def require_key(x_api_key: Optional[str] = Header(None),
                authorization: Optional[str] = Header(None)) -> None:
    """Gate the *API* surfaces (/v1, /generate, /chat) when a key is defined.

    The key is API_KEY when it is set, and otherwise the Qwen token itself -- one secret to
    keep, as asked. The page is deliberately not gated (it is used without a login), so what
    protects the service is the rate limit and the concurrency ceiling, not this.

    This is the only credential a caller ever handles: the Qwen token and the reviewer key
    stay here, so they never reach a browser, a Roblox client or a log.
    """
    if not CALLER_KEY:
        return
    supplied = x_api_key or ""
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, CALLER_KEY):
        raise HTTPException(401, "missing or invalid API key (send it as the X-API-Key header)")


# --- talking to a provider --------------------------------------------------------------

def stream_answer(messages: list, temperature: Optional[float], provider: Provider,
                  max_tokens: int, box: Optional[dict] = None):
    """Stream an answer, piece by piece, out of a provider's /chat/completions.

    `box` gets the finish reason and any token usage, which is how a truncated answer is
    caught instead of being shipped.
    """
    body = provider.request(messages, temperature, max_tokens, stream=True)
    try:
        with httpx.Client(timeout=httpx.Timeout(provider.timeout, connect=10.0),
                          follow_redirects=True) as c:
            with c.stream("POST", provider.endpoint(), json=body, headers=provider.headers()) as r:
                if r.status_code >= 400:
                    detail = r.read().decode("utf-8", "replace")
                    raise HTTPException(502, failure_reason(r.status_code, detail, provider))
                if "event-stream" not in r.headers.get("content-type", ""):
                    # Not a stream: either the endpoint rejected the request with a 200, or it
                    # ignored stream=true and answered in one piece. Reading the body tells us
                    # which; reporting an empty answer would hide the reason.
                    raw = r.read().decode("utf-8", "replace")
                    text = message_text(raw)
                    if not text:
                        raise HTTPException(502, failure_reason(200, raw, provider))
                    print(f"[{provider.name}] answered in one piece instead of streaming", flush=True)
                    if box is not None:
                        box["finish"] = box.get("finish") or "stop"
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
                    if box is not None and isinstance(chunk, dict) and chunk.get("usage"):
                        box["usage"] = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0] or {}
                    if box is not None and choice.get("finish_reason"):
                        box["finish"] = choice["finish_reason"]
                    # reasoning_content is deliberately skipped: the answer is what is wanted.
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece:
                        yield piece
    except httpx.HTTPError as e:
        raise upstream_error(e, provider)


def upstream_error(e: httpx.HTTPError, provider: Provider) -> HTTPException:
    """Map an httpx failure onto a status the caller can act on."""
    if isinstance(e, httpx.TimeoutException):
        return HTTPException(504, f"{provider.label()} timed out after {provider.timeout:g}s")
    return HTTPException(502, f"cannot reach {provider.endpoint()} ({e.__class__.__name__})")


def call_once(messages: list, temperature: Optional[float], provider: Provider,
              max_tokens: int) -> str:
    """One non-streamed call, for the small internal jobs (the token checks)."""
    body = provider.request(messages, temperature, max_tokens, stream=False)
    with httpx.Client(timeout=httpx.Timeout(provider.timeout, connect=10.0),
                      follow_redirects=True) as c:
        r = c.post(provider.endpoint(), json=body, headers=provider.headers())
    if r.status_code >= 400:
        raise HTTPException(502, failure_reason(r.status_code, r.text, provider))
    return message_text(r.text)


# --- the chain itself --------------------------------------------------------------------

def run_phase(job: Job, messages: list, temperature: Optional[float], provider: Provider,
              max_tokens: int, channel: str, phase: str, note: str) -> tuple:
    """Stream one model call into a channel and record what it cost.

    Every call in the chain goes through here, so every call ends up in the job's phase
    record: which model, how long, how many characters, and whether it stopped early. Without
    that, "is this more reliable?" is not a question anyone can answer.
    """
    job.finish(phase=phase, note=note)
    box: dict = {"finish": None, "usage": None}
    started = time.time()
    pieces: list = []
    for piece in stream_answer(messages, temperature, provider, max_tokens, box):
        pieces.append(piece)
        job.add(channel, piece)
    text = "".join(pieces)
    record = {
        "phase": phase,
        "provider": provider.name,
        "model": provider.model,
        "ms": int((time.time() - started) * 1000),
        "chars": len(text),
        "finish": box["finish"],
        "usage": box["usage"],
    }
    job.phases.append(record)
    print(f"[job] {job.id} {phase}: {provider.model} {record['ms']}ms, {record['chars']} chars, "
          f"finish={record['finish']}", flush=True)
    return text, record


def review_messages(job: Job, draft: str, notes: list) -> list:
    """The reviewer's whole brief: what was asked, what came back, and what was checked."""
    brief = BRIEF or "You are a senior reviewer of generated code."
    checked = "\n".join(f"- {n}" for n in notes) if notes else "- nothing flagged"
    script, masked = redact(draft)
    if len(script) > REVIEW_SCRIPT_MAX:
        script = script[:REVIEW_SCRIPT_MAX] + "\n-- [script truncated for review] --"
    asked = asked_for(job.messages)
    request_text = "\n".join(f"- {without_greeting(t)[:2000]}" for t in asked) or "(none)"
    user = f"""REQUEST (what the user asked for):
{request_text}

TARGET: {TARGET_RUNTIME}

DRAFT FROM THE OTHER MODEL:
{script}

AUTOMATIC CHECKS ALREADY RUN:
{checked}

Review the draft against the request above."""
    if masked:
        print(f"[job] {job.id} review: {masked} secret(s) masked before sending", flush=True)
    return [
        {"role": "system", "content": brief + "\n\n" + RUBRIC},
        {"role": "user", "content": user},
    ]


def parse_review(text: str) -> tuple:
    """The reviewer's verdict and its numbered points, as the refine step needs them.

    The format is what makes the chain work: prose reviews cannot be applied, a numbered list
    can. If the reviewer ignored the format, its answer is passed on whole rather than being
    thrown away.
    """
    body = (text or "").strip()
    verdict = "ISSUES"
    match = re.search(r"(?im)^\s*VERDICT\s*[:=]\s*([A-Za-z]+)", body)
    if match:
        verdict = "OK" if match.group(1).strip().upper().startswith("OK") else "ISSUES"
        body = body[match.end():].strip()
    items: list = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        numbered = re.match(r"^(\d+)\s*[.)]\s*(.+)$", stripped)
        if numbered:
            items.append(f"{numbered.group(1)}. {numbered.group(2).strip()}")
        elif items:
            items[-1] = f"{items[-1]} {stripped}"
        else:
            items.append(stripped)
    items = [i for i in items if len(i) > 3][:REVIEW_ITEMS]
    if verdict == "OK":
        return verdict, []
    if not items:
        # No list, but not an okay verdict: hand over whatever it said rather than nothing.
        items = [body[:REVIEW_PASTE_MAX]] if body else []
    return verdict, items


def refine_instruction(items: list, notes: list) -> str:
    """What goes back into the same chat as the draft: the reviewer's list, and nothing more.

    The draft is already an assistant turn in this conversation, so the model is editing its
    own work rather than starting again -- which is the whole reason the refine step happens
    in the same chat the script was written in.
    """
    listed = "\n".join(items)
    extra = ""
    if notes:
        extra = ("\nAutomatic checks also flagged:\n"
                 + "\n".join(f"- {n}" for n in notes) + "\n")
    return f"""A reviewer checked the script you just wrote. Apply exactly these points and change \
nothing else -- keep every part that already works, keep the same structure, do not rename \
anything that was not named here.

{listed}
{extra}
If a point is wrong, leave the code as it is and move on. Return the complete corrected script \
and nothing else: no explanation, no notes, no commentary, no markdown code fences."""


def run_job(job: Job) -> None:
    """Draft, check, review, refine -- on a thread of its own, so no reader can lose it."""
    if not slot_take():
        job.finish(status="error",
                   error=f"{MAX_CONCURRENT} chains are already running; try again shortly")
        return
    try:
        draft, _ = run_phase(job, job.messages, job.temperature, QWEN, DRAFT_TOKENS,
                             "draft", "draft", f"{QWEN.model} writing a draft")
        draft = strip_fences(draft)
        # The channel is what a reader sees and what a later reader replays, so the fences the
        # model wrapped the script in are dropped from it as well as from the answer.
        if job.channel("draft").strip() != draft:
            job.reset_channel("draft")
            job.add("draft", draft)
        notes, usable = structural_notes(draft, job.phases[-1]["finish"])
        if not usable:
            reason = "; ".join(notes) or "the model returned nothing"
            raise HTTPException(502, f"{reason} -- try again, or raise DRAFT_TOKENS")

        # Nothing is reviewed when nothing is configured or asked for: the draft is the answer,
        # and the answer channel carries it so a reader sees one stream either way.
        if not (job.want_review and review_enabled()):
            job.add("answer", draft)
            job.finish(status="done", phase="done", note=f"{QWEN.model} answered")
            note_error("")
            return

        job.finish(phase="check", note="checking the draft")
        review = ""
        try:
            review, _ = run_phase(job, review_messages(job, draft, notes), REVIEW_TEMPERATURE,
                                  REVIEWER, REVIEW_MAX_TOKENS, "review", "review",
                                  f"{REVIEWER.model} reviewing the draft")
            note_review_error("")
        except HTTPException as e:
            # A reviewer that is down, rate limited or out of credits must not cost the user
            # the draft that is already written. It is recorded instead, so the reviewer chip
            # shows it rather than looking like a review that found nothing.
            print(f"[job] {job.id} review failed: {e.detail}", flush=True)
            job.add("review", f"\n[the review did not happen: {e.detail}]\n")
            note_review_error(str(e.detail))
        job.review_text = review

        verdict, items = parse_review(review)
        if not items:
            job.add("answer", draft)
            note = (f"{REVIEWER.model} approved the draft" if review
                    else "no review, shipping the draft")
            job.finish(status="done", phase="done", note=note)
            note_error("")
            return

        # The refine turn is internal: it is not greeted, and it is not part of the history the
        # user's next question carries.
        refine_turns = list(job.messages) + [
            {"role": "assistant", "content": draft},
            {"role": "user", "content": refine_instruction(items, notes)},
        ]
        final, _ = run_phase(job, refine_turns, job.temperature, QWEN, REFINE_TOKENS,
                             "answer", "refine", f"{QWEN.model} applying the review")
        final = strip_fences(final)
        if job.channel("answer").strip() != final:
            job.reset_channel("answer")
            job.add("answer", final)
        shipped = final
        _, refined_ok = structural_notes(final, job.phases[-1]["finish"])
        if not refined_ok:
            # The regression guard: a refine that comes back worse than the draft it was
            # editing is discarded, and the draft is what gets shipped instead of it.
            print(f"[job] {job.id} the refined script was not usable; shipping the draft", flush=True)
            shipped = draft
            job.reset_channel("answer")
            job.add("answer", draft)
            job.phases.append({"phase": "guard", "provider": "bridge", "model": "",
                               "ms": 0, "chars": 0, "finish": "discarded the rewrite"})
        job.finish(status="done", phase="done",
                   note=f"{REVIEWER.model} found {len(items)} point(s); {QWEN.model} rewrote it")
        note_error("")
        print(f"[job] {job.id} done in {job.report()['elapsed']:g}s, {len(shipped)} chars",
              flush=True)
    except HTTPException as e:
        # Printed as well as sent: the page shows it once, the log keeps it.
        print(f"[job] {job.id} failed: {e.detail}", flush=True)
        note_error(str(e.detail))
        job.finish(status="error", phase="error", error=str(e.detail))
    except httpx.HTTPError as e:
        detail = upstream_error(e, QWEN).detail
        print(f"[job] {job.id} failed: {detail}", flush=True)
        note_error(detail)
        job.finish(status="error", phase="error", error=detail)
    except Exception as e:  # a bug here must never leave a reader waiting forever
        print(f"[job] {job.id} crashed: {e.__class__.__name__}: {e}", flush=True)
        note_error(f"{e.__class__.__name__}: {e}")
        job.finish(status="error", phase="error", error=f"{e.__class__.__name__}: {e}")
    finally:
        slot_give()


def start_job(messages: list, temperature: Optional[float], review: Optional[bool] = None,
              request: Optional[Request] = None) -> Job:
    """Address the newest question, then set the work going on its own thread."""
    if not CONFIGURED:
        raise HTTPException(503, NOT_CONFIGURED)
    ip = client_ip(request)
    if not rate_ok(ip):
        raise HTTPException(429, f"too many requests from {ip}; {RATE_LIMIT} per minute")
    turns = greet(trim_messages(messages))
    want = review_enabled() if review is None else bool(review and review_enabled())
    note = f"{QWEN.model} drafting"
    job = Job(turns, temperature, note, want)
    register(job)
    print(f"[job] {job.id} on {QWEN_MODEL}"
          f"{' + ' + REVIEWER.model if want else ''}: {len(turns)} turns, asking about "
          f"{last_user_text(turns).strip()[:60]!r}", flush=True)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


# --- the conversation endpoints --------------------------------------------------------

class GenReq(BaseModel):
    """One-shot request (client.lua and older callers): a prompt, no history."""

    prompt: str
    temperature: Optional[float] = 0.7
    review: Optional[bool] = None


class ChatReq(BaseModel):
    """One turn of a conversation.

    `messages` is the whole conversation the caller is keeping, oldest first. The newest user
    turn gets the greeting in front of it, and the list is what the model sees, so it answers
    in the same chat it has been answering in. `review: false` skips the reviewer for one turn.
    """

    messages: list
    temperature: Optional[float] = 0.7
    review: Optional[bool] = None


def job_summary(job: Job) -> dict:
    """A finished job as one object: the answer, plus what the reviewer said about the draft."""
    return {
        "job": job.id,
        "text": job.text(),
        "code": job.text(),
        "draft": job.channel("draft"),
        "review": job.review_text,
        "model": QWEN_MODEL,
        "reviewer": REVIEWER.model if job.want_review and review_enabled() else "",
        "thinking": QWEN_THINKING,
        "phases": job.phases,
        "elapsed": job.report()["elapsed"],
    }


@app.post("/chat/stream")
def chat_stream(req: ChatReq, request: Request):
    """Start one turn and hand back its job id immediately.

    Nothing is generated on this request, so it cannot hang and be dropped: the whole chain
    runs on the job's thread and the page watches /chat/stream/{job} instead. This is the
    endpoint the page uses, so it is not key-gated -- only rate limited.
    """
    messages = clean_messages(req.messages)
    job = start_job(messages, req.temperature, req.review, request)
    return {"job": job.id, "model": QWEN_MODEL, "reviewer": REVIEWER.model if job.want_review else "",
            "thinking": QWEN_THINKING, "turns": len(job.messages), "timeout": CHAT_TIMEOUT}


@app.post("/chat")
def chat(req: ChatReq, _: None = Depends(require_key)):
    """The same chain, blocking -- for callers that cannot follow a stream (client.lua)."""
    job = start_job(clean_messages(req.messages), req.temperature, req.review)
    job.wait(job_wait())
    if job.status == "error":
        raise HTTPException(502, job.error)
    return job_summary(job)


def frame(payload: dict) -> str:
    return json.dumps(payload) + "\n"


def job_frames(job: Job):
    """NDJSON for one reader: everything the job has so far, then each new piece.

    Every reader starts at zero, so a browser whose connection died just asks again and
    rebuilds the same answer while the job carries on. The frames in between matter even when
    there is nothing to report: a stream that goes silent for minutes is what a proxy or a
    sleeping phone drops, so the wait is punctuated with heartbeats -- and a chain runs three
    model calls, so there is a lot of waiting to punctuate.
    """
    index = 0
    last_phase = ""
    yield frame({"replay": True, "job": job.id, **job.report()})
    while True:
        with job.cond:
            # Wait only while there is nothing new and the job is still going: a finished job
            # is handed over at once rather than after a heartbeat.
            if not job.pieces[index:] and job.status in ("queued", "running"):
                job.cond.wait(timeout=HEARTBEAT)
            pieces = job.pieces[index:]
            index += len(pieces)
            report = job.report()
            status, error = job.status, job.error
        for channel, piece in pieces:
            if piece is None:
                yield frame({"ch": channel, "reset": True})
            else:
                yield frame({"t": piece, "ch": channel})
        if report["phase"] != last_phase:
            last_phase = report["phase"]
            yield frame({"phase": last_phase, "note": report["note"], **report})
        if status == "error":
            yield frame({"error": error, "text": job.text()})
            return
        if status == "done":
            yield frame({"done": True, "text": job.text(), "code": job.text(),
                         "draft": job.channel("draft"), "review": job.review_text,
                         "phases": job.phases, **report})
            return
        if not pieces:
            yield frame({"beat": True, **report})


@app.get("/chat/stream/{job_id}")
@app.get("/generate/stream/{job_id}")
@app.get("/job/{job_id}")
def watch_stream(job_id: str):
    """Stream a job's progress. Calling it again after a drop is the whole point."""
    job = lookup(job_id)
    return StreamingResponse(
        job_frames(job),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/chat/result/{job_id}")
def job_result(job_id: str, _: None = Depends(require_key)):
    """Where a job stands, as one JSON object.

    A client that cannot hold a stream open -- Roblox's HttpService reads a response in one
    piece, so it cannot follow NDJSON -- starts the turn on /chat/stream and polls this. The
    same fields as the terminal frame, plus the running state, so progress is visible instead
    of one silent wait while three model calls happen.
    """
    job = lookup(job_id)
    body = job_summary(job) if job.status in ("done", "error") else {
        "job": job.id,
        "phase": job.phase,
        "note": job.note,
        "draft": job.channel("draft"),
        "review": job.review_text,
        "model": QWEN_MODEL,
        "reviewer": REVIEWER.model if job.want_review and review_enabled() else "",
        "phases": job.phases,
    }
    body.update(job.report())
    if job.status == "error":
        body["error"] = job.error
    return body


def job_wait() -> float:
    """How long a blocking caller waits: a chain is three calls, so three windows."""
    phases = 3 if review_enabled() else 1
    return CHAT_TIMEOUT * phases + 30


@app.post("/generate")
def generate(req: GenReq, _: None = Depends(require_key)):
    """Block until the whole chain is done -- this is the path `client.lua` uses."""
    job = start_job([{"role": "user", "content": req.prompt}], req.temperature, req.review)
    job.wait(job_wait())
    if job.status == "error":
        raise HTTPException(502, job.error)
    return job_summary(job)


@app.post("/generate/stream")
def start_stream(req: GenReq, _: None = Depends(require_key)):
    """The one-shot flow as a job, for callers that stream but keep no history."""
    job = start_job([{"role": "user", "content": req.prompt}], req.temperature, req.review)
    return {"job": job.id, "model": QWEN_MODEL, "timeout": CHAT_TIMEOUT}


# --- the OpenAI-compatible surface -----------------------------------------------------

def relay_stream(body: dict):
    """Pass qwen-api's SSE through untouched.

    Nothing is rewritten on this path, which is what keeps the rest of qwen-api working
    through the bridge: reasoning_content, tool calls, web-search annotations and the hidden
    continuation metadata all survive, so an OpenAI client that manages its own history keeps
    working exactly as it would against Qwen itself. No reviewer runs here: this surface is a
    passthrough, and a caller that wants the chain uses /chat.
    """
    with httpx.Client(timeout=httpx.Timeout(CHAT_TIMEOUT, connect=10.0),
                      follow_redirects=True) as c:
        with c.stream("POST", QWEN.endpoint(), json=body, headers=QWEN.headers()) as r:
            if r.status_code >= 400:
                yield sse_error(failure_reason(r.status_code, r.read().decode("utf-8", "replace"), QWEN))
                return
            if "event-stream" not in r.headers.get("content-type", ""):
                raw = r.read().decode("utf-8", "replace")
                text = message_text(raw)
                if not text:
                    yield sse_error(failure_reason(200, raw, QWEN))
                    return
                yield sse({"id": "chatcmpl-bridge", "object": "chat.completion.chunk",
                           "model": QWEN_MODEL,
                           "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                        "finish_reason": None}]})
                yield "data: [DONE]\n\n"
                return
            for chunk in r.iter_bytes():
                yield chunk


@app.post("/v1/chat/completions")
def chat_completions(payload: dict = Body(...), _: None = Depends(require_key)):
    """The bridge itself: OpenAI-compatible, token injected, streaming preserved.

    A caller sends exactly what it would send to OpenAI. The newest user turn gets the greeting
    in front of it and the model is pinned to QWEN_MODEL. Everything else -- tools,
    web_search_options, reasoning_effort, temperature, stream -- passes through.
    """
    if not CONFIGURED:
        raise HTTPException(503, NOT_CONFIGURED)
    body = dict(payload)
    messages = trim_messages(clean_messages(payload.get("messages")))
    body["messages"] = greet(messages)
    asked = str(body.get("model") or "").strip()
    if asked and asked != QWEN_MODEL:
        print(f"[bridge] {asked} requested, using {QWEN_MODEL} (pinned)", flush=True)
    body["model"] = QWEN_MODEL
    body["thinking_mode"] = QWEN_THINKING
    if MAX_TOKENS > 0 and "max_tokens" not in body and "max_completion_tokens" not in body:
        body["max_tokens"] = MAX_TOKENS
    if body.get("stream"):
        return StreamingResponse(relay_stream(body), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    try:
        with httpx.Client(timeout=httpx.Timeout(CHAT_TIMEOUT, connect=10.0),
                          follow_redirects=True) as c:
            r = c.post(QWEN.endpoint(), json=body, headers=QWEN.headers())
    except httpx.HTTPError as e:
        raise upstream_error(e, QWEN)
    if r.status_code >= 400:
        raise HTTPException(502, failure_reason(r.status_code, r.text, QWEN))
    return Response(r.content, media_type="application/json")


@app.get("/v1/models")
def models(_: None = Depends(require_key)):
    """The models this bridge uses, and what else the proxy serves, for reference."""
    ids = [QWEN_MODEL]
    if review_enabled():
        ids.append(REVIEWER.model)
    for mid in list_models():
        if mid not in ids:
            ids.append(mid)
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": mid, "object": "model", "created": now,
         "owned_by": "deepseek" if mid == REVIEWER.model else "qwen"} for mid in ids
    ]}


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
        chip(state["bridge"], "bridge",
             state["last_error"] or (state["provider_label"] if state["bridge"]
                                     else "QWEN_TOKEN is not set")),
        chip(state["token_ok"], "token", state["token_detail"]),
        chip(state["reviewer"]["ok"], "reviewer",
             state["reviewer"]["detail"] or state["reviewer"]["model"]),
        chip(True, "model", state["model"]),
        chip(True, "mode", f"{state['thinking']} · {state['greeting']}" if state["greeting"]
             else state["thinking"]),
    ])
    try:
        page = INDEX.read_text(encoding="utf-8")
    except OSError:
        # The API stays usable if the static page was not copied into the image.
        return HTMLResponse("<h1>bahs</h1><p>web/index.html is missing; use /health and /docs.</p>")
    return HTMLResponse(
        page.replace("__CHIPS__", chips)
            .replace("__MODEL__", html.escape(state["model"]))
            .replace("__REVIEWER__", html.escape(state["reviewer"]["model"] if state["reviewer"]
                                                 and state["reviewer"].get("model") else "reviewer"))
            .replace("__REVIEW_ON__", "true" if state["review"] else "false")
            .replace("__GREETING__", html.escape(GREETING))
    )


async def snapshot() -> dict:
    # Always reports rather than raising, so the platform healthcheck only depends on the API
    # being up; token, reviewer and model readiness come back in the body. The checks are
    # network calls, so they run off the event loop: /health is polled every few seconds and
    # must never hold up a turn.
    state, rev = await asyncio.gather(asyncio.to_thread(token_state),
                                      asyncio.to_thread(reviewer_state))
    body = {
        "status": "ok",
        "bridge": CONFIGURED,
        "endpoint": QWEN_URL,
        "provider_label": QWEN.label(),
        "model": QWEN_MODEL,
        "thinking": QWEN_THINKING,
        "greeting": GREETING,
        "token_ok": state["ok"],
        "token_detail": state["detail"],
        "review": review_enabled(),
        "reviewer": {**rev, "model": REVIEWER.model, "endpoint": REVIEW_URL,
                     "configured": REVIEWER.configured, "thinking": REVIEW_THINKING,
                     "brief_chars": len(BRIEF), "items": REVIEW_ITEMS},
        "limits": {"per_minute": RATE_LIMIT, "concurrent": MAX_CONCURRENT,
                   "running": _running["now"]},
        "last_error": last_error(),
        "api_key_required": bool(CALLER_KEY),
    }
    if not CONFIGURED:
        body["status"] = "degraded"
        body["error"] = NOT_CONFIGURED
    elif not review_enabled():
        body["status"] = "partial"
    return body


@app.get("/health")
async def health():
    return await snapshot()
