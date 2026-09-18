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
    luau_find(script, pattern, context)     the lines that match, numbered -- one part of a script
    roblox_api(query, class_name, member)   the real API dump: does that member exist, and how
    apply_edit(script, find, replace)       one targeted edit, instead of the whole file again
    secret_scan(script)                     what must not ship in the script
    web_get(url, max_chars)                 read a page: docs, a DevForum answer, a raw file
    run_script(script, timeout)             run it in the connected executor and read the output
    luau_diff(a, b)                         the lines that differ between two versions
    plan_todo(action, text, index)          the writer's own list of what to build, and how far it got
    script_versions(action, name, script)   save the script under a name, and get it back later
    consult_planner(question, script)       ask the second model one question, when it wants one

The two that look outside the script -- `roblox_api` at the API dump and `web_get` at the open web
-- are the writer's only way to check something it cannot see from here: there is no Roblox in this
image, so a member is either in the dump or it is a guess, and how a thing is *used* is either on a
page it can read or it is a guess too.
"""
from bridge import (env, redact,  # the same env(), and the same masking the chain already uses
                    as_prompt, deepseek_chat, session_ttl, stream_call, strip_metadata,  # and the same second model
                    DEEPSEEK, DEEPSEEK_TEMPERATURE, DEEPSEEK_TOKENS)

import difflib, html as html_mod, httpx, json, re, threading, time, uuid
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
            "name": "luau_find",
            "description": ("Find the lines of a script that match a pattern, with line numbers and "
                            "optional context. The way to look at the one part of a long script "
                            "you are changing instead of reading the whole thing again."),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": SCRIPT_ARG,
                    "pattern": {"type": "string",
                                "description": ("a Lua pattern: 'FireServer' , '%.Name', "
                                                "'for .- do', '%d+'")},
                    "context": {"type": "integer",
                                "description": "lines to show after each match (default 0)"},
                },
                "required": ["script", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_get",
            "description": ("Fetch a page and read it as text: Roblox documentation, a DevForum "
                            "thread, a gist, a raw file. Use it when the answer depends on how "
                            "something is really used rather than on whether the member exists."),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "the http(s) URL to read"},
                    "max_chars": {"type": "integer",
                                  "description": "how much of it to keep (default 8000)"},
                },
                "required": ["url"],
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
    {
        "type": "function",
        "function": {
            "name": "plan_todo",
            "description": ("Your own list of what the script has to do, kept for the whole "
                            "session. Write the steps down first, tick them off as they are done, "
                            "and read it back before answering -- it is what keeps a long script "
                            "from losing half of itself between calls. Actions: add, done, undo, "
                            "clear, list."),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "add | done | undo | clear | list"},
                    "text": {"type": "string", "description": "the step, or words from one"},
                    "index": {"type": "integer", "description": "the step by number (1 is first)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "script_versions",
            "description": ("Save the script you have under a name and get it back later: keep one "
                            "that worked before changing it, and the way back does not depend on "
                            "your memory of it. Actions: save, load, list, drop, diff."),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "save | load | list | drop | diff"},
                    "name": {"type": "string", "description": "what to call it"},
                    "script": SCRIPT_ARG,
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "luau_diff",
            "description": ("The difference between two versions of a script, line by line, with "
                            "the count of lines added and removed. Use it after an edit to see "
                            "that it touched the one line it was meant to and nothing else."),
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "string", "description": "the script before"},
                    "b": {"type": "string", "description": "the script after"},
                    "from": {"type": "string", "description": "what to call the first one"},
                    "to": {"type": "string", "description": "what to call the second one"},
                },
                "required": ["a", "b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_planner",
            "description": ("Ask the second model one question in the middle of the work -- which "
                            "of two approaches, what a traceback means, how a member really "
                            "behaves. It has not been writing this script and answers only what "
                            "you ask, in prose. Use it for a decision, not for a review."),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "the one question"},
                    "script": SCRIPT_ARG,
                },
                "required": ["question"],
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


# --- what the writer keeps for itself: its plan, and the versions of its script -------------
#
# The tools either side of this are about a script; these are about *writing* one, which is not
# done in one breath. A plan written down and ticked off is the difference between working through
# four things and losing two of them between calls; the versions saved are the difference between
# going back to what worked and writing it again from memory. Both are per session, both live in
# this process, and both are forgotten with the session -- there is no database here either.
#
# `consult_planner` is the one place a second model is asked on purpose. It is not the reader that
# used to review every answer (that is gone, and a second opinion nobody asked for is not worth a
# call): it is a question the writer chooses to ask, answered in prose as an ordinary tool result,
# which the writer is free to take or leave.

_memory: dict = {"plan": {}, "versions": {}}
_memory_lock = threading.Lock()
PLAN_MAX = 12
VERSION_MAX = 12
VERSION_CHARS = 60000


def _memory_get(session: str) -> dict:
    """This session's plan and its saved scripts, made on first use and dropped when stale."""
    key = (session or "")[:64] or "-"
    now = time.time()
    with _memory_lock:
        for store in ("plan", "versions"):
            for old in [k for k, v in _memory[store].items() if now - v["at"] > session_ttl()]:
                _memory[store].pop(old, None)
        plan = _memory["plan"].setdefault(key, {"at": now, "items": []})
        versions = _memory["versions"].setdefault(key, {"at": now, "saved": {}, "order": []})
        plan["at"] = versions["at"] = now
    return {"plan": plan, "versions": versions}


def _plan_text(items: list, title: str = "the plan") -> str:
    if not items:
        return f"{title} is empty"
    out = [f"{title} -- {len([i for i in items if i['done']])}/{len(items)} done"]
    for index, item in enumerate(items, start=1):
        out.append(f"  {index}. [{'x' if item['done'] else ' '}] {item['text']}")
    return "\n".join(out)


def _plan_pick(items: list, text: str, index: int) -> Optional[dict]:
    """The item a call means: by number when it gave one, and otherwise by its own words."""
    if index:
        return items[index - 1] if 1 <= index <= len(items) else None
    wanted = " ".join(str(text or "").split()).lower()
    if not wanted:
        return None
    exact = [i for i in items if i["text"].lower() == wanted]
    if exact:
        return exact[0]
    part = [i for i in items if wanted in i["text"].lower()]
    return part[0] if len(part) == 1 else None


def plan_todo(action: str = "", text: str = "", index: int = 0, session: str = "") -> str:
    """The writer's own list of what it is building, and how far it has got.

    Kept for the session rather than for one turn, because the point of it is the tool round
    *after* this one: what a call in the middle of a long script needs to know is what is still
    left. Actions: add, done, undo, clear, list.
    """
    items = _memory_get(session)["plan"]["items"]
    act = " ".join(str(action or "").split()).lower() or ("add" if str(text or "").strip() else "list")
    if act in ("add", "new", "todo"):
        line = " ".join(str(text or "").split())[:200]
        if not line:
            return "nothing was added: `text` was empty"
        if len(items) >= PLAN_MAX:
            return (f"the plan is full ({PLAN_MAX} items): finish one or clear the plan before "
                    "adding another")
        items.append({"text": line, "done": False})
    elif act in ("done", "finish", "tick", "ticked"):
        found = _plan_pick(items, text, index)
        if found is None:
            return _plan_text(items, "no item matched that -- the plan is")
        found["done"] = True
    elif act in ("undo", "reopen", "undone"):
        found = _plan_pick(items, text, index)
        if found is None:
            return _plan_text(items, "no item matched that -- the plan is")
        found["done"] = False
    elif act in ("clear", "reset", "drop"):
        items.clear()
    elif act not in ("list", "show", "get"):
        return f"unknown action {act!r}: add, done, undo, clear or list"
    return _plan_text(items)


def _versions_text(saved: dict, order: list) -> str:
    if not saved:
        return "nothing saved yet: script_versions(save) keeps the script you have"
    out = [f"{len(saved)} saved version(s), newest last:"]
    for name in [n for n in order if n in saved]:
        held = saved[name]
        out.append(f"  {name}: {len(held['script'].splitlines())} line(s), "
                   f"{len(held['script'])} char(s), {int(time.time() - held['at'])}s ago")
    return "\n".join(out)


def _version_pick(saved: dict, order: list, name: str) -> Optional[str]:
    """The saved name a call means: exactly, or by being the only one it could be."""
    wanted = " ".join(str(name or "").split())
    if not wanted:
        return order[-1] if order else None
    exact = [n for n in order if n.lower() == wanted.lower()]
    if exact:
        return exact[0]
    part = [n for n in order if wanted.lower() in n.lower()]
    return part[0] if len(part) == 1 else None


def script_versions(action: str = "", name: str = "", script: str = "", session: str = "") -> str:
    """Save the script under a name, get it back later, and see how two of them differ.

    The reason this is a tool and not a habit: the model's own context is the only place the
    earlier version lived, and a version it has to hold in its head is one it stops looking at.
    Actions: save, load, list, drop, diff (against the `script` it sent, or the newest other one).
    """
    store = _memory_get(session)["versions"]
    saved, order = store["saved"], store["order"]
    act = " ".join(str(action or "").split()).lower() or ("save" if str(script or "").strip() else "list")
    label = " ".join(str(name or "").split())[:60]

    if act in ("save", "keep", "add", "store"):
        body = str(script or "")
        if not body.strip():
            return "nothing was saved: send the script in `script`"
        if len(body) > VERSION_CHARS:
            return f"that script is {len(body)} characters, past the {VERSION_CHARS} kept here"
        key = label or f"v{len(order) + 1}"
        saved[key] = {"script": body, "at": time.time()}
        if key not in order:
            order.append(key)
        while len(order) > VERSION_MAX:
            saved.pop(order.pop(0), None)
        for stale in [n for n in list(saved) if n not in order]:
            saved.pop(stale, None)
        return f"saved {key!r} ({len(body.splitlines())} lines)\n" + _versions_text(saved, order)

    if act in ("load", "get", "back", "read"):
        key = _version_pick(saved, order, label)
        if key is None:
            return _versions_text(saved, order) + "\n\n-- no single version matched that name"
        return f"--- {key} (saved {int(time.time() - saved[key]['at'])}s ago) ---\n{saved[key]['script']}"

    if act in ("drop", "delete", "forget"):
        key = _version_pick(saved, order, label)
        if key is None:
            return _versions_text(saved, order)
        saved.pop(key, None)
        order.remove(key)
        return f"dropped {key!r}\n" + _versions_text(saved, order)

    if act in ("diff", "compare"):
        key = _version_pick(saved, order, label)
        if key is None:
            return _versions_text(saved, order) + "\n\n-- no single version matched that name"
        other = str(script or "")
        if not other.strip():
            rest = [n for n in order if n != key]
            if not rest:
                return f"only one version is saved, so there is nothing to compare {key!r} with"
            other, other_name = saved[rest[-1]]["script"], rest[-1]
        else:
            other_name = "the script you sent"
        return f"--- {key} vs {other_name} ---\n" + unified_diff(saved[key]["script"], other,
                                                                key, other_name)

    if act not in ("list", "show"):
        return f"unknown action {act!r}: save, load, list, drop or diff"
    return _versions_text(saved, order)


def unified_diff(a: str, b: str, name_a: str = "before", name_b: str = "after",
                 context: int = 2, limit: int = 400) -> str:
    """Two scripts, and the lines that differ between them -- nothing else.

    For the question an edit raises and nothing else answers: did that change touch the one line
    it was meant to, and did anything else move with it.
    """
    lines = list(difflib.unified_diff(str(a or "").splitlines(), str(b or "").splitlines(),
                                      fromfile=name_a, tofile=name_b, lineterm="", n=context))
    if not lines:
        return "identical: the two scripts are the same text, line for line"
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more diff line(s) ..."]
    added = len([line for line in lines if line.startswith("+") and not line.startswith("+++")])
    removed = len([line for line in lines if line.startswith("-") and not line.startswith("---")])
    head = f"{added} line(s) added, {removed} removed"
    return _text(head, "\n".join(lines))


def consult_planner(question: str, script: str = "", session: str = "") -> str:
    """Ask the planner model one question, when the writer wants a second opinion.

    Not a review of the answer -- nobody asked for one, which is why the automatic second reader is
    gone -- but a question the writer chooses: which of two approaches, what a traceback means,
    whether a member behaves the way it assumed. It is answered in prose and handed back as an
    ordinary tool result, so the writer can ignore it.
    """
    asked = " ".join(str(question or "").split())
    if not asked:
        return "nothing was asked: send the question in `question`"
    if not DEEPSEEK.configured:
        return ("there is no second model on this service: DEEPSEEK_TOKEN is not set, so the "
                "planner cannot be asked anything")
    if env("AGENT_CONSULT", default="on").lower() in ("off", "0", "false", "no"):
        return "asking the planner is switched off on this service (AGENT_CONSULT=off)"
    held = str(script or "")
    body = asked + (f"\n\nThe script as it stands:\n\n{held}" if held.strip() else "")
    messages = [
        {"role": "system", "content":
            "You are the planner of a two-model chain: another model is writing the Luau script and "
            "has asked you one question in the middle of it. Answer that question, in prose, "
            "briefly and concretely -- the writer needs the decision and the reason, not code, and "
            "not a review of anything it did not ask about."},
        {"role": "user", "content": body},
    ]
    box = {"finish": None, "usage": None, "tool_calls": [], "raw": "", "meta": ""}
    pieces: list = []
    if DEEPSEEK.web is not None:
        for piece in DEEPSEEK.web.stream(as_prompt(messages), box, deepseek_chat(session)):
            pieces.append(piece)
    else:
        for piece in stream_call(messages, DEEPSEEK_TEMPERATURE, DEEPSEEK, DEEPSEEK_TOKENS, box):
            pieces.append(piece)
    answer = strip_metadata("".join(pieces)).strip()
    if not answer:
        return f"{DEEPSEEK.model} answered with nothing"
    return _text(f"{DEEPSEEK.model} says:", answer)


# --- the dispatch table ----------------------------------------------------------------------

def _text(title: str, body: str) -> str:
    return f"{title}\n{body}" if body else title


# --- looking at one part of a script, and at a page --------------------------------------

# Lua's character classes, in Python's spelling. Only the ones a writer types, and `%X` with an
# unknown letter stays literal: a pattern the model got wrong has to come back as a message about
# the pattern, not as a search that quietly matches the wrong thing.
LUA_CLASS = {"a": "[A-Za-z]", "c": "[\\x00-\\x1f\\x7f]", "d": "\\d", "l": "[a-z]",
             "p": "[^\\w\\s]", "s": "\\s", "u": "[A-Z]", "w": "[A-Za-z0-9_]",
             "x": "[A-Fa-f0-9]", "z": "\\x00"}


def lua_pattern(pattern: str) -> str:
    """A Lua pattern, close enough to search with: the classes, and the magic that is the same.

    `-` is Lua's lazy repetition and becomes Python's `*?`; everything Lua treats as literal is
    escaped. It is not the whole language -- there is no `%b` nor `%f` -- and it says so by simply
    not matching, which is the same answer as a pattern that finds nothing.
    """
    out, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if char == "%" and index + 1 < len(pattern):
            nxt = pattern[index + 1]
            out.append(LUA_CLASS.get(nxt.lower(), re.escape(nxt)))
            index += 2
            continue
        if char == "-":
            out.append("*?")
        elif char in ".[]*+?^$()|":
            out.append(char)
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def find_lines(script: str, pattern: str, context: int = 0, limit: int = 80) -> str:
    """The matching lines of a script, numbered, with a little context each. "" means none."""
    if not (pattern or "").strip():
        return "no pattern was given"
    try:
        rx = re.compile(lua_pattern(pattern))
    except re.error as e:
        return f"that is not a pattern that can be searched with: {e}"
    lines = (script or "").splitlines()
    hits = [index for index, line in enumerate(lines) if rx.search(line)]
    if not hits:
        return f"no line of the script matches {pattern!r}"
    out, shown = [], 0
    for index in hits[:limit]:
        out.append(f"{index + 1}: {lines[index].strip()[:200]}")
        for offset in range(1, max(0, context) + 1):
            if index + offset < len(lines):
                out.append(f"{index + offset + 1}:   {lines[index + offset].strip()[:200]}")
        shown += 1
    head = f"{len(hits)} line(s) match {pattern!r}"
    if len(hits) > shown:
        head += f" (showing the first {shown})"
    return _text(head, "\n".join(out))


# What a fetched page is worth keeping of. Bigger pages exist; nothing here needs all of one.
WEB_MAX_CHARS = int(env("WEB_MAX_CHARS", default="8000"))
WEB_TIMEOUT = float(env("WEB_TIMEOUT", default="20"))
WEB_AGENT = "bahs-agent/1.0 (+luau writer; reads public documentation pages)"
# Where the page's own machinery sits between the reader and the words: cut it, then everything
# else that is a tag, then turn the entities back into the characters they stand for.
PAGE_NOISE = re.compile(r"<(script|style|nav|svg|noscript)\b.*?</\1>", re.S | re.I)
PAGE_BREAK = re.compile(r"</(p|div|li|h[1-6]|tr|section|article|pre|code)>|<br\s*/?>", re.I)
PAGE_TAG = re.compile(r"<[^>]+>")


def page_text(raw: str) -> str:
    """A page as readable text: no scripts, no styles, no tags, no run of blank lines."""
    body = PAGE_NOISE.sub(" ", raw or "")
    body = PAGE_BREAK.sub("\n", body)
    body = html_mod.unescape(PAGE_TAG.sub(" ", body))
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_text(url: str, cap: int = 0, timeout: float = 0) -> str:
    """Fetch a URL and give back what it says. Raises nothing: a failure is the answer."""
    limit = max(500, min(int(cap or WEB_MAX_CHARS), 60000))
    with httpx.Client(timeout=timeout or WEB_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": WEB_AGENT, "Accept": "text/html,text/plain,*/*"}) as c:
        r = c.get(url)
        if r.status_code >= 400:
            return f"{url} answered HTTP {r.status_code} {r.reason_phrase}"
        raw = r.text or ""
        ctype = r.headers.get("content-type", "").lower()
    body = page_text(raw) if "html" in ctype else raw
    body = body.strip()
    if not body:
        return f"{url} answered with nothing readable"
    cut = "\n[... the page was longer than the cap; ask for another part of it ...]"
    return body if len(body) <= limit else body[:limit] + cut


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
        if name == "luau_find":
            script = str(arguments.get("script") or "")
            pattern = str(arguments.get("pattern") or "")
            answer = find_lines(script, pattern, int(arguments.get("context") or 0))
            return {"ok": not answer.startswith(("no pattern", "that is not a pattern")),
                    "summary": answer.splitlines()[0][:200], "output": answer}
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
        if name == "web_get":
            url = str(arguments.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return {"ok": False, "summary": "not a URL",
                        "output": "the url has to start with http:// or https://"}
            if env("AGENT_WEB", default="on").lower() in ("off", "0", "false", "no"):
                return {"ok": False, "summary": "reading pages is switched off",
                        "output": "AGENT_WEB=off on this service, so nothing can be fetched"}
            body = fetch_text(url, int(arguments.get("max_chars") or 0))
            ok = not body.startswith((f"{url} answered HTTP", f"{url} answered with nothing"))
            return {"ok": ok, "summary": f"{len(body)} chars from {url}" if ok else body[:200],
                    "output": _text(f"GET {url}", body)}
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
        if name == "luau_diff":
            a, b = str(arguments.get("a") or ""), str(arguments.get("b") or "")
            if not a.strip() and not b.strip():
                return {"ok": False, "summary": "nothing to diff",
                        "output": "send the two scripts in `a` and `b`"}
            answer = unified_diff(a, b, str(arguments.get("from") or "before"),
                                  str(arguments.get("to") or "after"))
            return {"ok": True, "summary": answer.splitlines()[0][:200], "output": answer}
        if name == "plan_todo":
            answer = plan_todo(str(arguments.get("action") or ""), str(arguments.get("text") or ""),
                               int(arguments.get("index") or 0), session)
            return {"ok": True, "summary": answer.splitlines()[0][:200], "output": answer}
        if name == "script_versions":
            answer = script_versions(str(arguments.get("action") or ""),
                                     str(arguments.get("name") or ""),
                                     str(arguments.get("script") or ""), session)
            return {"ok": True, "summary": answer.splitlines()[0][:200], "output": answer}
        if name == "consult_planner":
            question = str(arguments.get("question") or "")
            if not question.strip():
                return {"ok": False, "summary": "nothing was asked",
                        "output": "send the question in `question`"}
            answer = consult_planner(question, str(arguments.get("script") or ""), session)
            return {"ok": True, "summary": f"{len(answer)} chars from {DEEPSEEK.model}",
                    "output": answer}
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
        # The second model is only ever asked on purpose, by the writer's own tool call.
        "consult": env("AGENT_CONSULT", default="on"),
        "web": env("AGENT_WEB", default="on"),
        "dump_ok": dump["ok"],
        "dump_classes": dump["classes"],
        "dump_source": dump["source"],
        "dump_error": dump["error"],
        "executor_ok": executor["ok"],
        "executor_clients": executor["clients"],
        "executor_waiting": executor["waiting"],
    }
