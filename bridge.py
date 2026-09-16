"""Everything that talks to the model: the token, the config, the request, and its stream.

One model, one role. Qwen (qwen3.8-max, thinking on) writes the Luau, and the work a second
reader used to do -- checking the script, looking up the API, running it, patching it -- is now a
toolbox the writer calls itself (see luau.py).

    you -- ask --> bahs -- one call, thinking on --> qwen3.8-max
                     |                                   |
                     |                                   +-- calls a tool  -> luau.py
                     |                                   +-- reads the result, on it goes
                     |
                     +-- the same upstream chat, always: the proxy's hidden
                         `<!-- qwen_metadata: ... -->` is kept for the session, so a follow-up
                         continues the chat the last answer came from instead of opening a new one.

chat.qwen.ai has no public API. github.com/encryptarun/qwen-api turns it into
OpenAI-compatible endpoints using the Qwen *access token* from the browser
(chat.qwen.ai -> DevTools console -> localStorage.token). That token is the key to a whole Qwen
account, so it lives here and never in a page or a Roblox script.

Three things about that proxy shape this file:

  * **Thinking is a request field** (`thinking_mode: "thinking"`), not a hint, and its
    `reasoning_content` deltas are dropped here: the answer is what is wanted, and a reasoning
    delta is not the answer.
  * **Tool calling is prompt-engineered upstream**, but arrives as OpenAI `tool_calls` (and, in
    some builds, as a `<tool_calls>[...]</tool_calls>` block inside the content). Both are read
    here, and neither is ever allowed into the text the user sees.
  * **A chat is continued by the hidden metadata the proxy puts in the assistant content**
    (`<!-- qwen_metadata: {"response_id":...} -->`). Keeping it, and putting it back on the
    assistant turn of the next request, is the difference between one chat and a new chat per
    question. That is what the session store below is for, and why the marker is cut out of
    everything that is streamed, stored or displayed.

The service's surface (jobs, endpoints, the page) is in server.py; what the service can say about
itself is in state.py.
"""
# The shared imports live here and nowhere else: `state`, `luau` and `server` are written as
# `from bridge import *`, so these names are the plumbing all four modules are made of. They are
# not re-exported for convenience -- moving them would break the other three.
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request  # noqa: F401
from fastapi.middleware.cors import CORSMiddleware  # noqa: F401
from fastapi.responses import HTMLResponse, Response, StreamingResponse  # noqa: F401
from pydantic import BaseModel  # noqa: F401
from typing import Optional
from contextlib import asynccontextmanager  # noqa: F401
from pathlib import Path
from collections import defaultdict, deque  # noqa: F401
import asyncio, hmac, html, httpx, json, os, re, threading, time, uuid  # noqa: F401

# --- plumbing -------------------------------------------------------------------------


def client_timeout(seconds: float) -> httpx.Timeout:
    """How long a call may take: as long as the model needs, by default.

    `seconds` of 0 or less means no limit on the answer. Connecting is always bounded, so a host
    that cannot be reached still fails in seconds rather than sitting there looking like a model
    that is thinking.
    """
    if seconds and seconds > 0:
        return httpx.Timeout(seconds, connect=10.0)
    return httpx.Timeout(None, connect=10.0)


def env(*names: str, default: str = "") -> str:
    """The first of these variables that is set, so an old name keeps working."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


# --- the provider -----------------------------------------------------------------------

class Provider:
    """One OpenAI-compatible endpoint, and how it spells this service's two toggles."""

    def __init__(self, name: str, url: str, key: str, model: str, dialect: dict,
                 timeout: float, extra: Optional[dict] = None):
        self.name = name
        self.url = url.rstrip("/")
        self.key = key
        self.model = model
        self.dialect = dialect
        self.timeout = timeout
        self.extra = extra or {}

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
                stream: bool = True, tools: Optional[list] = None,
                tool_choice: Optional[str] = None) -> dict:
        """An OpenAI-shaped body, pinned to this provider's model and thinking mode.

        The model is not the caller's to choose: one model, one behaviour, so a request behaves
        the same whoever sends it.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            **self.dialect,
            **self.extra,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens > 0:
            body["max_tokens"] = max_tokens
        # What goes out is logged with what comes back: without it, a short answer, a request that
        # never carried the conversation, and a provider that stopped early all look the same
        # afterwards. The token check does not come through here (stream=False), so this is one
        # line per model call.
        if stream and env("LOG_REQUESTS", default="1") != "0":
            asked = sum(len(m.get("content") or "") for m in body["messages"]
                        if isinstance(m, dict) and isinstance(m.get("content"), str))
            print(f"[upstream] {self.name} -> {self.model}: {len(body['messages'])} message(s), "
                  f"{asked} chars, max_tokens {body.get('max_tokens', 'unset')}"
                  + (f", {len(tools)} tool(s)" if tools else ""), flush=True)
        return body


def qwen_token() -> str:
    """The Qwen access token, under whichever name it was put in the variables."""
    return env("QWEN_TOKEN", "QWEN_API_KEY", "QWEN_ACCESS_TOKEN")


QWEN_URL = env("QWEN_URL", default="https://qwen.aikit.club/v1")
# qwen-api also serves its own bookkeeping endpoints (/validate) at the root.
QWEN_ROOT = QWEN_URL[: -len("/v1")] if QWEN_URL.endswith("/v1") else QWEN_URL

QWEN_TOKEN = qwen_token()
# Every call goes to this model. There is no picker: one model, one behaviour.
QWEN_MODEL = env("QWEN_MODEL", default="qwen3.8-max")
# fast (answer straight away) | auto | thinking. Thinking is the default and stays that way: the
# writer is producing a whole Luau script and reasoning about an API it cannot see, and it costs
# nothing in the answer -- the stream's reasoning_content is dropped, so only the script is read.
QWEN_THINKING = env("QWEN_THINKING", default="thinking")
# No ceiling on a call by default: a long answer with tools is not an error, so nothing here cuts
# a model off for taking its time. 0 (or less) means no limit at all -- connecting is still
# bounded to 10s, so an unreachable host fails in seconds instead of looking like a slow model.
CHAT_TIMEOUT = float(env("CHAT_TIMEOUT", default="0"))

QWEN = Provider("qwen", QWEN_URL, QWEN_TOKEN, QWEN_MODEL,
                {"thinking_mode": QWEN_THINKING}, CHAT_TIMEOUT)

# --- one chat, not a new one per question ------------------------------------------------
#
# qwen-api continues the upstream chat when the request carries the hidden metadata it put in the
# last assistant answer:
#
#     <!-- qwen_metadata: {"response_id":"...","request_id":"..."} -->
#
# So the marker is worth more than the text around it. It is cut out of every stream this service
# reads (nobody should see it in a chat bubble), remembered against a session, and put back on the
# assistant turn of the next request in that session -- which is what makes the model answer in
# the chat it has been answering in rather than in a fresh one every time.

META_RE = re.compile(r"<!--\s*qwen_metadata:.*?-->", re.S)
TOOL_XML = re.compile(r"<tool_calls>\s*(.*?)\s*</tool_calls>", re.S)
# The two things that must never reach the text: a tool call in its XML shape, and the metadata.
MARKERS = (("<tool_calls>", "</tool_calls>"), ("<!--", "-->"))

_sessions: dict = {}
_sessions_lock = threading.Lock()


def session_ttl() -> float:
    return float(env("SESSION_TTL", default="3600") or 3600)


def session_new() -> str:
    return uuid.uuid4().hex[:12]


def session_remember(name: str, meta: str) -> None:
    """Keep the hidden continuation marker for a session. Empty means 'forget it'."""
    if not name:
        return
    with _sessions_lock:
        now = time.time()
        _sessions[name] = {"at": now, "meta": meta or ""}
        for old in [k for k, v in _sessions.items() if now - v["at"] > session_ttl()]:
            _sessions.pop(old, None)


def session_meta(name: str) -> str:
    with _sessions_lock:
        found = _sessions.get(name or "")
    return found["meta"] if found else ""


def session_count() -> int:
    with _sessions_lock:
        return len(_sessions)


def strip_metadata(text: str) -> str:
    """The text without the hidden continuation marker."""
    return META_RE.sub("", text or "").strip()


def with_continuation(messages: list, session: str) -> list:
    """The turns to send, with the session's continuation marker back on the last assistant turn.

    Every marker the caller may be carrying is dropped first (the page keeps what it streamed, and
    a marker sent twice would be text), and the session's own marker is put on the newest
    assistant turn -- where the proxy looks for it when it decides whether this is a follow-up.
    Without a session, or with one nothing is remembered for, the turns go out untouched.
    """
    out = []
    for message in messages:
        if message.get("role") == "assistant" and isinstance(message.get("content"), str) \
                and "qwen_metadata" in message["content"]:
            out.append({**message, "content": strip_metadata(message["content"])})
        else:
            out.append(message)
    meta = session_meta(session)
    if not meta:
        return out
    for index in range(len(out) - 1, -1, -1):
        if out[index].get("role") == "assistant":
            content = out[index].get("content")
            out[index] = {**out[index],
                          "content": ((content + "\n") if isinstance(content, str) and content
                                      else "") + meta}
            return out
    # A conversation with no assistant turn in it has nothing to continue from, and a marker sent
    # as a turn of its own would put an assistant message *after* the question being asked. Left
    # alone: this turn opens the upstream chat, the next one continues it.
    return out


# --- the toolbox ------------------------------------------------------------------------

# With tools on, this rides in front of the caller's conversation as the system turn. It is the
# only standing instruction the writer gets, and it is about the work, not about a persona.
TOOL_SYSTEM = """You write Luau that runs under a Roblox executor, injected into a live client. \
It is not a Roblox Studio place script and not a script for the Studio editor: there is no server \
side, no plugin API and no edit mode, and a script that assumes any of them is wrong.

You have tools, and using them is part of writing the script:
* `roblox_api` -- check that a class, property, function or event really exists before you rely on it.
* `luau_check` -- check the whole script for unbalanced blocks, unterminated strings and calls that break in an executor, before you hand it over.
* `run_script` -- run it in the executor that is listening, when one is, and read what it printed or how it failed. Nothing else can prove a script runs.
* `apply_edit` -- change one part of a script you already have instead of writing the whole thing again.
* `luau_format` -- re-indent a script you assembled from pieces.
* `secret_scan` -- find credentials in the script before it ships.

Call a tool when it would change your answer; never describe a call in prose. When you are done, \
answer with the complete runnable script and nothing else: no commentary, no summary of your \
changes, no markdown code fences."""

AGENT_ROUNDS_MAX = 12
# How many tool rounds one turn may take. A round is one model call plus the tools it asked for;
# a script that compiles on the second try needs three or four, and a turn nobody would sit
# through is not worth starting, so this is clamped rather than trusted.
AGENT_ROUNDS = max(0, min(int(env("AGENT_ROUNDS", default="8")), AGENT_ROUNDS_MAX))
# The tool that actually runs a script is the one that can hang (an executor stuck in a wait), so
# it is bounded separately.
TOOL_RESULT_MAX = int(env("TOOL_RESULT_MAX", default="20000"))

# --- the tokens one call may use ---------------------------------------------------------
#
# The writer exists to produce a whole script, and 4096 tokens is roughly 200 lines of Luau. An
# answer that stops at its ceiling is refused rather than shipped, so a ceiling that is too low
# shows up as a failed turn. The second number is the room a call that is mid-conversation gets,
# which is larger because it may be rewriting what was already handed to it.
MAX_TOKENS = int(env("MAX_TOKENS", default="4096"))
ANSWER_TOKENS = int(env("ANSWER_TOKENS", "DRAFT_TOKENS", default="8192"))
REFINE_TOKENS = int(env("REFINE_TOKENS", default="16384"))

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
# How often /health may ask the proxy whether its key still works.
TOKEN_CHECK_TTL = float(env("TOKEN_CHECK_TTL", default="60"))
# Requests per minute per IP on the endpoints the page uses, and how many turns may run at once.
# The page has no login by design, so these are what stand between the URL and the Qwen account
# behind it. RATE_LIMIT 0 disables the per-IP window.
RATE_LIMIT = int(env("RATE_LIMIT", default="30"))
MAX_CONCURRENT = int(env("MAX_CONCURRENT", default="4"))
# The key callers must send on /v1, /chat, /generate and the executor endpoints: API_KEY when it
# is set, otherwise the Qwen token itself -- one secret to keep. "" leaves those open as well.
API_KEY = env("API_KEY")
CALLER_KEY = API_KEY or QWEN_TOKEN

INDEX = Path(__file__).parent / "web" / "index.html"

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
    prefixed twice.
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
    """The caller's turns, as the provider wants them: a role and some content.

    A `tool` turn is kept as well: it is what a caller that manages its own tool calls has to be
    able to send back.
    """
    if not isinstance(raw, list):
        raise HTTPException(400, "messages must be a list")
    turns = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip() not in ROLES:
            continue
        if item.get("content") is None and not item.get("tool_calls"):
            continue
        turns.append(item)
    if not turns:
        raise HTTPException(400, "messages must contain at least one turn with content")
    return turns


def trim_messages(messages: list) -> list:
    """Keep the newest turns inside the history budget.

    `head` is what is never dropped: the system messages in front. Everything after them is a
    turn, and the oldest ones go first once the conversation is longer than HISTORY_MESSAGES or
    fatter than HISTORY_CHARS. The tool rounds of the turn being worked on are appended after
    this, so the turn in flight is never the one that gets dropped.
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
    """The question as it was typed, without the greeting the model was addressed with."""
    stripped = (text or "").strip()
    if GREETING and stripped.lower().startswith(GREETING.lower()):
        return stripped[len(GREETING):].lstrip()
    return stripped


# --- secrets never leave for a provider ---------------------------------------------------

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
    """Mask credentials, and say how many were masked.

    A generated script often carries a webhook, a token or an asset key. Nothing needs them to
    say whether the code works, and every place they are copied to is one more place they can
    leak from. Returns the masked text and the count of what was masked.
    """
    count = 0
    for pattern in SECRET_PATTERNS:
        text, found = pattern.subn("<redacted>", text)
        count += found
    return text, count


# --- reading the answer --------------------------------------------------------------------

FENCE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*\n(.*?)\n?```\s*$", re.S)


def strip_fences(text: str) -> str:
    """A whole answer wrapped in one ``` block is unwrapped; anything else is left alone."""
    match = FENCE.match(text or "")
    return match.group(1).strip() if match else (text or "").strip()


def looks_like_code(text: str) -> bool:
    """Whether what came back is a script rather than a sentence about one.

    Used to decide whether a tool call that carried its script in the content had one at all, so
    it is deliberately about structure -- most lines have to look like Lua -- rather than about
    the words in them.
    """
    body = (text or "").strip()
    if len(body) < 40:
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    markers = ("local ", "function", "end", "then", "do ", "else", "return", "print(",
               "Instance.", "game:", "script.", "require(", "task.", "wait(", "pcall", "--")
    hits = sum(1 for line in lines
               if line.startswith("--") or any(marker in line for marker in markers)
               or (("=" in line or "(" in line) and len(line) > 3))
    return hits >= max(2, (len(lines) * 2) // 3)


def structural_notes(text: str, finish: Optional[str]) -> tuple:
    """What can be checked without running the script, and whether it is worth shipping.

    This is deliberately *structural*, not a verdict on correctness: it catches the failures that
    make everything after it pointless -- an empty answer, one cut off by the token ceiling, an
    unterminated fence -- and hands the rest to `luau_check` and the executor, which are the only
    things that can say more.
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


# --- what the provider said, when it said no ---------------------------------------------

def failure_reason(status: int, body: str, provider: Provider) -> str:
    """The provider's own sentence for a failure, plus the one fix that is not obvious."""
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
    who = provider.name
    model = provider.model
    if status == 401:
        return (f"QWEN_TOKEN was rejected by qwen-api ({message}) -- copy a fresh token from "
                "chat.qwen.ai (DevTools console: localStorage.token) and update the variable")
    if status == 403:
        return f"{who} refused the request ({message})"
    if status == 404:
        return f"{model} is not a model {who} serves ({message})"
    if status == 429:
        return (f"{who} is rate limiting ({message}) -- retry shortly, or point QWEN_MODEL at "
                "another model")
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


def upstream_error(e: httpx.HTTPError, provider: Provider) -> HTTPException:
    """Map an httpx failure onto a status the caller can act on."""
    if isinstance(e, httpx.TimeoutException):
        if provider.timeout and provider.timeout > 0:
            return HTTPException(504, f"{provider.label()} timed out after {provider.timeout:g}s")
        return HTTPException(504, f"cannot connect to {provider.endpoint()} in time "
                                  f"({e.__class__.__name__})")
    return HTTPException(502, f"cannot reach {provider.endpoint()} ({e.__class__.__name__})")


# --- the call itself, and its stream -----------------------------------------------------

class Cutter:
    """Streams what the user should see out of a stream that also carries hidden things.

    Two things ride inside the assistant content and neither belongs in a chat bubble: the XML
    shape of a tool call, and the continuation metadata the proxy puts in every answer. They are
    cut out here -- including a marker that arrives split across two deltas, which is why the last
    few characters are always held back until they cannot still become one.
    """

    def __init__(self) -> None:
        self.raw = ""            # everything, exactly as it arrived
        self.emitted = 0         # how much of it has been handed out
        self.hold = ""           # the opener being held, "" when nothing is
        self.hold_at = 0

    def block_end(self) -> int:
        closer = dict(MARKERS)[self.hold]
        end = self.raw.find(closer, self.hold_at)
        return -1 if end < 0 else end + len(closer)

    def safe(self) -> int:
        """How much can be emitted without risking half a marker on screen."""
        window = max(len(open_) for open_, _ in MARKERS) - 1
        tail = self.raw[-window:] if window > 0 else ""
        keep = 0
        for opener, _ in MARKERS:
            for size in range(1, len(opener)):
                if tail.endswith(opener[:size]):
                    keep = max(keep, size)
        return max(self.emitted, len(self.raw) - keep)

    def take(self, upto: int) -> str:
        if upto <= self.emitted:
            return ""
        out = self.raw[self.emitted:upto]
        self.emitted = upto
        return out

    def feed(self, piece: str) -> str:
        """Add one delta and get back the text that may be shown."""
        self.raw += piece
        out = ""
        while True:
            if self.hold:
                end = self.block_end()
                if end < 0:
                    return out
                self.emitted = end
                self.hold = ""
                continue
            found = -1
            for opener, _ in MARKERS:
                at = self.raw.find(opener, self.emitted)
                if at >= 0 and (found < 0 or at < found):
                    found = at
                    self.hold = opener
            if found >= 0:
                self.hold_at = found
                out += self.take(found)
                continue
            out += self.take(self.safe())
            return out


def tool_calls_of(raw: str, streamed: list) -> list:
    """The tool calls in one answer, from whichever shape they arrived in.

    qwen-api answers with OpenAI `tool_calls`, and also instructs the model to emit
    `<tool_calls>[{"name": ..., "arguments": {...}}]</tool_calls>` in the content. Both are read,
    the OpenAI one first, and an argument blob that is not JSON is passed through as the single
    `script` argument rather than being dropped: a model that fumbled the JSON usually still sent
    the script.
    """
    out: list = []
    for index, call in enumerate(streamed or []):
        function = call.get("function") or {}
        name = str(function.get("name") or call.get("name") or "").strip()
        if not name:
            continue
        out.append({"id": str(call.get("id") or f"call_{index}"), "name": name,
                    "arguments": parse_arguments(function.get("arguments") or call.get("arguments"))})
    if out:
        return out
    for match in TOOL_XML.finditer(raw or ""):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            parsed = _salvage(match.group(1))
        if isinstance(parsed, dict):
            parsed = parsed.get("tool_calls") or parsed.get("calls") or [parsed]
        for index, call in enumerate(parsed if isinstance(parsed, list) else []):
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or (call.get("function") or {}).get("name") or "").strip()
            if not name:
                continue
            args = call.get("arguments")
            if args is None:
                args = (call.get("function") or {}).get("arguments")
            out.append({"id": str(call.get("id") or f"call_{index}"), "name": name,
                        "arguments": parse_arguments(args)})
    return out


def _salvage(block: str) -> list:
    """One JSON object per line, for a model that emitted several without an array."""
    out = []
    for line in (block or "").splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            out.append(got)
    return out


def parse_arguments(value) -> dict:
    """A tool call's arguments as a dict, however badly they were spelled."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        got = json.loads(value)
    except ValueError:
        got = None
    if isinstance(got, dict):
        return got
    # Not JSON. A script argument is the common case, so it is taken as one rather than thrown
    # away -- the tool will complain about anything else it was supposed to get.
    return {"script": value}


def stream_call(messages: list, temperature: Optional[float], provider: Provider,
                max_tokens: int, box: Optional[dict] = None,
                tools: Optional[list] = None, tool_choice: Optional[str] = None):
    """Stream one answer out of the provider's /chat/completions.

    Yields only what the user should see. `box` collects what the job needs to know afterwards:
    the raw content (tool calls, metadata and all), the finish reason, token usage, the hidden
    metadata, and the tool calls themselves. reasoning_content is dropped: thinking is on, and
    the answer is the script rather than the thinking about it.
    """
    body = provider.request(messages, temperature, max_tokens, stream=True,
                           tools=tools, tool_choice=tool_choice)
    cutter = Cutter()
    streamed_calls: list = []
    try:
        with httpx.Client(timeout=client_timeout(provider.timeout),
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
                    visible = strip_metadata(text)
                    if box is not None:
                        box["raw"] = text
                    yield visible
                    return
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if box is not None and chunk.get("usage"):
                        box["usage"] = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0] or {}
                    if box is not None and choice.get("finish_reason"):
                        box["finish"] = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    for call in delta.get("tool_calls") or []:
                        _merge_call(streamed_calls, call)
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        shown = cutter.feed(piece)
                        if shown:
                            yield shown
    except httpx.HTTPError as e:
        raise upstream_error(e, provider)
    if box is not None:
        box["raw"] = cutter.raw
        box["meta"] = " ".join(META_RE.findall(cutter.raw)).strip()
        box["tool_calls"] = tool_calls_of(cutter.raw, streamed_calls)


def _merge_call(calls: list, delta: dict) -> None:
    """OpenAI streams a tool call in fragments: an id and a name first, then pieces of arguments."""
    index = delta.get("index")
    index = int(index) if isinstance(index, int) else len(calls)
    while len(calls) <= index:
        calls.append({"id": "", "function": {"name": "", "arguments": ""}})
    slot = calls[index]
    if delta.get("id"):
        slot["id"] = str(delta["id"])
    function = delta.get("function") or {}
    if function.get("name"):
        slot["function"]["name"] = str(function["name"])
    if function.get("arguments"):
        slot["function"]["arguments"] += str(function["arguments"])
