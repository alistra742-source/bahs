"""What the service can say about itself, and how many people may use it at once.

Two credentials, two failures, and both are invisible from the page when they happen: an expired
Qwen token and an expired DeepSeek one look exactly like a mode that hung, and both of them look
like a question nobody answered. So each is checked, remembered for a minute (the page polls
/health every few seconds) and reported by name.

The rest is what the page depends on but never sees directly: the model list the proxy serves, the
last thing that went wrong, and the two limits -- a per-IP window and a ceiling on turns running
at once.

Everything it needs comes from `bridge`, which is where the provider and the ceilings live. What
the *toolbox* can say about itself (the API dump, the executor) is in `luau`, next to the tools
that own it.
"""
from bridge import *  # noqa: F401,F403 -- env(), httpx, QWEN, the config


# --- the model list, and whether the token still works ----------------------------------

_models: dict = {"at": 0.0, "ids": []}
_models_lock = threading.Lock()


def list_models(force: bool = False) -> list:
    """The proxy's model ids, remembered for a few minutes; empty when unreadable.

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


# --- the second model's credential ---------------------------------------------------------
#
# The same question the Qwen token is asked, for the same reason: a DeepSeek credential that has
# expired (a chat.deepseek.com userToken lasts weeks) and a model name the account is no longer
# entitled to both look, from the page, exactly like a mode that never answers.

_deepseek: dict = {"at": 0.0, "ok": False, "detail": "not checked"}
_deepseek_lock = threading.Lock()


def deepseek_state(force: bool = False) -> dict:
    """Whether the DeepSeek credential works, remembered briefly. Never raises."""
    with _deepseek_lock:
        cached = dict(_deepseek)
    if not force and cached["at"] and time.time() - cached["at"] < TOKEN_CHECK_TTL:
        return cached
    if not DEEPSEEK.configured:
        state = {"at": time.time(), "ok": False, "detail": "DEEPSEEK_TOKEN is not set"}
    elif DEEPSEEK.web is not None:
        # chat.deepseek.com answers /users/current, which is the same question the Qwen token is
        # asked, so the chip means the same thing on both sides.
        try:
            ok, detail = DEEPSEEK.web.validate()
        except HTTPException as e:
            ok, detail = False, str(e.detail)
        state = {"at": time.time(), "ok": ok, "detail": detail}
    else:
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as c:
                r = c.get(f"{DEEPSEEK.url}/models", headers=DEEPSEEK.headers())
            if r.status_code == 404:
                state = {"at": time.time(), "ok": True, "detail": "key set"}
            elif r.status_code >= 400:
                state = {"at": time.time(), "ok": False,
                         "detail": failure_reason(r.status_code, r.text[:300], DEEPSEEK)}
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
                if not seen or DEEPSEEK.model in seen:
                    state = {"at": time.time(), "ok": True,
                             "detail": "key set, model served" if seen else "key set"}
                else:
                    # A retired model id and a rejected key look alike from the page, so the chip
                    # names the nearest model the key can actually see.
                    near = [m for m in seen if "deepseek" in m.lower()]
                    hint = near[0] if near else seen[0]
                    state = {"at": time.time(), "ok": False,
                             "detail": f"{DEEPSEEK.model} is not served -- try {hint}"}
        except httpx.HTTPError as e:
            state = {"at": time.time(), "ok": False,
                     "detail": f"cannot reach {DEEPSEEK.url} ({e.__class__.__name__})"}
    with _deepseek_lock:
        _deepseek.update(state)
    return state


# --- what went wrong, and how long it stays said -----------------------------------------
#
# The last thing that went wrong, so /health (and the page's chip) can report it long after the
# error frame has scrolled by. Cleared by the next turn that succeeds.

_last_error = ""
_last_error_lock = threading.Lock()


def note_error(text: str) -> None:
    global _last_error
    with _last_error_lock:
        _last_error = text[:300]


def last_error() -> str:
    with _last_error_lock:
        return _last_error


# --- how many people can do this at once -------------------------------------------------
#
# The page needs no login, so the URL is the only thing standing between a stranger and the Qwen
# account behind it. A key was the other option; this is what has to carry it instead: a per-IP
# window, and a ceiling on turns running at the same time.

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


def slot_take() -> bool:  # a ceiling on turns running at once, so four people cannot exhaust it
    with _running_lock:
        if _running["now"] >= MAX_CONCURRENT:
            return False
        _running["now"] += 1
        return True


def slot_give() -> None:
    with _running_lock:
        _running["now"] = max(0, _running["now"] - 1)


def running_now() -> int:
    """How many turns are running, for /health."""
    with _running_lock:
        return _running["now"]
