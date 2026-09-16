"""What the service can say about itself, and how many people may use it at once.

Three accounts, three things that can be wrong, and each one is invisible from the page when it
is: an expired Qwen token and a hung generation look the same; a retired reviewer model and a
rejected key look the same; and a review that silently did not happen looks like a reviewer with
nothing to say. So each is checked, remembered for a minute (the page polls /health every few
seconds) and reported by name.

The last section is the two limits the page depends on -- a per-IP window and a ceiling on chains
running at once -- which is the same kind of state: something the endpoints read, never the chain
itself.

Everything it needs comes from `bridge`, which is where the providers and the ceilings live.
"""
from bridge import *  # noqa: F401,F403 -- env(), httpx, QWEN, REVIEWER, the config


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
    never answers), so the reviewer is checked the same way the Qwen token is.
    """
    with _reviewer_lock:
        cached = dict(_reviewer)
    if not force and cached["at"] and time.time() - cached["at"] < TOKEN_CHECK_TTL:
        return cached
    if not REVIEWER.configured:
        state = {"at": time.time(), "ok": False, "detail": "no reviewer token set"}
    elif REVIEWER.web is not None:
        # chat.deepseek.com answers /users/current, which is the same question the Qwen token is
        # asked, so the chip means the same thing on both sides.
        try:
            ok, detail = REVIEWER.web.validate()
        except HTTPException as e:
            ok, detail = False, str(e.detail)
        state = {"at": time.time(), "ok": ok, "detail": detail}
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
            "rounds": NEGOTIATE_ROUNDS, "seed": SEED_BRIEF, "choices": CHOICE_ROUNDS}


# --- what went wrong, and how long it stays said ----------------------------------------
#
# The last thing that went wrong, so /health (and the page's chip) can report it long after the
# error frame has scrolled by. Cleared by the next turn that succeeds.

_last_error = ""
_last_error_lock = threading.Lock()
# A reviewer that fails is not a failed turn -- the script that stands still goes out -- but it is
# not nothing either: a review silently not happening looks exactly like a reviewer with nothing
# to say. So it gets its own note, shown on the reviewer chip until a review works.
_review_note = ""
_review_note_lock = threading.Lock()


def note_error(text: str) -> None:
    global _last_error
    with _last_error_lock:
        _last_error = text[:300]


def last_error() -> str:
    with _last_error_lock:
        return _last_error


def note_review_error(text: str) -> None:
    global _review_note
    with _review_note_lock:
        _review_note = text[:300]


def review_note() -> str:
    with _review_note_lock:
        return _review_note


# --- how many people can do this at once -------------------------------------------------
#
# The page needs no login, so the URL is the only thing standing between a stranger and your
# Qwen account plus your reviewer credits. A key was the other option; this is what has to carry
# it instead: a per-IP window, and a ceiling on chains running at the same time.

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


def rate_ok(ip: str) -> bool:  # per IP, per minute
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


def running_now() -> int:
    """How many chains are running, for /health."""
    with _running_lock:
        return _running["now"]
