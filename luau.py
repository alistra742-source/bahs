"""The toolbox: everything the model can ask this service to do *to* a script.

One model writes the Luau now, so the thing that used to be a second reader has become a set of
tools it calls on itself: check its own script, look up whether the API it just used exists,
patch a line instead of rewriting the file, look for credentials before shipping, format it, and
-- when an executor is listening -- actually run it and read the error back.

Nothing here pretends to be more than it is. There is no Luau runtime in this image, so
`luau_check` is structural (blocks, strings, brackets, known-bad calls) and says so; the only
thing that can prove a script runs is the executor, which is what `run_script` is for.

The live run is the one piece that is not pure: the model queues a script, a Roblox client
(client.lua) polls /agent/pull for it, runs it, and posts the console output and the traceback to
/agent/push. `run_script` waits for that answer and hands it back to the model as a tool result,
which is what turns "write a script" into "write it, run it, read the error, fix it".

    luau_check(script)                      structural read of the script
    luau_format(script)                     re-indent by block depth
    roblox_api(query, class_name, member)   the real API dump: does that member exist, and how
    apply_edit(script, find, replace)       one targeted edit, instead of the whole file again
    secret_scan(script)                     what must not ship in the script
    run_script(script, timeout)             run it in the connected executor and read the output
"""
from bridge import env, redact  # the same env(), and the same masking the chain already uses

import httpx, json, re, threading, time, uuid
from collections import deque
from pathlib import Path
from typing import Optional


# --- the schemas the model is handed ------------------------------------------------------
#
# What the model sees. Descriptions are short on purpose: the proxy prompts the model with these,
# and a long description is prompt budget spent on every call.

SCRIPT_ARG = {"type": "string", "description": "the complete Luau script, whole"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "luau_check",
            "description": ("Check a Luau script for structural errors before running it: "
                            "unbalanced blocks (function/if/do/for/while/repeat against end/"
                            "until), unterminated strings, unbalanced brackets, and calls that "
                            "break in a Roblox executor. Returns errors and warnings with line "
                            "numbers, or says it is clean."),
            "parameters": {
                "type": "object",
                "properties": {"script": SCRIPT_ARG},
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "luau_format",
            "description": ("Re-indent a whole Luau script by block depth. Use it when the script "
                            "was assembled from pieces; it never changes code, only leading "
                            "whitespace."),
            "parameters": {
                "type": "object",
                "properties": {"script": SCRIPT_ARG},
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "roblox_api",
            "description": ("Look up the real Roblox API from the API dump: does this class, "
                            "property, function or event exist, on what class, with what "
                            "parameters, and is it deprecated or restricted. Use it instead of "
                            "guessing a member name."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "class or member name, or part of one"},
                    "class_name": {"type": "string",
                                   "description": "exact class to list members of, e.g. Part"},
                    "member": {"type": "string",
                               "description": "member to look for, e.g. GetPropertyChangedSignal"},
                    "limit": {"type": "integer", "description": "how many members to show"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_edit",
            "description": ("Replace an exact piece of a script with another and get the whole "
                            "edited script back. The cheap way to change one line: the find text "
                            "has to match exactly once unless all=true."),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": SCRIPT_ARG,
                    "find": {"type": "string", "description": "the exact text to replace"},
                    "replace": {"type": "string", "description": "what to put in its place"},
                    "all": {"type": "boolean",
                            "description": "replace every match instead of refusing an ambiguous one"},
                },
                "required": ["script", "find", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "secret_scan",
            "description": ("Scan a script for credentials that must not ship (webhooks, tokens, "
                            "api keys, long hex). Reports the line and the kind, never the value."),
            "parameters": {
                "type": "object",
                "properties": {"script": SCRIPT_ARG},
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": ("Run the script in the Roblox executor that is listening, and return "
                            "what it printed and any traceback. This is the only real test there "
                            "is: use it before claiming the script works. Returns straight away "
                            "when no executor is connected."),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": SCRIPT_ARG,
                    "timeout": {"type": "integer",
                                "description": "seconds to wait for the run (default 45)"},
                },
                "required": ["script"],
            },
        },
    },
]

TOOL_NAMES = [tool["function"]["name"] for tool in TOOLS]


def tools_enabled() -> bool:
    return env("AGENT_TOOLS", default="on").lower() not in ("off", "0", "false", "no")


# --- masking: code without the strings and comments ----------------------------------------
#
# Every structural check has to look at code, not at what the code says: a ")" inside a comment or
# an "end" inside a string is not code. Masking replaces strings and comments with spaces while
# keeping every line break and every column, so line numbers stay honest.

LONG_OPEN = re.compile(r"\[(=*)\[")


def mask(text: str, strings: bool = True) -> tuple:
    """The script with its strings and comments blanked out, plus what was left unterminated.

    Returns (masked, problems). The masked text is the same length as the original and keeps its
    newlines, so a position in one is a position in the other.

    `strings=False` blanks the comments but *keeps* the strings, which is what a check about the
    contents of a string needs: `game:GetService("ServerStorage")` is about the name, and the name
    is inside quotes. Those checks must not run over the string-masked text.
    """
    out = list(text)
    problems: list = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char == "-" and text.startswith("--", index):
            match = LONG_OPEN.match(text, index + 2)
            if match:
                closer = "]" + match.group(1) + "]"
                end = text.find(closer, match.end())
                if end < 0:
                    problems.append(f"a block comment opened on line {line_of(text, index)} "
                                    "is never closed")
                    end = size
                else:
                    end += len(closer)
            else:
                end = text.find("\n", index)
                end = size if end < 0 else end
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue
        if char in "\"'":
            if not strings:
                index += 1
                continue
            cursor = index + 1
            closed = False
            while cursor < size:
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text[cursor] == char:
                    closed = True
                    cursor += 1
                    break
                if text[cursor] == "\n":
                    break
                cursor += 1
            if not closed:
                problems.append(f"a string opened on line {line_of(text, index)} is never closed")
            for position in range(index, min(cursor, size)):
                if out[position] != "\n":
                    out[position] = " "
            index = cursor
            continue
        if char == "[":
            match = LONG_OPEN.match(text, index)
            if match and strings:
                closer = "]" + match.group(1) + "]"
                end = text.find(closer, match.end())
                if end < 0:
                    problems.append(f"a long string opened on line {line_of(text, index)} "
                                    "is never closed")
                    end = size
                else:
                    end += len(closer)
                for position in range(index, end):
                    if out[position] != "\n":
                        out[position] = " "
                index = end
                continue
        index += 1
    return "".join(out), problems


def line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


# --- the block walk ------------------------------------------------------------------------

WORD = re.compile(r"\b(function|elseif|else|if|then|for|while|do|repeat|until|end)\b")
CLOSERS = ("end", "until")
MIDDLE = ("else", "elseif")
# An opener that is only opened by the `do` that belongs to it: `for ... do` and `while ... do`
# are one block, not two, so the `do` is not counted again.
LOOP_PENDING = "loop-pending"


def walk(masked: str) -> dict:
    """Walk the block words line by line and check every block opens and closes once.

    Returns per-line depth information as well as the errors, because the formatter needs the
    same walk: one implementation means the two can never disagree about what nests inside what.
    """
    stack: list = []
    errors: list = []
    lines: list = []
    opened = 0
    for number, line in enumerate(masked.split("\n"), start=1):
        start = len(stack)  # how deep the line begins: what the formatter indents it to
        lead = 0            # how many closers this line opens with, for indentation
        opens = 0
        closes = 0
        seen = False        # an opener/closer has already been seen on this line
        for match in WORD.finditer(line):
            word = match.group(1)
            if word == "function":
                stack.append(("function", number))
                opens += 1
            elif word == "if":
                stack.append(("if", number))
                opens += 1
            elif word in ("for", "while"):
                stack.append((LOOP_PENDING, number))
                opens += 1
            elif word == "do":
                if stack and stack[-1][0] == LOOP_PENDING:
                    kind, opened = stack[-1]
                    stack[-1] = ("loop", opened)
                else:
                    stack.append(("do", number))
                    opens += 1
            elif word == "repeat":
                stack.append(("repeat", number))
                opens += 1
            elif word == "until":
                if stack and stack[-1][0] == "repeat":
                    stack.pop()
                else:
                    errors.append(f"line {number}: `until` closes a repeat that was never opened")
                closes += 1
                if not seen:
                    lead += 1
            elif word == "end":
                if not stack:
                    errors.append(f"line {number}: `end` closes a block that was never opened")
                elif stack[-1][0] == LOOP_PENDING:
                    errors.append(f"line {number}: the for/while on line {stack[-1][1]} "
                                  "never reached its `do`")
                    stack.pop()
                else:
                    stack.pop()
                closes += 1
                if not seen:
                    lead += 1
            elif word in MIDDLE:
                if not stack:
                    errors.append(f"line {number}: `{word}` outside any block")
                if not seen:
                    lead += 1
            seen = True
        opened += opens
        lines.append({"line": number, "lead": lead, "opens": opens, "closes": closes,
                      "depth": max(0, start - lead)})
    for kind, line_number in stack:
        errors.append(f"line {line_number}: a `{kind if kind != LOOP_PENDING else 'for/while'}` "
                      "is never closed")
    return {"lines": lines, "errors": errors, "blocks": opened, "unclosed": len(stack)}


def brackets(masked: str) -> list:
    """Unbalanced (), [] and {}: the classic 'the script is one character short'."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list = []
    for position, char in enumerate(masked):
        if char in "([{":
            stack.append((char, position))
        elif char in pairs:
            if not stack:
                return [f"line {line_of(masked, position)}: a stray `{char}`"]
            opener, opened_at = stack.pop()
            if opener != pairs[char]:
                return [f"line {line_of(masked, position)}: `{char}` closes the `{opener}` "
                        f"opened on line {line_of(masked, opened_at)}"]
    if stack:
        opener, opened_at = stack[-1]
        return [f"line {line_of(masked, opened_at)}: `{opener}` is never closed"]
    return []


# C: the check is about code, so it looks at the masked text. N: the check is about a name inside
# a string -- a service, say -- so it looks at the comment-free text, where strings are still there.
CHECKS = (
    (re.compile(r"(?<![\w.:])wait\s*\("), "C",
     "`wait()` is the old global; `task.wait()` is what an executor still runs"),
    (re.compile(r"(?<![\w.:])spawn\s*\("), "C",
     "`spawn()` is the old global; `task.spawn()` is the surviving one"),
    (re.compile(r"(?<![\w.:])delay\s*\("), "C",
     "`delay()` is the old global; `task.delay()` is the surviving one"),
    (re.compile(r":GetService\s*\(\s*[\"'](ServerStorage|ServerScriptService|ServerSecurity|"
                r"DataStoreService|MessagingService)"), "N",
     "a server-only service: an executor runs on the client and cannot see it"),
    (re.compile(r":GetService\s*\(\s*[\"'](ChangeHistoryService|StudioService|"
                r"PluginManager|PluginGuiService)"), "N",
     "a Studio-only service: it is not there in a live client, so this errors on the first line"),
    (re.compile(r"\bPlayers\.LocalPlayer\b"), "C",
     "`LocalPlayer` can still be nil at inject time; wait for it (or for a character) first"),
    (re.compile(r"\brequire\s*\(\s*\d"), "C",
     "`require(assetId)` is blocked in an executor; fetch the source and loadstring it instead"),
    (re.compile(r"\bloadstring\s*\("), "C",
     "`loadstring` throws at compile time on a syntax error: wrap the call in pcall and read the "
     "error"),
)
EXECUTOR_ONLY = re.compile(r"\b(getgenv|getrenv|getrawmetatable|setreadonly|hookfunction|"
                           r"checkcaller|isexecutorclosure|firetouchinterest|setclipboard)\b")


def check(script: str) -> dict:
    """Everything that can be said about a script without running it."""
    text = script or ""
    if not text.strip():
        return {"ok": False, "errors": ["the script is empty"], "warnings": [],
                "lines": 0, "blocks": 0}
    masked, problems = mask(text)
    walked = walk(masked)
    errors = list(problems) + walked["errors"] + brackets(masked)
    # Code checks look at the masked text; the ones about a name inside a string look at the
    # comment-free text, because the string is what they are about.
    named, _ = mask(text, strings=False)
    warnings: list = []
    for pattern, about, note in CHECKS:
        where = masked if about == "C" else named
        found = pattern.search(where)
        if found:
            warnings.append(f"line {line_of(where, found.start())}: {note}")
    if EXECUTOR_ONLY.search(masked) and "getgenv" in masked:
        warnings.append("`getgenv()` is executor-only: fine here, but the script will not run "
                        "in Roblox Studio")
    if re.search(r"while\s+true\s+do", masked) and not re.search(r"\btask\.wait\b|\bwait\b", masked):
        warnings.append("a `while true do` loop with no wait inside it will freeze the client")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "lines": len(text.splitlines()), "blocks": walked["blocks"]}


# A line that continues onto the next one: an operator with nothing after it, or a bracket left
# open. `then` and `do` are deliberately not here -- they end a header, they do not continue it.
CONTINUES = re.compile(r"(=|,|\.\.|\band\b|\bor\b|[-+*/%])\s*$")


def format_script(script: str) -> str:
    """Re-indent by block depth. Leading whitespace only; every other character is kept.

    The depth comes from the same walk the checker uses, so a script the checker is happy with is
    formatted the way it nests. A line that continues the one before it (an open bracket, a
    trailing operator) gets one more level, and a line that closes something (`)`, `}`, `end`)
    does not.
    """
    text = (script or "").replace("\r\n", "\n").replace("\t", "    ")
    if not text.strip():
        return ""
    masked, _ = mask(text)
    walked = walk(masked)
    masked_lines = masked.split("\n")
    out: list = []
    pending = 0            # inside an open bracket or after a trailing operator
    for index, raw in enumerate(text.split("\n")):
        stripped = raw.strip()
        info = walked["lines"][index]
        if not stripped:
            out.append("")
            continue
        line = masked_lines[index]
        # A closing bracket belongs with what it closes, so it does not take the extra level.
        here = info["depth"]
        if pending and stripped[0] not in ")]}":
            here += 1
        out.append("    " * max(0, here) + stripped)
        open_brackets = (line.count("(") + line.count("[") + line.count("{")
                         - line.count(")") - line.count("]") - line.count("}"))
        if open_brackets > 0 or CONTINUES.search(line):
            pending = 1
        elif pending and open_brackets <= 0:
            pending = 0
    return "\n".join(out).strip("\n") + "\n"


# --- the Roblox API dump --------------------------------------------------------------------
#
# The dump is the only honest answer to "does that member exist": it is the file Studio itself
# reads. It is a few megabytes, so it is fetched once and kept; nothing is written to disk in the
# image, and a fetch that fails is reported rather than guessed around.

DEFAULT_VERSION_URL = "https://setup.rbxcdn.com/versionQTStudio"
DEFAULT_DUMP_URL = ("https://setup.rbxcdn.com/{version}-API-Dump.json")
MIRROR_DUMP_URL = ("https://raw.githubusercontent.com/MaximumADHD/Roblox-Client-Tracker/"
                   "roblox/API-Dump.json")

_dump: dict = {"at": 0.0, "classes": {}, "error": "", "source": "", "version": ""}
_dump_lock = threading.Lock()


def dump_ttl() -> float:
    return float(env("ROBLOX_API_TTL", default="21600") or 21600)


def _read_dump() -> tuple:
    """(classes, source, version) or (None, error, "").

    A URL or file in ROBLOX_API_DUMP is used as given -- that is the offline path, and how this
    is tested. Left unset, the version endpoint names the current client build and the dump is
    fetched beside it, with the mirror as a fallback rather than as the first choice.
    """
    asked = env("ROBLOX_API_DUMP")
    version = ""
    sources: list = []
    if asked and not asked.startswith("http"):
        try:
            payload = json.loads(Path(asked).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return None, f"cannot read the dump at {asked} ({e.__class__.__name__})", ""
        return _classes_of(payload), asked, "local"
    if asked:
        sources.append(asked)
    else:
        try:
            with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0),
                              follow_redirects=True) as c:
                version = c.get(env("ROBLOX_API_URL", default=DEFAULT_VERSION_URL)).text.strip()
        except httpx.HTTPError:
            version = ""
        if version:
            sources.append(DEFAULT_DUMP_URL.format(version=version))
        sources.append(MIRROR_DUMP_URL)
    for url in sources:
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=8.0),
                              follow_redirects=True) as c:
                response = c.get(url)
            if response.status_code >= 400:
                continue
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            continue
        classes = _classes_of(payload)
        if classes:
            return classes, url, version
    return None, "no API dump could be fetched (the version endpoint and the mirror both failed)", ""


def _classes_of(payload) -> dict:
    classes = {}
    for item in (payload or {}).get("Classes") or []:
        if isinstance(item, dict) and item.get("Name"):
            classes[str(item["Name"])] = item
    return classes


def dump_peek() -> dict:
    """What is known about the dump right now, without fetching anything.

    /health is polled every few seconds, so it must never start a multi-megabyte download: the
    dump is fetched by the tool that needs it, and this only reports what that produced.
    """
    with _dump_lock:
        cached = dict(_dump)
    if cached["classes"]:
        return {"ok": True, "classes": len(cached["classes"]), "source": cached["source"],
                "version": cached["version"], "error": ""}
    return {"ok": False, "classes": 0, "source": "", "version": "",
            "error": cached["error"]}


def dump_state(force: bool = False) -> dict:
    """The dump, fetched if it is missing or old. Called by the tool, never by /health."""
    with _dump_lock:
        cached = dict(_dump)
    if not force and cached["classes"] and time.time() - cached["at"] < dump_ttl():
        return {"ok": True, "classes": len(cached["classes"]), "source": cached["source"],
                "version": cached["version"], "error": ""}
    if cached["error"] and not force and time.time() - cached["at"] < 300:
        return {"ok": False, "classes": 0, "source": "", "version": "", "error": cached["error"]}
    classes, source, version = _read_dump()
    with _dump_lock:
        _dump.update({"at": time.time(), "classes": classes or {}, "source": source,
                      "version": version, "error": "" if classes else source})
        state = dict(_dump)
    if not classes:
        print(f"[luau] the Roblox API dump is unavailable: {source}", flush=True)
        return {"ok": False, "classes": 0, "source": "", "version": "", "error": source}
    print(f"[luau] Roblox API dump: {len(classes)} classes from {source}", flush=True)
    return {"ok": True, "classes": len(classes), "source": source, "version": version, "error": ""}


def _type_name(spec) -> str:
    if isinstance(spec, str):
        return spec
    if not isinstance(spec, dict):
        return "?"
    category = str(spec.get("Category") or "")
    name = str(spec.get("Name") or "?")
    if category == "Group":
        return name.lower()          # Tuple / Array / Dictionary
    return name


def _signature(member: dict) -> str:
    kind = str(member.get("MemberType") or "")
    name = str(member.get("Name") or "")
    if kind == "Function":
        params = ", ".join(f"{p.get('Name')}: {_type_name(p.get('Type'))}"
                           for p in member.get("Parameters") or [])
        returns = member.get("ReturnType")
        made = _type_name(returns) if returns else "()"
        return f"{name}({params}) -> {made}"
    if kind in ("Event", "Callback"):
        params = ", ".join(f"{p.get('Name')}: {_type_name(p.get('Type'))}"
                           for p in member.get("Parameters") or [])
        return f"{name}({params})"
    value = member.get("ValueType")
    return f"{name} : {_type_name(value) if value else '?'}"


def _tags(member: dict) -> str:
    tags = [str(t) for t in member.get("Tags") or []]
    security = member.get("Security")
    if isinstance(security, dict):
        for side in ("Read", "Write"):
            level = str(security.get(side) or "None")
            if level != "None":
                tags.append(f"{side.lower()}:{level}")
    elif isinstance(security, str) and security != "None":
        tags.append(security)
    return (" [" + ", ".join(tags) + "]") if tags else ""


def _member_lines(cls: dict, member_filter: str = "", limit: int = 40) -> list:
    out: list = []
    for member in cls.get("Members") or []:
        if not isinstance(member, dict):
            continue
        name = str(member.get("Name") or "")
        if member_filter and member_filter.lower() not in name.lower():
            continue
        out.append(f"  {_signature(member)}{_tags(member)}")
    return out[:limit]


def api_lookup(query: str = "", class_name: str = "", member: str = "",
               limit: int = 40) -> str:
    """The answer to "does this exist": a named class, a member somewhere, or a search."""
    state = dump_state()
    if not state["ok"]:
        return ("the Roblox API dump is not available right now "
                f"({state['error']}), so this cannot be answered from the real API. "
                "Reason about the API yourself and say that you could not check it.")
    with _dump_lock:
        classes = dict(_dump["classes"])
    limit = max(1, min(int(limit or 40), 120))
    exact = {name.lower(): name for name in classes}
    if class_name:
        got = classes.get(exact.get(class_name.lower(), ""))
        if not got:
            near = [name for name in classes if class_name.lower() in name.lower()][:10]
            return (f"{class_name} is not a Roblox class."
                    + (f" Closest names: {', '.join(near)}" if near else ""))
        lines = _member_lines(got, member, limit)
        head = (f"{class_name} in the real API dump"
                + (f" (members matching {member!r})" if member else "")
                + (f", first {limit}" if len(lines) == limit else ""))
        return f"{head}:\n" + "\n".join(lines or ["  (no member matched)"])
    needle = (member or query or "").lower()
    if not needle:
        return "ask for a class_name, a member, or a query"
    found: list = []
    for name in sorted(classes):
        for line in _member_lines(classes[name], needle, 6):
            found.append(f"{name}:{line.strip()}")
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break
    names = [name for name in sorted(classes) if needle in name.lower()][:limit]
    parts: list = []
    if names:
        parts.append("classes: " + ", ".join(names))
    if found:
        parts.append("members:\n  " + "\n  ".join(found))
    if not parts:
        return f"nothing in the API dump matches {needle!r}"
    return "\n".join(parts)


# --- the live run: the executor on the other end ---------------------------------------------
#
# The one thing that can prove a script runs is Roblox running it. The queue is deliberately
# simple -- one deque, one result per run id -- because everything about it is short-lived: a run
# that nobody collected is dropped, and a client that stopped polling is forgotten by its own
# timestamp rather than by anything it has to say.

_runs: dict = {}
_queue: deque = deque()
_lock = threading.Lock()
_cond = threading.Condition(_lock)
_clients: dict = {}
RUN_TIMEOUT = 45.0


def run_timeout() -> float:
    return float(env("RUN_TIMEOUT", default="45") or 45)


def executor_idle() -> float:
    return float(env("EXECUTOR_IDLE", default="90") or 90)


def _live_clients() -> list:
    now = time.time()
    return [name for name, seen in _clients.items() if now - seen < executor_idle()]


def queue_script(script: str, session: str = "", timeout: float = 0) -> str:
    """Put one script in front of the executor. Returns its run id."""
    run_id = uuid.uuid4().hex[:10]
    with _cond:
        _runs[run_id] = {"id": run_id, "script": script, "session": session,
                         "queued": time.time(), "result": None,
                         "timeout": timeout or run_timeout()}
        _queue.append(run_id)
        # Never let a client that stopped listening turn this into a leak.
        for old in [k for k, v in _runs.items() if time.time() - v["queued"] > 900]:
            _runs.pop(old, None)
        _cond.notify_all()
    return run_id


def take_script(client: str) -> Optional[dict]:
    """One script for a client that asked for work, or None when there is nothing to run."""
    with _cond:
        _clients[client or "?"] = time.time()
        while _queue:
            run_id = _queue.popleft()
            run = _runs.get(run_id)
            if run and run["result"] is None:
                run["taken"] = time.time()
                return {"run": run_id, "script": run["script"]}
    return None


def deliver(run_id: str, ok: bool, output: str = "", error: str = "") -> bool:
    """The executor's answer for one run. False when the run is unknown or already answered."""
    with _cond:
        run = _runs.get(run_id)
        if not run or run["result"] is not None:
            return False
        run["result"] = {"ok": bool(ok), "output": output or "", "error": error or ""}
        _cond.notify_all()
        return True


def wait_script(run_id: str, timeout: float = 0) -> Optional[dict]:
    """Block until the executor answers, or the wait runs out. None means it never did."""
    deadline = time.time() + (timeout or run_timeout())
    with _cond:
        run = _runs.get(run_id)
        if not run:
            return None
        while run["result"] is None and run.get("cancel") is None:
            left = deadline - time.time()
            if left <= 0:
                return None
            _cond.wait(timeout=min(left, 1.0))
        return run["result"]


def executor_state() -> dict:
    live = _live_clients()
    with _lock:
        waiting = len([r for r in _runs.values() if r["result"] is None])
    return {"ok": bool(live), "clients": live, "waiting": waiting}


# --- the dispatch table ----------------------------------------------------------------------

def _text(title: str, body: str) -> str:
    return f"{title}\n{body}" if body else title


def run(name: str, arguments: dict, session: str = "") -> dict:
    """Run one tool call and give back something a model can act on.

    Never raises: a tool that fails has to come back as a *result* the model can read and work
    around -- a raised exception would end the whole turn instead of the one call.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    try:
        if name == "luau_check":
            script = str(arguments.get("script") or "")
            result = check(script)
            head = (f"{result['lines']} line(s), {result['blocks']} block(s): "
                    + ("no structural problem found" if result["ok"] else
                       f"{len(result['errors'])} error(s)"))
            body = "\n".join([f"error: {e}" for e in result["errors"]]
                             + [f"warning: {w}" for w in result["warnings"]])
            return {"ok": result["ok"], "summary": head, "output": _text(head, body)}
        if name == "luau_format":
            script = str(arguments.get("script") or "")
            if not script.strip():
                return {"ok": False, "summary": "nothing to format", "output": "the script is empty"}
            formatted = format_script(script)
            return {"ok": True, "summary": f"re-indented {len(formatted.splitlines())} line(s)",
                    "output": formatted}
        if name == "roblox_api":
            answer = api_lookup(str(arguments.get("query") or ""),
                                str(arguments.get("class_name") or ""),
                                str(arguments.get("member") or ""),
                                int(arguments.get("limit") or 40))
            ok = not answer.startswith("the Roblox API dump is not available")
            return {"ok": ok, "summary": answer.splitlines()[0][:200], "output": answer}
        if name == "apply_edit":
            script = str(arguments.get("script") or "")
            find = str(arguments.get("find") or "")
            replace = str(arguments.get("replace") or "")
            everything = bool(arguments.get("all"))
            if not find:
                return {"ok": False, "summary": "nothing to find",
                        "output": "`find` was empty, so nothing was replaced"}
            count = script.count(find)
            if not count:
                return {"ok": False, "summary": "the find text is not in the script",
                        "output": ("the `find` text does not appear in the script, so nothing "
                                   "was replaced. Read the script again (or fetch it whole) and "
                                   "copy the text exactly")}
            if count > 1 and not everything:
                return {"ok": False, "summary": f"{count} matches; refusing to guess",
                        "output": (f"`find` matches {count} times. Give more context so it "
                                   "matches once, or pass all=true to replace every match")}
            edited = script.replace(find, replace, -1 if everything else 1)
            return {"ok": True, "summary": f"replaced {count if everything else 1} occurrence(s)",
                    "output": edited}
        if name == "secret_scan":
            script = str(arguments.get("script") or "")
            masked, count = redact(script)
            if not count:
                return {"ok": True, "summary": "no credentials found",
                        "output": "no webhook, token, api key or long hex string was found"}
            lines: list = []
            for index, line in enumerate(script.splitlines(), start=1):
                if redact(line)[1]:
                    lines.append(f"line {index}: {line.strip()[:120]}")
            return {"ok": True, "summary": f"{count} credential(s) in the script",
                    "output": _text(f"{count} credential(s) found -- do not ship them",
                                    "\n".join(lines))}
        if name == "run_script":
            script = str(arguments.get("script") or "")
            if not script.strip():
                return {"ok": False, "summary": "nothing to run", "output": "the script is empty"}
            if env("AGENT_RUN", default="on").lower() in ("off", "0", "false", "no"):
                return {"ok": False, "summary": "running is switched off",
                        "output": "AGENT_RUN=off on this service, so nothing can be executed"}
            if not executor_state()["ok"]:
                return {"ok": False, "summary": "no executor is listening",
                        "output": ("no Roblox client is polling for work, so nothing can run. "
                                   "Write the script, check it with luau_check, and say that it "
                                   "was not executed")}
            seconds = float(arguments.get("timeout") or run_timeout())
            seconds = max(5.0, min(seconds, 300.0))
            run_id = queue_script(script, session, seconds)
            got = wait_script(run_id, seconds)
            if got is None:
                return {"ok": False, "summary": f"the run did not answer in {seconds:g}s",
                        "output": ("the script was queued and the executor never answered in "
                                   f"{seconds:g}s: it may be stuck in a wait, or the client "
                                   "stopped polling")}
            if got["ok"]:
                return {"ok": True, "summary": "the executor ran it without error",
                        "output": _text("the executor ran the script without error",
                                        "printed:\n" + (got["output"] or "(nothing printed)"))}
            return {"ok": False, "summary": "the run failed",
                    "output": _text("the executor reported an error",
                                    (got["error"] or "no detail")
                                    + ("\nprinted:\n" + got["output"] if got["output"] else ""))}
    except Exception as e:  # a broken tool is a tool result, never a dead turn
        return {"ok": False, "summary": f"{name} failed",
                "output": f"{name} could not run: {e.__class__.__name__}: {e}"}
    return {"ok": False, "summary": f"unknown tool {name}", "output": f"there is no tool {name!r}"}


def tool_state() -> dict:
    dump = dump_peek()
    executor = executor_state()
    return {
        "on": tools_enabled(),
        "names": TOOL_NAMES,
        "run": env("AGENT_RUN", default="on"),
        "dump_ok": dump["ok"],
        "dump_classes": dump["classes"],
        "dump_source": dump["source"],
        "dump_error": dump["error"],
        "executor_ok": executor["ok"],
        "executor_clients": executor["clients"],
        "executor_waiting": executor["waiting"],
    }
