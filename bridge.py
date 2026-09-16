from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path
from collections import defaultdict, deque
import asyncio, hmac, html, httpx, json, os, re, threading, time, uuid

import pow_solver  # DeepSeek's proof of work; imports wasmtime lazily

# --------------------------------------------------------------------------------------
# What this service is: two models, competing over one script, behind one API.
#
#   you -- ask --> bahs -- brief, on its own -->         DeepSeek  (V4 Flash, thinking off)
#                      \
#                       `- draft -->                     Qwen      (qwen-api)
#                           \
#                            `- the other version -->    DeepSeek, in the chat the brief opened
#                                 \
#                                  `- merge, same chat as the draft --> Qwen
#                                       \
#                                        `- agree? no -> another version (NEGOTIATE_ROUNDS)
#
# Neither model is asked what is wrong. Each is asked for the script it would ship, and the
# writer merges the two in the chat it wrote the draft in; the reviewer is then asked whether
# it would ship the merge, and the turn ends when it says yes or the rounds run out.
#
# chat.qwen.ai has no public API. github.com/encryptarun/qwen-api turns it into
# OpenAI-compatible endpoints using the Qwen *access token* from the browser
# (chat.qwen.ai -> DevTools console -> localStorage.token). That token is the key to a
# whole Qwen account, so it lives here and never in a page or a Roblox script.
#
# The reviewer is DeepSeek V4 Flash, with thinking and search switched off and never the
# caller's to choose. Which DeepSeek it is depends on the credential: an `sk-...` API key
# reviews through api.deepseek.com, and a chat.deepseek.com `userToken` reviews through the
# site's own endpoints (see DeepSeekWeb below) -- where the message call needs a proof of work,
# solved here with DeepSeek's own sha3 module (pow_solver.py).
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
# in. The brief, the reviewer's version and the merge instruction are internal turns -- they
# are never part of what the user's next question carries.
#
# Nothing is pulled, loaded or warmed: no weights, no GPU, no volume, no database. The one
# file this service reads is Send.txt, the brief handed to the reviewer before anything
# else (REVIEW_BRIEF).
# --------------------------------------------------------------------------------------


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


# --- the two providers ------------------------------------------------------------------

class Provider:
    """One OpenAI-compatible endpoint, plus whatever it calls "answer without thinking".

    Both sides speak the same request and response shape, so the only per-provider
    knowledge is where it lives, what it calls the model, and how it spells "no thinking".
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
        # OpenAI-shaped, so the request goes through the web transport instead.
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
        # What goes out is logged with what comes back: without it, a short answer, a request that
        # never carried the brief, and a provider that stopped early all look the same afterwards.
        # The token checks do not come through here (stream=False), so this is one line per call.
        if stream and env("LOG_REQUESTS", default="1") != "0":
            asked = sum(len(m.get("content") or "") for m in body["messages"]
                        if isinstance(m, dict))
            print(f"[upstream] {self.name} -> {self.model}: {len(body['messages'])} message(s), "
                  f"{asked} chars, max_tokens {body.get('max_tokens', 'unset')}", flush=True)
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
# No ceiling on a generation, by default: a long negotiation is not an error, so nothing here
# cuts a model off for taking its time. 0 (or less) means no limit at all -- connecting is still
# bounded to 10s, so an unreachable host fails in seconds instead of looking like a slow model.
# Set a number to put a ceiling back.
CHAT_TIMEOUT = float(env("CHAT_TIMEOUT", default="0"))

QWEN = Provider(
    "qwen", QWEN_URL, QWEN_TOKEN, QWEN_MODEL,
    {"thinking_mode": QWEN_THINKING}, CHAT_TIMEOUT,
)

# --- chat.deepseek.com, driven by the token the site itself stores -----------------------
#
# The web app has no public API, but its own endpoints answer a server, so a *userToken* -- the
# value behind chat.deepseek.com -> F12 -> Console ->
# JSON.parse(localStorage.getItem("userToken")).value -- is enough to review a script. Three of
# the four calls need nothing else:
#
#   GET  /users/current             is the token still good?
#   POST /chat_session/create       a session id, {"character_id": null}
#   POST /chat/create_pow_challenge a challenge for the message about to be sent
#   POST /chat/completion           the answer -- and this one is gated by a proof of work
#
# The proof of work is the one piece that cannot be solved here: it needs DeepSeek's own
# sha3_wasm_bg.wasm, and the copies published with the two open-source bridges are stale (their
# wasm_solve writes nothing for any difficulty or input -- the README has the measurement). So the
# challenge is fetched and logged, no header is invented, and whatever the API answers is what
# gets reported -- which is how you find out whether it is enforced for your account at all.
#
# thinking_enabled and search_enabled are sent false, always. The reviewer reads a script; it does
# not reason out loud and it does not search the web. The site takes no temperature or token
# ceiling, so those are ignored on this path.

LOGIN_HINT = ("copy a fresh userToken: chat.deepseek.com -> F12 -> Console -> "
              "JSON.parse(localStorage.getItem(\"userToken\")).value")


def as_prompt(messages: list) -> str:
    """The turns as one string, in order, system first.

    The web endpoint has no roles: one prompt field. Concatenating in the order the turns were
    built is what keeps the brief ahead of everything else on this path too.
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
    Both are accepted rather than betting on one. Thinking is dropped -- it is switched off,
    and the reviewer's answer is what is wanted -- and a status, an id or an error frame yields
    nothing, so none of them can arrive as text in the middle of a script.
    """
    choices = chunk.get("choices") or []
    if choices:
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        if box is not None and choice.get("finish_reason"):
            box["finish"] = choice["finish_reason"]
        if str(delta.get("type") or "").lower() in ("thinking", "reasoning"):
            return ""
        return delta.get("content") or ""
    path = str(chunk.get("p") or "")
    value = chunk.get("v")
    if path == "response/status":
        if box is not None and str(value).strip().upper() == "FINISHED":
            box["finish"] = box.get("finish") or "stop"
        return ""
    if "thinking" in path.lower() or path.rsplit("/", 1)[-1].lower() in NON_TEXT_PATHS:
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
    id of the message it just wrote arrives in the stream, and it is kept here -- so the brief,
    the reviewer's own version of the script and its agreement all sit in one conversation, which
    is what makes the later questions be about the script the brief was read for.
    """

    def __init__(self, web: "DeepSeekWeb"):
        self.web = web
        self.id = ""
        self.parent: Optional[str] = None
        self.messages = 0


class DeepSeekWeb:
    """chat.deepseek.com as a reviewer, over the endpoints the web app itself calls.

    `base` points at the site by default; it can be pointed somewhere else, which is both how this
    is tested and how a mirror would be used (a base ending in /api/v0).
    """

    DEFAULT_BASE = "https://chat.deepseek.com/api/v0"

    def __init__(self, token: str, timeout: float, cookies: str = "", base: str = ""):
        self.token = token
        self.timeout = timeout
        self.cookie = (cookies or "").strip()
        self.base = (base or self.DEFAULT_BASE).rstrip("/")
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
        """A fresh chat for this review, so reviews never read each other."""
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
        there, so only the new prompt is sent and the reviewer answers about the script it was
        shown rather than starting over. Without one, the call opens a chat of its own, which is
        what a one-off review wants.
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
        # The prompt's size and whether the brief survived into it, in one line: this is the answer
        # to "is the whole send.txt being sent", and it is checkable in the service's own log.
        if BRIEF:
            print(f"[deepseek] sending {len(prompt)} chars (brief {len(BRIEF)} chars from "
                  f"{BRIEF_PATH.name}: {'whole' if BRIEF in prompt else 'NOT COMPLETE'}), "
                  "thinking off, search off", flush=True)
        else:
            print(f"[deepseek] sending {len(prompt)} chars (no brief loaded), thinking off, "
                  "search off", flush=True)
        payload = {
            "chat_session_id": session.id,
            "parent_message_id": parent,
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": False,
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
                    # The site never said it had finished writing, so this review is whatever
                    # arrived before the stream stopped -- said out loud rather than passed off as
                    # the whole answer.
                    print("[deepseek] the site's stream ended without a finished status: the "
                          "review may be only the part it managed to write", flush=True)


# --- the reviewer ------------------------------------------------------------------------
#
# DeepSeek V4 Flash, thinking off, search off. It is never sent the user's conversation: it is
# sent the brief, then a script, and it answers with a script of its own. It is the second
# author, not the model the user chose.
#
# Two credentials fit in DEEPSEEK_TOKEN, and which one it is decides the endpoint:
#   * an API key (`sk-...`) from platform.deepseek.com -> the API, OpenAI-shaped.
#   * the `userToken` chat.deepseek.com keeps in localStorage -> the site's own endpoints,
#     driven by the DeepSeekWeb transport above, because the API does not take that token.
# The endpoint follows the credential on its own, so a pasted userToken is not rejected by the
# API first: that 401 says the token is bad when it is only in the wrong place.
REVIEW_KEY = env("DEEPSEEK_TOKEN", "REVIEW_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY")
# Where the review goes. Left alone it is DeepSeek's API -- unless the credential is not an API key,
# in which case it is the site. DeepSeek's API keys start with `sk-` and the token chat.deepseek.com
# keeps in localStorage does not, and sending one of those to the API earns a 401 that reads like
# the token is broken when the endpoint is. Picking the endpoint from the credential itself means a
# pasted userToken works without a second variable.
_review_asked_url = env("REVIEW_URL", "DEEPSEEK_URL")
_session_token = bool(REVIEW_KEY) and not REVIEW_KEY.startswith("sk-")
# The API takes only an `sk-` key, so a session token aimed at it (or at nothing) goes to the site
# instead: that pair cannot authenticate, and the 401 it earns reads like a broken token.
REVIEW_URL_AUTO = _session_token and (not _review_asked_url or "api.deepseek.com" in _review_asked_url)
REVIEW_URL = ("https://chat.deepseek.com" if REVIEW_URL_AUTO
              else (_review_asked_url or "https://api.deepseek.com"))
REVIEW_MODEL = env("REVIEW_MODEL", "DEEPSEEK_MODEL", default="deepseek-v4-flash")
# openai (an OpenAI-shaped endpoint, including api.deepseek.com) | web (a bridge in front of
# chat.deepseek.com: no system role, and the toggles are plain booleans) | deepseek-web (the
# site's own endpoints, driven by the userToken -- the DeepSeekWeb transport above).
REVIEW_SHAPE = env("REVIEW_SHAPE", default="openai").lower()
# A userToken is the site's own token, so pointing REVIEW_URL at the site selects its transport
# without having to be asked. An API key from platform.deepseek.com keeps the OpenAI shape.
if "chat.deepseek.com" in REVIEW_URL:
    REVIEW_SHAPE = "deepseek-web"
# The cf_clearance cookie, in case chat.deepseek.com ever answers a request with a browser check.
REVIEW_COOKIE = env("DEEPSEEK_COOKIE", "REVIEW_COOKIE")
# The web endpoints take no model id: the session's model is whatever the account is set to, so
# claiming a specific one would be a lie on the chip.
if REVIEW_SHAPE == "deepseek-web" and not env("REVIEW_MODEL", "DEEPSEEK_MODEL"):
    REVIEW_MODEL = "deepseek-web"
# Thinking is enabled by default on DeepSeek V4, so it is switched off explicitly, and search
# is never switched on anywhere in this service: a review has to be cheap, quick, and about
# the script in front of it rather than about the web.
REVIEW_THINKING = env("REVIEW_THINKING", default="off").lower()
SEARCH_OFF = True
REVIEW_TEMPERATURE = float(env("REVIEW_TEMPERATURE", default="0.2"))
# The reviewer writes a whole script of its own now rather than a list, so its call gets the room
# a script needs on the API path (the site path takes no ceiling at all).
PEER_TOKENS = int(env("PEER_TOKENS", "REVIEW_MAX_TOKENS", default="8192"))
# The ceiling on NEGOTIATE_ROUNDS. Five rounds is five versions and five merges on top of the
# draft; more than that is a turn nobody would sit through.
MAX_NEGOTIATE_ROUNDS = 5
# Reading the brief is one short acknowledgement, so it is capped separately: a brief that invites
# an essay must not spend the turn on the acknowledgement.
SEED_TOKENS = int(env("SEED_TOKENS", default="512"))  # the acknowledgement only
# How many times the two models go back and forth over the same script. Every round is one
# version from the reviewer and one merge from the writer, and the rounds after the first are the
# reviewer agreeing with the merged script or proposing another one. 0 ships the draft alone.
# A round costs two model calls, so five is the most that is worth waiting for; the value is
# clamped rather than trusted, because a typo here is a turn that never ends.
NEGOTIATE_ROUNDS = max(0, min(int(env("NEGOTIATE_ROUNDS", default="5")), MAX_NEGOTIATE_ROUNDS))
# Whether the brief goes out on its own first, with the request only after the answer to it.
SEED_BRIEF = env("SEED_BRIEF", default="on").lower() not in ("off", "0", "false", "no")
# The script sent for review is bounded too: a 1M-token context is not a reason to use it.
REVIEW_SCRIPT_MAX = int(env("REVIEW_SCRIPT_MAX", default="48000"))
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


# The reviewer gets the same treatment: no limit, because the site's own generation is the
# slowest thing in the chain and cutting it off mid-script is worse than waiting for it.
REVIEW_TIMEOUT = float(env("REVIEW_TIMEOUT", default="0"))

REVIEWER = Provider(
    "deepseek", REVIEW_URL, REVIEW_KEY, REVIEW_MODEL, reviewer_dialect(),
    REVIEW_TIMEOUT,
    REVIEW_EXTRA,
    REVIEW_SHAPE,
    (DeepSeekWeb(REVIEW_KEY, REVIEW_TIMEOUT, REVIEW_COOKIE,
                 REVIEW_URL if "/api/v0" in REVIEW_URL else "")
     if REVIEW_SHAPE == "deepseek-web" else None),
)

# on (always) | off (never) | auto (only when a reviewer key is set)
PIPELINE = env("PIPELINE", default="auto").lower()


def review_enabled() -> bool:
    """Whether a question goes through the reviewer as well.

    A reviewer pointed at chat.deepseek.com is a real reviewer: that path is the web transport
    above, driven by the userToken. It only looks unconfigured when there is no token at all.
    """
    if not REVIEWER.configured:
        return False
    if PIPELINE in ("off", "0", "false", "no"):
        return False
    return True


# --- the tokens one call may use ---------------------------------------------------------
#
# Three calls, three ceilings. The draft and the rewrite produce whole scripts, so they get
# room; the review produces a list, so it does not. A draft that stops at its ceiling is
# reported as a failure instead of being shipped, because everything after it would be built
# on a cut-off script.
# Both Qwen calls exist to produce a whole script, and 4096 tokens is roughly 200 lines of Luau.
# An answer that stops at its ceiling is refused rather than shipped, so a ceiling that is too low
# shows up as a failed turn -- the rewrite can be long as well, so it gets the larger room.
MAX_TOKENS = int(env("MAX_TOKENS", default="4096"))
DRAFT_TOKENS = int(env("DRAFT_TOKENS", default="8192"))
REFINE_TOKENS = int(env("REFINE_TOKENS", default="16384"))
# How much of a script may be pasted into one instruction to the writer. The draft is already a
# turn in that conversation, so this is the ceiling on the *other* version being merged in.
MERGE_PASTE_MAX = int(env("MERGE_PASTE_MAX", default="48000"))

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

# Sent with the brief (or on its own when there is no brief), before anything is asked of the
# reviewer. The contract is a script, not a list of complaints: the reviewer's answer is put in
# front of the writer to be merged with theirs, and prose cannot be merged.
RUBRIC = f"""You are one of two models working on the same script. The other model writes a \
version; you write the version you would ship. You are not a commenter -- you are the other \
author, and everything you write is put in front of the writer to be merged with theirs.

Judge everything by one question: will it actually run in {TARGET_RUNTIME}? Ignore style, \
naming, formatting and taste.

Answer in exactly this format, and nothing else:

VERDICT: BETTER
<the complete script>

or, when the script in front of you cannot be made more reliable:

VERDICT: KEEP

Rules for this format:
- VERDICT: BETTER means a complete script follows immediately: every line of it, nothing left \
out, no placeholders, no "...", no commentary, and no markdown code fences.
- Keep what already works. Change only what would break, and only where your version is more \
reliable: an API name or property that does not exist in Roblox, a deprecated global that no \
longer runs, server/client confusion, a yield where none can happen, a loop that never ends, \
an event connected twice, anything that throws on its first line.
- Same language, same structure and the same entry points as the version in front of you, so the \
two can be merged line for line. Never rename anything the request did not name.
- Prefer the simplest thing that works over the cleverest thing that might.
- No praise, no summary, no explanation of your changes. The script is the answer.
- If the request was conversational rather than a request for a script, answer VERDICT: KEEP."""

# The line that follows the brief when the brief is sent on its own: it asks for the \
# acknowledgement and nothing else, so reading Send.txt does not become an essay that the real \
# request has to be queued behind.
SEED_NOTE = ("[the bridge] That is your standing instruction set, and it is the first thing in "
             "this chat. Nothing has been asked of you yet: reply with one short line saying you "
             "have read it -- no code, no summary, no questions -- and wait for the request that "
             "follows here in this same chat.")


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
    prefixed twice. Internal turns (the merge instruction) never go through this.
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
    HISTORY_MESSAGES or fatter than HISTORY_CHARS. The draft and the merge instruction are
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
            # A userToken sent to the API reads as a bad key, which is a misleading diagnosis: the
            # credential is fine, the endpoint is the wrong one. Say which fix is the right one.
            session_token = bool(provider.key) and not provider.key.startswith("sk-")
            if session_token and provider.web is None:
                return (f"{provider.label()} rejected the token ({message}) -- DEEPSEEK_TOKEN holds "
                        "a chat.deepseek.com session token, not an API key, and the review is "
                        "still going to the API. Set REVIEW_URL=https://chat.deepseek.com to use "
                        "the web transport, or put an `sk-...` API key from platform.deepseek.com "
                        "in DEEPSEEK_TOKEN")
            return (f"{provider.label()} rejected the key ({message}) -- check DEEPSEEK_TOKEN")
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


