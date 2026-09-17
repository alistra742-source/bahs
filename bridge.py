"""Everything that talks to the models: the tokens, the config, the request, and its stream.

Two models and three modes. Which one answers a question is the caller's to pick, per turn, and a
mode that needs a credential this service does not have is refused by name rather than served by
the other model:

    agent     deepseek plans once, then qwen3.8-max writes -- two calls, exactly
    qwen      qwen3.8-max, thinking on, with the toolbox, on its own
    deepseek  deepseek, on its own, no tools attached

Qwen is the writer with a toolbox: the work a second reader used to do -- checking the script,
looking up the API, running it, patching it -- is a set of tools it calls itself (see luau.py).
DeepSeek is a model you can talk to, and the planner in front of Qwen in agent mode.

    you -- ask --> bahs -- the mode's calls --> deepseek / qwen3.8-max
                     |                              |
                     |                              +-- calls a tool  -> luau.py
                     |                              +-- reads the result, on it goes
                     |
                     +-- the same upstream chat, always: qwen-api's hidden
                         `<!-- qwen_metadata: ... -->` and chat.deepseek.com's message id are
                         kept for the session, so a follow-up continues the chat the last answer
                         came from instead of opening a new one.

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

DeepSeek is the other side of this file and it is two transports in one: its OpenAI-shaped API
(`DEEPSEEK_TOKEN` is an `sk-...` key) or chat.deepseek.com itself, driven by the site's own
`userToken`. The site's endpoints are not OpenAI-shaped -- one `prompt` field, no system role,
thinking and search as plain booleans, and a proof of work on every message (pow_solver.py) -- so
that path has its own transport below. Which one is used follows the credential, because sending
a site token to the API earns a 401 that reads like a broken token when the endpoint is wrong.

The service's surface (jobs, endpoints, the page) is in server.py; what the service can say about
itself is in state.py.
"""
# The shared imports live here and nowhere else -- nothing else imports them: `state`, `luau` and `server` are written as
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

import pow_solver  # DeepSeek's proof of work; imports wasmtime lazily

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
    """One endpoint, and how it spells this service's two toggles.

    Both models speak the same request and response shape on their API paths, so the only
    per-provider knowledge is where it lives, what it calls the model, and how it spells
    "thinking". `shape` is the exception: an endpoint with no system role (a web-chat bridge) gets
    the turns folded into one prompt, and chat.deepseek.com itself is not OpenAI-shaped at all, so
    it gets a transport (`web`) instead of a request body.
    """

    def __init__(self, name: str, url: str, key: str, model: str, dialect: dict,
                 timeout: float, extra: Optional[dict] = None, shape: str = "openai",
                 web: Optional["DeepSeekWeb"] = None):
        self.name = name
        self.url = url.rstrip("/")
        self.key = key
        self.model = model
        self.dialect = dialect
        self.timeout = timeout
        self.extra = extra or {}
        self.shape = shape
        # Set when this provider is chat.deepseek.com itself: those endpoints are not
        # OpenAI-shaped, so the call goes through the web transport instead.
        self.web = web

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

        The model is not the caller's to choose: each mode pins its own, so a request behaves the
        same whoever sends it. Tools are attached here too, and only Qwen is ever sent any.
        """
        body = {
            "model": self.model,
            "messages": fold(self.shape, messages),
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


def fold(shape: str, messages: list) -> list:
    """One prompt for an endpoint that has no system role (the web-chat bridges).

    The instruction has to come before anything else, so the turns are concatenated in the order
    they were built -- system first -- rather than being dropped or reordered. An OpenAI-shaped
    endpoint gets the turns untouched.
    """
    if shape != "web":
        return messages
    parts = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
    return [{"role": "user", "content": "\n\n".join(parts)}]


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

# The same model on the same endpoint with the other setting, so the caller can ask for speed on
# the turn where it wants speed and for reasoning on the turn where it wants that -- instead of the
# whole service being pinned to whichever one the operator happened to start it with. A fast turn
# is the same writer with fewer tokens spent before it starts writing.
QWEN_FAST_THINKING = env("QWEN_FAST_THINKING", default="fast")
QWEN_FAST = Provider("qwen-fast", QWEN_URL, QWEN_TOKEN, QWEN_MODEL,
                     {"thinking_mode": QWEN_FAST_THINKING}, CHAT_TIMEOUT)

# Both of these are the writer: the tool rounds belong to them, and the session's continuation
# marker rides on their answers and no one else's -- so "is this Qwen" is a membership test here
# rather than an identity one, or a fast turn would forget the chat it was in.
QWEN_PROVIDERS = (QWEN, QWEN_FAST)

# The words a caller may use for the setting, in the sense a caller would mean them. Anything not
# listed as fast is thinking: thinking is what this service does, and an unrecognised word must not
# quietly turn reasoning off.
FAST_WORDS = ("fast", "quick", "instant", "speed", "off", "no", "false", "0", "none")


def writer_thinking(asked: str = "") -> str:
    """The writer's setting for one turn: "fast" or "thinking".

    An empty value is the service's own default, which is thinking -- the setting that produces the
    better script, and the one every turn had before a caller could pick.
    """
    word = (asked or "").strip().lower()
    if not word:
        return "fast" if QWEN_THINKING.strip().lower() in FAST_WORDS else "thinking"
    return "fast" if word in FAST_WORDS else "thinking"


def thinking_label(value: str) -> str:
    """How a setting reads on a chip, a log line or a status.

    The same words the caller may use, so every spelling of "not thinking" reads back as "fast" --
    including DeepSeek's on/off, which is a setting of its own.
    """
    return "fast" if (value or "").strip().lower() in FAST_WORDS else "thinking"


def writer_for(value: str = "") -> Provider:
    """The provider that writes the script: the same model, thinking or not."""
    return QWEN_FAST if writer_thinking(value) == "fast" else QWEN

# --- chat.deepseek.com, driven by the token the site itself stores -----------------------
#
# The web app has no public API, but its own endpoints answer a server, so a *userToken* -- the
# value behind chat.deepseek.com -> F12 -> Console ->
# JSON.parse(localStorage.getItem("userToken")).value -- is enough to talk to DeepSeek:
#
#   GET  /users/current             is the token still good?
#   POST /chat_session/create       a session id, {"character_id": null}
#   POST /chat/create_pow_challenge a challenge for the message about to be sent
#   POST /chat/completion           the answer -- and this one is gated by a proof of work
#
# The proof of work is solved with the site's own sha3 module (pow_solver.py), which the image
# carries: without it the message goes out without the header and comes back 40300, which is
# reported as it is rather than hidden. The module is not a reimplementation because the
# algorithm is neither SHA3-256 nor Keccak-256, and a near-miss earns the same refusal.

LOGIN_HINT = ("copy a fresh userToken: chat.deepseek.com -> F12 -> Console -> "
              "JSON.parse(localStorage.getItem(\"userToken\")).value")


def as_prompt(messages: list) -> str:
    """The turns as one string, in order, system first.

    The web endpoint has no roles: one `prompt` field. Concatenating in the order the turns were
    built is what keeps the instruction ahead of everything else on this path too.
    """
    parts = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
    return "\n\n".join(parts)


# The leaves of the site's `p` paths that carry state rather than answer text. The message id is
# the important one here: it arrives on every message, it is what threads the next one onto this
# one, and reading it as text would put a message id in the middle of the script.
NON_TEXT_PATHS = ("status", "message_id", "id", "session_id", "conversation_id", "title",
                  "created_at", "updated_at", "inserted_at", "quota", "finish_reason",
                  "type", "role", "model")


def read_chunk(chunk: dict, box: Optional[dict] = None) -> str:
    """One piece of the answer out of a chat.deepseek.com frame, in either shape it uses.

    The site has streamed an OpenAI-like frame and an older one (v, with fragments under p).
    Both are accepted rather than betting on one. Thinking is dropped -- it is not the answer, and
    the same rule holds here as on the Qwen side -- and a status, an id or an error frame yields
    nothing, so none of them can arrive as text in the middle of a script.
    """
    choices = chunk.get("choices") or []
    if choices:
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        if box is not None and choice.get("finish_reason"):
            box["finish"] = choice["finish_reason"]
        kind = str(delta.get("type") or "").lower()
        if kind in ("thinking", "reasoning"):
            # The site's own thinking, on a field of its own: still never the answer -- a fragment
            # here would land in the middle of the script -- but handed to a caller that asked to
            # see it, which is what a client with a thinking pane is.
            content = delta.get("content")
            if box is not None and callable(box.get("thoughts")) and isinstance(content, str):
                box["thoughts"](content)
            return ""
        return delta.get("content") or ""
    path = str(chunk.get("p") or "")
    value = chunk.get("v")
    if path == "response/status":
        if box is not None and str(value).strip().upper() == "FINISHED":
            box["finish"] = box.get("finish") or "stop"
        return ""
    if "thinking" in path.lower():
        # The same rule on the site's older frame shape.
        if box is not None and callable(box.get("thoughts")) and isinstance(value, str):
            box["thoughts"](value)
        return ""
    if path.rsplit("/", 1)[-1].lower() in NON_TEXT_PATHS:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part.get("content") or "" for part in value
                       if isinstance(part, dict)
                       and str(part.get("type") or "RESPONSE").upper() == "RESPONSE")
    return ""


class WebSession:
    """One chat on chat.deepseek.com, so a second message lands in the same conversation.

    The site threads a chat by `parent_message_id`: each message continues the one before it. The
    id of the message it just wrote arrives in the stream, and it is kept here -- which is what
    makes a follow-up about the script the last answer produced rather than a new question in a
    new chat.
    """

    def __init__(self, web: "DeepSeekWeb"):
        self.web = web
        self.id = ""
        self.parent: Optional[str] = None
        self.messages = 0


class DeepSeekWeb:
    """chat.deepseek.com as a model, over the endpoints the web app itself calls.

    `base` points at the site by default; it can be pointed somewhere else, which is both how this
    is tested and how a mirror would be used (a base ending in /api/v0).
    """

    DEFAULT_BASE = "https://chat.deepseek.com/api/v0"

    def __init__(self, token: str, timeout: float, cookies: str = "", base: str = "",
                 thinking: bool = True):
        self.token = token
        self.timeout = timeout
        self.cookie = (cookies or "").strip()
        self.base = (base or self.DEFAULT_BASE).rstrip("/")
        self.thinking = thinking
        self.label = self.base.split("//", 1)[-1].split("/", 1)[0]
        self.model = "deepseek-web"

    def headers(self, pow_value: Optional[str] = None) -> dict:
        """What the site's own client sends, so the request looks like the site's."""
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": f"Bearer {self.token}",
            "content-type": "application/json",
            "origin": "https://chat.deepseek.com",
            "referer": "https://chat.deepseek.com/",
            "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"),
            "x-app-version": "20241129.1",
            "x-client-locale": "en_US",
            "x-client-platform": "web",
            "x-client-version": "1.0.0-always",
        }
        if self.cookie:
            headers["cookie"] = self.cookie
        if pow_value:
            headers["x-ds-pow-response"] = pow_value
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=client_timeout(self.timeout), follow_redirects=True)

    def _call(self, method: str, path: str, payload: Optional[dict] = None):
        try:
            with self._client() as c:
                return c.request(method, self.base + path, json=payload, headers=self.headers())
        except httpx.HTTPError as e:
            raise HTTPException(502, f"cannot reach chat.deepseek.com ({e.__class__.__name__})")

    @staticmethod
    def _json(response) -> dict:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _explain(self, body: dict, status: int = 0) -> str:
        """What chat.deepseek.com said, with the one fix that is not obvious."""
        code = body.get("code")
        message = str(body.get("msg") or "").strip()
        if code == 40002:
            return "chat.deepseek.com: Missing Token -- DEEPSEEK_TOKEN is not set on this service"
        if code == 40003:
            return (f"chat.deepseek.com rejected DEEPSEEK_TOKEN ({message or 'invalid token'}) -- "
                    + LOGIN_HINT)
        # The two proof-of-work refusals, told apart rather than pooled: 40300 is the header not
        # being there (no module, an unknown algorithm, or a challenge that could not be solved),
        # 40301 is an answer the server rejected -- a different problem with a different fix.
        if code == 40300:
            return (f"chat.deepseek.com: MISSING_HEADER ({message or 'no detail'}) -- the message "
                    "was refused for the proof-of-work header. The [deepseek] lines in this "
                    "service's log say which half failed: the sha3 module, or the solve")
        if code == 40301:
            return (f"chat.deepseek.com: INVALID_POW_RESPONSE ({message or 'no detail'}) -- the "
                    "proof of work was solved with a module that is not the one the site uses")
        text = json.dumps(body)[:300] if body else f"HTTP {status}"
        if "pow" in text.lower() or "proof" in text.lower():
            return f"chat.deepseek.com refused the proof of work ({text})"
        return f"chat.deepseek.com said {code}: {message}" if code else text

    def validate(self) -> tuple:
        """Whether the userToken is still good, for the chip."""
        response = self._call("GET", "/users/current")
        body = self._json(response)
        if response.status_code >= 400 and not body:
            return False, f"chat.deepseek.com answered HTTP {response.status_code}"
        if body.get("code") not in (None, 0):
            return False, self._explain(body, response.status_code)
        return True, "token accepted"

    def create_session(self) -> str:
        """A fresh chat, so two conversations never read each other's turns."""
        response = self._call("POST", "/chat_session/create", {"character_id": None})
        body = self._json(response)
        session = (((body.get("data") or {}).get("biz_data") or {}).get("id"))
        if not session:
            raise HTTPException(502, self._explain(body, response.status_code))
        return str(session)

    def challenge(self):
        """The proof-of-work challenge for the next message, when it can be had at all."""
        try:
            response = self._call("POST", "/chat/create_pow_challenge",
                                  {"target_path": "/api/v0/chat/completion"})
        except HTTPException:
            return None
        body = self._json(response)
        return (((body.get("data") or {}).get("biz_data") or {}).get("challenge"))

    def new_session(self) -> WebSession:
        """An open chat, for a conversation that will hold more than one message."""
        return WebSession(self)

    @staticmethod
    def _message_id(chunk: dict) -> str:
        """The id of the message the site is writing, when it says so.

        Two shapes have been seen: a `response/message_id` frame, and the id as a field of a
        frame. Either is used. Neither being there is not an error -- it only means this message
        cannot be threaded onto the previous one, which the caller says out loud.
        """
        if str(chunk.get("p") or "") in ("response/message_id", "message_id"):
            value = chunk.get("v")
            if isinstance(value, str) and value:
                return value
        value = chunk.get("message_id")
        if isinstance(value, str) and value:
            return value
        data = chunk.get("data")
        biz = (data or {}).get("biz_data") if isinstance(data, dict) else None
        for holder in (data, biz):
            if isinstance(holder, dict):
                value = holder.get("message_id")
                if isinstance(value, str) and value:
                    return value
        return ""

    def stream(self, prompt: str, box: Optional[dict] = None,
               session: Optional[WebSession] = None):
        """Send one prompt and stream the answer back.

        With a session, this is the next message in that chat: the turns before it are already
        there, so only the new prompt is sent and the answer is about the question that opened the
        chat. Without one, the call opens a chat of its own.
        """
        if session is None:
            session = self.new_session()
        if not session.id:
            session.id = self.create_session()
        # The site threads a chat by the id of the message before this one. It hands that id over
        # inside the stream, so it is taken from there; when it does not, the message goes out
        # with no parent, which starts a new branch of the same chat -- said out loud rather than
        # passed off as a continuation.
        parent = session.parent
        if session.messages and not parent:
            print("[deepseek] the site gave no message id for the previous message, so this one "
                  "may start a new branch of the same chat", flush=True)
        challenge = self.challenge() or {}
        pow_value = ""
        if challenge:
            # The solve is native and bounded by the challenge's difficulty (~10 ms for the 144000
            # the site hands out), and solve() returns "" rather than raising when it cannot.
            pow_value = pow_solver.solve(challenge)
            if not pow_value:
                print(f"[deepseek] the challenge (difficulty {challenge.get('difficulty')}) was not "
                      "solved; this message goes out without the header, which the API answers "
                      "with 40300 MISSING_HEADER", flush=True)
        print(f"[deepseek] sending {len(prompt)} chars (thinking "
              f"{'on' if self.thinking else 'off'}, search off)", flush=True)
        payload = {
            "chat_session_id": session.id,
            "parent_message_id": parent,
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": self.thinking,
            "search_enabled": False,
        }
        with self._client() as c:
            with c.stream("POST", f"{self.base}/chat/completion", json=payload,
                          headers=self.headers(pow_value)) as r:
                if r.status_code >= 400 or "event-stream" not in r.headers.get("content-type", ""):
                    raw = r.read().decode("utf-8", "replace")
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = {"msg": raw[:300]}
                    raise HTTPException(502, self._explain(
                        body if isinstance(body, dict) else {}, r.status_code))
                session.messages += 1
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("code") and chunk.get("msg") and chunk.get("v") is None:
                        raise HTTPException(502, self._explain(chunk, 200))
                    message_id = self._message_id(chunk)
                    if message_id:
                        session.parent = message_id
                    piece = read_chunk(chunk, box)
                    if piece:
                        yield piece
                if box is not None and not box.get("finish"):
                    # The site never said it had finished writing, so this answer is whatever
                    # arrived before the stream stopped -- said out loud rather than passed off as
                    # the whole answer.
                    print("[deepseek] the site's stream ended without a finished status: the "
                          "answer may be only the part it managed to write", flush=True)


# --- the deepseek side -------------------------------------------------------------------
#
# Two credentials fit in DEEPSEEK_TOKEN, and which one it is decides the endpoint:
#   * an API key (`sk-...`) from platform.deepseek.com -> the API, OpenAI-shaped.
#   * the `userToken` chat.deepseek.com keeps in localStorage -> the site's own endpoints,
#     driven by the DeepSeekWeb transport above, because the API does not take that token.
# The endpoint follows the credential on its own, so a pasted userToken is not rejected by the
# API first: that 401 says the token is bad when it is only in the wrong place.
DEEPSEEK_TOKEN = env("DEEPSEEK_TOKEN", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY", "REVIEW_KEY")
_ask_url = env("DEEPSEEK_URL", "REVIEW_URL")
_session_token = bool(DEEPSEEK_TOKEN) and not DEEPSEEK_TOKEN.startswith("sk-")
# The API takes only an `sk-` key, so a session token aimed at it (or at nothing) goes to the site
# instead: that pair cannot authenticate, and the 401 it earns reads like a broken token.
DEEPSEEK_URL_AUTO = _session_token and (not _ask_url or "api.deepseek.com" in _ask_url)
DEEPSEEK_URL = ("https://chat.deepseek.com" if DEEPSEEK_URL_AUTO
                else (_ask_url or "https://api.deepseek.com"))
DEEPSEEK_MODEL = env("DEEPSEEK_MODEL", "REVIEW_MODEL", default="deepseek-v4-flash")
# openai (an OpenAI-shaped endpoint, including api.deepseek.com) | web (a bridge in front of
# chat.deepseek.com: no system role, and the toggles are plain booleans) | deepseek-web (the
# site's own endpoints, driven by the userToken -- the DeepSeekWeb transport above).
DEEPSEEK_SHAPE = env("DEEPSEEK_SHAPE", "REVIEW_SHAPE", default="openai").lower()
# A userToken is the site's own token, so pointing DEEPSEEK_URL at the site selects its transport
# without having to be asked. An API key from platform.deepseek.com keeps the OpenAI shape.
if "chat.deepseek.com" in DEEPSEEK_URL:
    DEEPSEEK_SHAPE = "deepseek-web"
# The cf_clearance cookie, in case chat.deepseek.com ever answers a request with a browser check.
DEEPSEEK_COOKIE = env("DEEPSEEK_COOKIE", "REVIEW_COOKIE")
# The web endpoints take no model id: the session's model is whatever the account is set to, so
# claiming a specific one would be a lie on the chip.
if DEEPSEEK_SHAPE == "deepseek-web" and not env("DEEPSEEK_MODEL", "REVIEW_MODEL"):
    DEEPSEEK_MODEL = "deepseek-web"
# Thinking is on: this model is writing a plan or a whole script here, not answering a quick
# question, and on neither transport does its reasoning reach the answer (see the content loop in
# stream_call and read_chunk, which hand it to the box's `thoughts` callback instead) -- so what
# comes back is the plan or the script either way, and a client can still watch it think.
DEEPSEEK_THINKING = env("DEEPSEEK_THINKING", "REVIEW_THINKING", default="on").lower()
# Search is never switched on anywhere in this service: the answers here are about the code in
# front of the model, and a web search is neither free nor useful for that.
SEARCH_OFF = True
# A planner and a writer are not asked to be creative; they are asked to be right about what runs.
DEEPSEEK_TEMPERATURE = float(env("DEEPSEEK_TEMPERATURE", "REVIEW_TEMPERATURE", default="0.3"))
# A whole script, or a plan; both are long, and the site path takes no ceiling at all.
DEEPSEEK_TOKENS = int(env("DEEPSEEK_TOKENS", "PEER_TOKENS", "REVIEW_MAX_TOKENS", default="8192"))
# The plan is a page of prose, not a script, so it is capped separately: one that invites an essay
# must not spend the turn's budget on it before the writer has been called once.
DEEPSEEK_PLAN_TOKENS = int(env("DEEPSEEK_PLAN_TOKENS", default="4096"))
# No ceiling on a call, like the rest of the chain: a slow model is not an error, and cutting a
# script off half way is worse than waiting for it. Connecting is still bounded.
DEEPSEEK_TIMEOUT = float(env("DEEPSEEK_TIMEOUT", "REVIEW_TIMEOUT", default="0"))
DEEPSEEK_EXTRA: dict = {}
try:
    _extra = json.loads(env("DEEPSEEK_EXTRA", "REVIEW_EXTRA", default="{}") or "{}")
    if isinstance(_extra, dict):
        DEEPSEEK_EXTRA = _extra
except ValueError:
    print("[bridge] DEEPSEEK_EXTRA is not valid JSON; ignoring it", flush=True)


def deepseek_dialect() -> dict:
    """DeepSeek's thinking switch, in whichever shape the endpoint expects.

    The API takes `thinking: {"type": ...}`. A bridge in front of the web chat takes booleans.
    Search is set to false in both, and is never set true anywhere in this file. The site's own
    transport has no dialect at all -- its payload is built by DeepSeekWeb.stream.
    """
    thinking_on = DEEPSEEK_THINKING in ("on", "thinking", "enabled", "slow")
    if DEEPSEEK_SHAPE == "web":
        return {"thinking": thinking_on, "search": not SEARCH_OFF,
                "thinking_enabled": thinking_on, "search_enabled": not SEARCH_OFF}
    return {"thinking": {"type": "enabled" if thinking_on else "disabled"}}


DEEPSEEK = Provider(
    "deepseek", DEEPSEEK_URL, DEEPSEEK_TOKEN, DEEPSEEK_MODEL, deepseek_dialect(),
    DEEPSEEK_TIMEOUT, DEEPSEEK_EXTRA, DEEPSEEK_SHAPE,
    (DeepSeekWeb(DEEPSEEK_TOKEN, DEEPSEEK_TIMEOUT, DEEPSEEK_COOKIE,
                 DEEPSEEK_URL if "/api/v0" in DEEPSEEK_URL else "",
                 DEEPSEEK_THINKING in ("on", "thinking", "enabled", "slow"))
     if DEEPSEEK_SHAPE == "deepseek-web" else None),
)

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
# A line that is a tool call for the Roblox client: `@@GREP word@@`, `@@SOURCE@@ path`, or the
# opening line of one whose argument runs on. The client reads these out of the answer and runs
# them, which is a job this service cannot do -- it has no Roblox to look at.
TOOL_LINE = re.compile(r"(?m)^[ \t]*@@[A-Z_]+")


def tool_request(text: str) -> bool:
    """Whether the answer is asking the client to run its tools rather than answering.

    The difference that matters here: prose about a script is a turn that failed, and a list of
    calls for the client is a turn doing its work. The first has to be asked again or refused; the
    second has to be shipped, because the client runs those calls and asks its next question with
    what they found -- and asking the model again instead would throw the calls away and have it
    answer from less than it knew.
    """
    return bool(TOOL_LINE.search(text or ""))
# The two things that must never reach the text: a tool call in its XML shape, and the metadata.
MARKERS = (("<tool_calls>", "</tool_calls>"), ("<!--", "-->"))

_sessions: dict = {}
_sessions_lock = threading.Lock()


def session_ttl() -> float:
    return float(env("SESSION_TTL", default="3600") or 3600)


def session_new() -> str:
    return uuid.uuid4().hex[:12]


def session_remember(name: str, meta: Optional[str]) -> None:
    """Keep the hidden continuation marker for a session. Empty means 'forget it', None 'leave it'.

    None is what a call to another model says: a DeepSeek answer carries no Qwen marker, and
    writing "" over the session's would end the Qwen chat in the middle of an agent turn.
    """
    if not name or meta is None:
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


# The same idea on the DeepSeek side, for the one transport that needs it: chat.deepseek.com
# threads a chat by the id of the message before this one, which only the site holds, so the open
# chat is kept here per session. An OpenAI-shaped endpoint needs none of this -- the caller sends
# the turns, and those already carry the conversation.
_ds_chats: dict = {}
_ds_chats_lock = threading.Lock()


def deepseek_chat(name: str) -> Optional["WebSession"]:
    """The chat this session is already in on chat.deepseek.com, opened on first use.

    A caller with no session gets a chat of its own rather than sharing one with every other
    anonymous caller, which is the difference between two people and one confused conversation.
    """
    web = DEEPSEEK.web
    if web is None:
        return None
    if not name:
        return web.new_session()
    with _ds_chats_lock:
        now = time.time()
        for old in [k for k, v in _ds_chats.items() if now - v["at"] > session_ttl()]:
            _ds_chats.pop(old, None)
        found = _ds_chats.get(name)
        if found is None:
            found = {"chat": web.new_session(), "at": now}
            _ds_chats[name] = found
        else:
            found["at"] = now
        return found["chat"]


def deepseek_chats() -> int:
    with _ds_chats_lock:
        return len(_ds_chats)


def strip_metadata(text: str) -> str:
    """The text without the hidden continuation marker."""
    return META_RE.sub("", text or "").strip()


def with_continuation(messages: list, session: str = "") -> list:
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


# --- what each model is told -------------------------------------------------------------
#
# Every mode puts a standing instruction in front of the caller's conversation -- about the work,
# not about a persona -- so the same question behaves the same way whoever sends it.

# The one thing that would make every answer wrong, said first and on every path: an executor is
# not Studio, and advice about the Studio editor is advice about a different script.
EXECUTOR_NOTE = """You write Luau that runs under a Roblox executor, injected into a live client. \
It is not a Roblox Studio place script and not a script for the Studio editor: there is no server \
side, no plugin API and no edit mode, and a script that assumes any of them is wrong."""

# What the answer has to be, whatever writes it: the script, and nothing around it. The fence is
# said three ways because it is the one formatting habit every model has, and a ``` is a syntax
# error the moment the answer is pasted into an executor -- which is where it goes.
ANSWER_RULE = """Answer with the complete runnable script and nothing else: no commentary, no \
summary of your changes, and no markdown fence -- never write ``` anywhere, not before the script, \
not after it, and not around a snippet inside it. The answer is pasted straight into an executor, \
where a fence line is a syntax error."""

# With tools on, this rides in front of the caller's conversation as the system turn. Every tool
# this service has is named here: a tool the writer is not told about is a tool it will not use,
# and the list is one line each so it stays cheap on every call.
TOOL_SYSTEM = EXECUTOR_NOTE + """

You have tools, and using them is part of writing the script:
* `roblox_api` -- check that a class, property, function or event really exists before you rely on it.
* `web_get` -- read a page: Roblox documentation, a DevForum thread, a raw file. Use it when the answer depends on how something is really used, not on whether the member exists.
* `luau_check` -- check the whole script for unbalanced blocks, unterminated strings and calls that break in an executor, before you hand it over.
* `run_script` -- run it in the executor that is listening, when one is, and read what it printed or how it failed. Nothing else can prove a script runs.
* `apply_edit` -- change one part of a script you already have instead of writing the whole thing again.
* `luau_find` -- the lines of a script that match a pattern, with line numbers, when you need one part of a long script.
* `luau_format` -- re-indent a script you assembled from pieces.
* `secret_scan` -- find credentials in the script before it ships.

Call a tool when it would change your answer; never describe a call in prose. """ + ANSWER_RULE

# The planner's instruction, in agent mode. This model does not write the script -- the writer
# does, from this plan -- so the answer wanted here is the design, and a plan that is a script is
# a plan the writer will copy instead of thinking about.
PLAN_SYSTEM = EXECUTOR_NOTE + """

You are the planner of a two-model chain: another model writes the script from your plan and has \
the tools to check its own work, so what it needs from you is judgement, not code.

Answer with a build plan, in this order:
1. what the script has to do, in the terms the question used;
2. the approach: which services, classes and members, named exactly -- a member that does not \
exist is the most expensive thing that can go into a plan;
3. the shape of the script: what sits at the top level, what the main loop or event handler is, \
what has to be cleaned up;
4. the traps: what breaks in an executor rather than in Studio, what has to happen in what order, \
and what needs a pcall.

Be specific and be brief. No code fences, no full script, no restating the question, no closing \
summary; a short inline snippet is fine where a line of code is the clearest way to say it."""

# What DeepSeek is told when it writes the script itself (deepseek mode). It is the same framing
# as the writer's, minus the tools -- there are none on that path -- and with the one warning that
# matters when nothing has checked the answer: nothing has.
WRITE_SYSTEM = EXECUTOR_NOTE + """

No tool runs your script before it is sent and nothing checks it for you, so be exact: name only \
members you are sure exist, and prefer the plainly-supported call over the clever one. """ \
    + ANSWER_RULE

AGENT_ROUNDS_MAX = 12
# How many tool rounds one turn may take. A round is one model call plus the tools it asked for, so
# this is the turn's wall-clock as much as it is its budget: at 8 a curious model could spend nine
# calls on one question, which is minutes of somebody watching a status line. Four is enough for a
# script that needs a second look, and the Roblox client runs a further round of tools of its own
# after every answer besides. Clamped rather than trusted.
AGENT_ROUNDS = max(0, min(int(env("AGENT_ROUNDS", default="4")), AGENT_ROUNDS_MAX))
# The tool that actually runs a script is the one that can hang (an executor stuck in a wait), so
# it is bounded separately.
TOOL_RESULT_MAX = int(env("TOOL_RESULT_MAX", default="20000"))
# How many times one turn may tell the writer "that was not a script, send the script" before it
# gives up. One is normally enough -- it is the same chat, and the second answer arrives while the
# first is still in front of it -- and each retry is another whole model call, which is the slowest
# thing in a turn, so the default is one rather than two. The bound is on a model stuck in prose,
# not a budget to spend on every turn. It is a real gap when it does not fire: `0` ships the prose.
SCRIPT_RETRIES = max(0, min(int(env("SCRIPT_RETRIES", default="1")), 4))

# --- the three modes ---------------------------------------------------------------------
#
# Which model answers is the caller's to choose, per turn. Two of the three need both
# credentials, one needs only Qwen, and one needs only DeepSeek -- so a mode whose credential is
# missing is refused by name rather than quietly served by the other model.
#
#   agent     the planner call is made once and never repeated, and what follows is the writer's
#             own tool rounds -- not a second opinion, not a review, not a merge. Two models.
#   qwen      qwen3.8-max, thinking on, with the toolbox.
#   deepseek  deepseek, on its own: it writes the script and no tools are attached.
MODE_AGENT, MODE_QWEN, MODE_DEEPSEEK = "agent", "qwen", "deepseek"
MODES = (MODE_AGENT, MODE_QWEN, MODE_DEEPSEEK)  # the picker's order
MODE_LABELS = {
    MODE_AGENT: f"{DEEPSEEK_MODEL} plans, then {QWEN_MODEL} writes",
    MODE_QWEN: f"{QWEN_MODEL}, thinking on, with the toolbox",
    MODE_DEEPSEEK: f"{DEEPSEEK_MODEL}, on its own",
}
# Which mode a turn runs in when the caller does not pick one.
CHAIN_MODE = env("CHAIN_MODE", default=MODE_QWEN).strip().lower() or MODE_QWEN


def mode_needs(mode: str) -> tuple:
    """Which credentials a mode has to have, as (variable, is set) pairs."""
    if mode == MODE_AGENT:
        return (("QWEN_TOKEN", CONFIGURED), ("DEEPSEEK_TOKEN", DEEPSEEK.configured))
    if mode == MODE_DEEPSEEK:
        return (("DEEPSEEK_TOKEN", DEEPSEEK.configured),)
    return (("QWEN_TOKEN", CONFIGURED),)


def mode_available(mode: str) -> bool:
    return all(ok for _, ok in mode_needs(mode))


def mode_label(mode: str) -> str:
    """The chain a mode runs, for a job, a chip or a log line."""
    return MODE_LABELS.get(mode, mode)


def modes_state() -> list:
    """The picker's options, with the ones that cannot run saying what they are missing."""
    return [{"id": mode, "label": MODE_LABELS[mode], "on": mode_available(mode),
             "needs": [name for name, ok in mode_needs(mode) if not ok]} for mode in MODES]


def default_mode() -> str:
    """The mode a turn runs in when the caller does not pick one.

    CHAIN_MODE when it can run, and otherwise the first mode that can: a service holding only a
    DeepSeek token still answers instead of refusing every turn over a default the operator set
    before the other credential was there. An explicitly asked-for mode never falls back.
    """
    if mode_available(CHAIN_MODE):
        return CHAIN_MODE
    for mode in MODES:
        if mode_available(mode):
            return mode
    return CHAIN_MODE


def resolve_mode(asked: str) -> str:
    """The mode this turn runs in, or a refusal naming what is missing.

    An unknown mode is refused rather than treated as the default, and the fallback only ever
    applies to a caller that picked nothing: answering as another model is not a fallback, it is
    a different question being answered.
    """
    want = (asked or "").strip().lower()
    if not want:
        return default_mode()
    if want not in MODES:
        raise HTTPException(400, f"unknown mode {want!r}; one of {', '.join(MODES)}")
    missing = [name for name, ok in mode_needs(want) if not ok]
    if missing:
        raise HTTPException(503, f"mode {want!r} needs {' and '.join(missing)} on this service")
    return want


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
# Every fenced block anywhere in the text, and a fence line on its own (an unclosed one, or a
# language tag left with nothing under it).
FENCE_BLOCK = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\n(.*?)```", re.S)
FENCE_LINE = re.compile(r"^[ \t]*```[A-Za-z0-9_+-]*[ \t]*$", re.M)


def unwrap_fences(text: str) -> str:
    """Every fence taken off, with everything that was inside it left where it was.

    For an answer that is prose and may quote a snippet -- the planner's plan -- where dropping
    the talk around a block would throw away the thing being asked for. The markers go, nothing
    else does.
    """
    body = (text or "").strip()
    body = FENCE_BLOCK.sub(lambda m: m.group(1).strip(), body)
    return FENCE_LINE.sub("", body).strip()


def strip_fences(text: str) -> str:
    """The script out of an answer, whatever the model wrapped it in.

    The prompt asks for the script and nothing else, and a fence is a syntax error the moment the
    answer is pasted into an executor -- so a fence is taken off rather than the turn refused:

      * the model fenced the script *and* talked around it (or fenced two versions), so the block
        that reads as Lua is the answer and the prose is not, which is the rule the writer already
        had;
      * the whole answer is nothing but fenced blocks -- the model split one script across them
        ("part one", "part two") rather than offering two versions of it -- so the pieces are put
        back together in the order they were written. Returning the longest block there would ship
        half a script that looks whole, which is worse than an answer that fails loudly;
      * the whole answer is one fenced block of prose, or there is no complete block at all -- just
        markers, an unclosed one or a stale language tag -- and the markers come off while every
        other line stays as it was.

    The blocks are looked at before the whole-answer shape because the whole-answer pattern would
    gladly swallow two blocks as one, quotes and all.
    """
    body = (text or "").strip()
    blocks = [m.group(1).strip() for m in FENCE_BLOCK.finditer(body)]
    scripts = [b for b in blocks if looks_like_code(b)]
    # What is left once every block is taken out: empty means the answer was nothing but blocks,
    # which is a script in pieces rather than a script among talk about it.
    talk = FENCE_LINE.sub("", FENCE_BLOCK.sub("", body)).strip()
    if len(scripts) == len(blocks) > 1 and not talk:  # one script in pieces, not two versions
        return "\n".join(scripts)
    if scripts:
        return max(scripts, key=len)  # the block that reads as Lua is the answer
    whole = FENCE.match(body)
    if whole:
        return whole.group(1).strip()
    return unwrap_fences(body) or body


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


# --- is the answer actually a script? -----------------------------------------------------
#
# The one failure that reads as success: a model that answers with a paragraph about the script it
# is going to write, or with a list of tools it means to call. `structural_notes` below cannot see
# it -- that check is about an answer being whole, and a paragraph is whole -- so it used to ship,
# with the turn reported as answered and the paragraph sitting where the script goes.
#
# Everything here is structural on purpose, because that is all this service can honestly be: it
# says what the text looks like, not what it does.

# A tool call the Roblox client runs by reading it out of the answer. Both shapes are in the wild --
# `@@GREP word@@` and `@@SOURCE@@ path` -- and neither is Lua. Longest branch first, so a token that
# closes before its argument is not left with a stray `@@` in the text.
TOOL_TOKEN = re.compile(r"@@[A-Z_]+[ \t]+[^@\n]*@@|@@[A-Z_]+@@|@@[A-Z_]+")
# A whole line that is one of those calls. Cut by line as well as by token, because the argument
# of `@@SOURCE@@ path` is the rest of that line: it is the client's to read, and the path is not
# a line of any script.
TOOL_CALL_LINE = re.compile(r"(?m)^[ \t]*@@[A-Z_]+.*$")

# The words that make a line look like a statement rather than a sentence about one. `game` and
# `script` are in here because `game.Workspace.Baseplate.Transparency = 1` is a whole script.
LUA_WORD = re.compile(r"\b(local|function|end|then|else|elseif|for|while|repeat|until|do|return|"
                      r"break|continue|and|or|not|nil|true|false|game|script|Instance|Enum|"
                      r"task|wait|spawn|print|warn|pcall|xpcall|ipairs|pairs|require|math|"
                      r"string|table|tostring|tonumber|type|typeof|self)\b")
# A line of nothing but closers is not evidence either way, so it is not counted at all.
PUNCTUATION_ONLY = re.compile(r"^[)\]},;]+$")


def reads_as_english(line: str) -> bool:
    """Whether a line reads as a sentence rather than as a statement.

    Four or more words with no assignment, no call and no table in them is English. It is what
    keeps `and`, `for`, `then` and `end` -- Lua words that are also ordinary words -- from
    making a paragraph of prose look like a script, which is the failure this verdict exists to
    catch.
    """
    if any(char in line for char in "=({["):
        return False
    return len(line.split()) >= 4


def script_verdict(text: str) -> tuple:
    """Whether an answer is a script, and when it is not, why -- in one sentence.

    Deliberately generous about what counts as one. A script is short and ugly and full of
    one-line statements, and refusing a real one costs a caller a turn; so the bar is "most of the
    lines read as Lua", with the single-statement and two-line scripts going through on their own.
    """
    body = TOOL_TOKEN.sub("", TOOL_CALL_LINE.sub("", text or ""))
    lines = []
    for line in body.splitlines():
        line = line.strip()
        if line and not PUNCTUATION_ONLY.match(line):
            lines.append(line)
    if not lines:
        return False, "the answer carried no script at all"
    readable = sum(1 for line in lines
                   if not reads_as_english(line)
                   and (LUA_WORD.search(line) or line.startswith("--")
                        or "=" in line or "(" in line or "{" in line))
    if readable == 0:
        return False, "no line of the answer reads as Lua"
    if len(lines) >= 3:
        if readable * 3 < len(lines) * 2:
            return False, f"only {readable} of {len(lines)} lines read as Lua"
    elif readable < len(lines):
        # One or two lines have no room for a majority, so every one of them counts.
        return False, f"only {readable} of {len(lines)} lines read as Lua"
    return True, ""


def prose_correction(why: str) -> str:
    """The turn that turns a prose answer into a script, asked in the chat that produced it.

    Sent as the newest user turn rather than as a new system instruction: the conversation is the
    writer's own, and what it needs to hear is what was wrong with what it just said.
    """
    return (f"That answer was not a script ({why}), so nothing could be pasted into the executor "
            f"and nothing was built. Reply with ONLY the complete Luau script, ready to run: no "
            f"prose, no explanation of the approach, no markdown fence, and no tool token on a "
            f"line of its own. The script itself, whole.")


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
