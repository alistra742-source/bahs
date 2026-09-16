"""The second reader: GLM, and the brief it reads before anything else.

One writer, two readers. Qwen drafts; DeepSeek reads send.txt and writes the version it would
ship; Qwen merges the two in the chat it drafted in; then GLM reads send2.txt -- on its own,
before it is asked anything -- and does the same over the script the first two settled on, which
Qwen merges once more. What ships is a script two independent readers have signed off on.

This module is the second reader's half of what `bridge` is for the first: its credential, its
model, its thinking setting, its brief and its chip. The chain that drives it -- the seed, the
rounds, the merges -- is in `server.py` next to the first reader's, because it is the same
protocol run twice over the same script.

The endpoint is Z.AI's platform API, which is OpenAI-shaped, so there is no custom transport
here. Note what is deliberately *not* here: chat.z.ai's own chat endpoint now requires a captcha
parameter and a signed `X-Signature` header produced by the site's own bundle, so a session
token from that site cannot be driven from a server at all. ZAI_TOKEN is an API key from z.ai
(Z.AI Open Platform -> API Keys), shaped `id.secret`.
"""
from bridge import *  # noqa: F401,F403 -- env(), Provider, failure_reason, BRIEF, the warning

# --- the credential and the model ---------------------------------------------------------

ZAI_TOKEN = env("ZAI_TOKEN", "ZAI_API_KEY", "GLM_TOKEN", "GLM_API_KEY", "ZHIPU_TOKEN")
ZAI_URL = env("ZAI_URL", "GLM_URL", default="https://api.z.ai/api/paas/v4")
# GLM-5.3 Flash. `ZAI_MODEL` is one variable to change if the platform renames it; /health lists
# what the key can actually see and offers the nearest name when this one is not served.
ZAI_MODEL = env("ZAI_MODEL", "GLM_MODEL", default="glm-5.3-flash")
# Deep think, at the top of its ladder: thinking on, reasoning effort "max". The 5.3 Flash model
# can only think -- it has no free/skip-thinking mode -- and reasoning_effort is what sets the
# depth, so "max" is the strongest it has. "off" is accepted for a model that allows it.
ZAI_THINKING = env("ZAI_THINKING", "ZAI_EFFORT", default="max").lower()
ZAI_OFF = ("off", "0", "false", "no", "disabled", "none")
ZAI_EFFORTS = ("max", "high", "low")
# A reader is not asked to be creative: it is asked to be right about what runs.
SECOND_TEMPERATURE = min(1.0, max(0.0, float(env("SECOND_TEMPERATURE", "ZAI_TEMPERATURE",
                                                 default="0.3"))))
# A whole script, with the thinking in front of it, so it gets the room one needs on this API
# (the ceiling is 131072; this is enough for any script and keeps a runaway answer finite).
SECOND_TOKENS = int(env("SECOND_TOKENS", "ZAI_TOKENS", default="16384"))
SECOND_SEED_TOKENS = int(env("SECOND_SEED_TOKENS", "ZAI_SEED_TOKENS", default="512"))
# No ceiling on a call, like the rest of the chain: a slow reader is not an error, and cutting a
# script off half way is worse than waiting for it. Connecting is still bounded.
ZAI_TIMEOUT = float(env("ZAI_TIMEOUT", default="0"))


def zai_dialect() -> dict:
    """Thinking on at the top of its ladder, and nothing else.

    No `tools` are ever sent on this path, so there is no search either: the only thing this
    reader is given is the script in front of it. `clear_thinking` is the platform's own default
    (the earlier turns' reasoning is dropped, so a long conversation does not grow without end).
    """
    if ZAI_THINKING in ZAI_OFF:
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled", "clear_thinking": True},
            "reasoning_effort": ZAI_THINKING if ZAI_THINKING in ZAI_EFFORTS else "max"}


ZAI = Provider("zai", ZAI_URL, ZAI_TOKEN, ZAI_MODEL, zai_dialect(), ZAI_TIMEOUT, {}, "openai")


def zai_failure(status: int, body: str) -> str:
    """Z.AI's own sentence for a failure, plus the fix that is specific to it.

    The one that matters: a chat.z.ai session token (a JWT) sent as an API key. That token is
    real, and it is for a site this service cannot drive -- its chat endpoint wants a captcha and
    a signature from its own bundle -- so the answer has to be "use an API key", not "the token
    is broken" or "try another endpoint".
    """
    detail = failure_reason(status, body, ZAI)
    looks_like_key = bool(ZAI_TOKEN) and ("." in ZAI_TOKEN or ZAI_TOKEN.startswith("sk-"))
    if status in (401, 403) and not looks_like_key:
        return (f"{ZAI.label()} rejected ZAI_TOKEN ({detail}) -- this has to be an API key from "
                "z.ai (Z.AI Open Platform -> API Keys), not a chat.z.ai session token: that "
                "site's own chat endpoint demands a captcha and a signed request, so only the "
                "platform API is driven from here")
    if status == 404:
        return (f"{ZAI_MODEL} is not a model {ZAI.label()} serves ({detail}) -- set ZAI_MODEL to "
                "one it does (glm-5.3, glm-5.3-flash, glm-4.7-flash, ...); the chip on /health "
                "lists what this key can see")
    return detail


# --- how many rounds the second reader gets -----------------------------------------------

# Five is the ceiling, the same as the first reader's: more than that is a turn nobody sits
# through. Two is the default, which is one version of its own and then one chance to agree with
# what came back -- the shortest chain that ends in an agreement rather than in a version.
MAX_SECOND_ROUNDS = 5
SECOND_ROUNDS = max(0, min(int(env("SECOND_ROUNDS", "ZAI_ROUNDS", default="2")),
                           MAX_SECOND_ROUNDS))
# Whether send2.txt goes out on its own first, with the request only after its answer. On by
# default, and off only to save the extra call.
SECOND_SEED = env("SECOND_SEED", "ZAI_SEED", default="on").lower() not in (
    "off", "0", "false", "no")


def second_enabled() -> bool:
    """Whether the chain has this third model to call, and whether it should.

    Same rules as the first reader: a token, a model and a URL, and the pipeline not switched
    off. Rounds of 0 is a valid way to say "two models are enough".
    """
    if not ZAI.configured or SECOND_ROUNDS <= 0:
        return False
    return PIPELINE not in ("off", "0", "false", "no")


# --- the second reader's brief ------------------------------------------------------------
#
# send2.txt, read at boot and sent on its own before anything is asked -- the same rule Send.txt
# gets. A missing file is not fatal: the first reader's brief is sent instead, because the same
# standing instructions are worth more than none, and /health names which file actually went out
# so a brief that did not make it into the image is visible rather than silent.

SECOND_BRIEF_MAX = int(env("SECOND_BRIEF_MAX", default=str(REVIEW_BRIEF_MAX)))
_HERE = Path(__file__).parent
SECOND_BRIEF_CHOICES = [_HERE / name for name in ("send2.txt", "Send2.txt")]


def read_brief(path: Path) -> str:
    """One brief file, capped, or "" when it is not there."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > SECOND_BRIEF_MAX:
        text = text[:SECOND_BRIEF_MAX] + "\n\n[... brief truncated ...]"
    return text


FALLBACK_BRIEF_NAME = (f"{BRIEF_PATH.name} (send2.txt is not in the image)" if BRIEF
                       else "the built-in rubric")


def pick_second_brief() -> tuple:
    """The second reader's brief: send2.txt when it is there, the first reader's otherwise.

    Returns the text, the name to show, and whether it fell back -- so the chip says `send2.txt`
    or says plainly that send.txt is standing in for it.
    """
    asked = env("ZAI_BRIEF", "SECOND_BRIEF", "REVIEW2_BRIEF")
    if asked:
        text = read_brief(Path(asked))
        if text:
            return text, Path(asked).name, False
        print(f"[zai] {asked} could not be read; falling back to {FALLBACK_BRIEF_NAME}",
              flush=True)
    for path in SECOND_BRIEF_CHOICES:
        text = read_brief(path)
        if text:
            return text, path.name, False
    return BRIEF, FALLBACK_BRIEF_NAME, True


SECOND_BRIEF, SECOND_BRIEF_NAME, SECOND_BRIEF_FALLBACK = pick_second_brief()


# --- whether it is working right now ------------------------------------------------------
#
# The same two questions the other two accounts are asked: does the key work, and does the model
# exist? A retired model id and a rejected key look identical from the page -- the stage simply
# never answers -- so the model list is read as well, and the nearest served name is offered when
# the configured one is not in it.

_second_failure = {"detail": ""}
_second_failure_lock = threading.Lock()
_second: dict = {"at": 0.0, "ok": False, "detail": "not checked"}
_second_probe_lock = threading.Lock()


def note_failure(text: str) -> None:
    """Remember that the second reader just failed, so its chip says so instead of going quiet."""
    with _second_failure_lock:
        _second_failure["detail"] = (text or "")[:400]


def last_failure() -> str:
    with _second_failure_lock:
        return _second_failure["detail"]


def _model_ids(response) -> list:
    """The ids out of an OpenAI-shaped /models answer, whichever wrapper they came in."""
    try:
        payload = response.json()
    except ValueError:
        return []
    items = payload if isinstance(payload, list) else (payload.get("data")
                                                       or payload.get("models") or [])
    ids = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
        elif isinstance(item, str):
            ids.append(item)
    return ids


def _nearest(seen: list) -> str:
    """A served name to suggest when the configured one is not there."""
    stem = ZAI_MODEL.split("-flash")[0].split("-")[0]
    for hint in ("flash", stem):
        for name in seen:
            if hint in name.lower():
                return name
    return seen[0] if seen else ""


def _probe(force: bool = False) -> dict:
    """Ask z.ai whether ZAI_TOKEN works and whether ZAI_MODEL is served, remembered briefly."""
    with _second_probe_lock:
        cached = dict(_second)
    if not force and cached["at"] and time.time() - cached["at"] < TOKEN_CHECK_TTL:
        return cached
    if not ZAI.configured:
        state = {"at": time.time(), "ok": False, "detail": "no ZAI_TOKEN set"}
    else:
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0),
                              follow_redirects=True) as c:
                r = c.get(f"{ZAI.url}/models", headers=ZAI.headers())
            if r.status_code == 404:
                # A deployment without /models; the stage itself still reports for real.
                state = {"at": time.time(), "ok": True, "detail": "key set"}
            elif r.status_code >= 400:
                state = {"at": time.time(), "ok": False,
                         "detail": zai_failure(r.status_code, (r.text or "")[:300])}
            else:
                seen = _model_ids(r)
                if not seen:
                    state = {"at": time.time(), "ok": True, "detail": "key set"}
                elif ZAI_MODEL in seen:
                    state = {"at": time.time(), "ok": True, "detail": "key set, model served"}
                else:
                    hint = _nearest(seen)
                    state = {"at": time.time(), "ok": False,
                             "detail": (f"{ZAI_MODEL} is not served"
                                        + (f" -- try {hint}" if hint else ""))}
        except httpx.HTTPError as e:
            state = {"at": time.time(), "ok": False,
                     "detail": f"cannot reach {ZAI.label()} ({e.__class__.__name__})"}
    with _second_probe_lock:
        _second.update(state)
    return state


def second_state(force: bool = False) -> dict:
    """Everything the page and /health report about the second reader.

    A stage that just failed is the more useful answer than a key that works, and it is the
    failure that would otherwise look like a reader with nothing to say -- the chain ships the
    script that stands either way.
    """
    state = _probe(force)
    note = last_failure()
    if note and state["ok"]:
        state = {"at": state["at"], "ok": False, "detail": note}
    return {**state, "model": ZAI_MODEL, "endpoint": ZAI_URL, "configured": ZAI.configured,
            "thinking": ZAI_THINKING, "rounds": SECOND_ROUNDS, "seed": SECOND_SEED,
            "brief": SECOND_BRIEF_NAME, "brief_chars": len(SECOND_BRIEF),
            "brief_fallback": SECOND_BRIEF_FALLBACK, "search": False, "on": second_enabled()}
