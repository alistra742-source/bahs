"""Check the three modes against a stubbed pair of models, and the toolbox against itself.

Run from the project root:  .venv/bin/python verify_chain.py

What is being checked, in order: one call to one model with thinking on; the tool round trip
(the model asks, the tool runs, the model is asked again with the result in front of it); the
answer having no tool XML and no continuation metadata in it; the session keeping *one* chat (the
marker the provider needs is remembered and put back on the next turn of the same session and on
no other); the executor round trip behind `run_script`; and the toolbox's own units.

And the three modes on top of that: agent mode calling DeepSeek exactly once and Qwen exactly
once (no negotiation, no review of the plan, no third model), the plan streaming on its own
channel and never into the answer, DeepSeek answering on its own in deepseek mode with no tools
attached, and a mode whose credential is missing being refused by name rather than served by the
other model.

Last, the Roblox client (ghaith.lua), read as the other half of the tool protocol: its own tool
table, that every tool in it reads the game rather than the player's machine, that neither the
SCRIPT pane nor "copy code" can end up holding a paragraph of the model's notes, and that a
console error becomes a turn of its own for the script that printed it.

And the ceiling on silence: a provider that opens a stream and stops sending ends the turn with
the silence named in seconds, instead of a turn nobody is ever told about.

No keys and no network: both models are one local stub, and the Roblox API dump is a fixture.
"""
import contextlib, html, io, json, os, re, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The hidden continuation marker qwen-api puts in an answer, and the one thing that keeps one
# upstream chat: named here because a section that turns it off has to turn it back on.
STUB_META = '<!-- qwen_metadata: {"response_id":"r1"} -->'

CALLS = []
STUB = {"mode": "plain", "tool": "luau_check", "arguments": None, "openai_tool": False,
        "finish": "stop", "script": "", "finite": 0, "prose": "Let me check that first.\n",
        "plan": "", "decoy": "", "hold": 4.0,
        "meta": STUB_META}

DEEPSEEK_MODEL = "deepseek-v4-flash"

# What the planner answers in agent mode. Prose, so it can be told apart from a script, and
# specific enough that finding any of it in the answer would be obvious.
PLAN = """1. Apply the speed from one RenderStepped connection, not once at spawn.
2. Services: Players. Members: Players.LocalPlayer, Player.Character, Humanoid.WalkSpeed.
3. Structure: one connection at the top level, and disconnect it when the character dies.
4. Traps: the character is nil on the first frame, and wait() is not task.wait()."""

# A marker the *second* model should never be able to install or clear: it is not the Qwen
# metadata, and a session's Qwen chat must survive a call to another model in the same session.
DECOY = '<!-- qwen_metadata: {"response_id":"from-the-planner"} -->'

SIMPLE_SCRIPT = """local Players = game:GetService("Players")
local SPEED = 16
local function apply(character)
    local humanoid = character:WaitForChild("Humanoid", 10)
    if humanoid then
        humanoid.WalkSpeed = SPEED
    end
end
Players.PlayerAdded:Connect(function(plr)
    if plr.Character then apply(plr.Character) end
    plr.CharacterAdded:Connect(apply)
end)"""

BROKEN_SCRIPT = SIMPLE_SCRIPT + "\nif true then\nprint(\"never closed\")\n"
FINAL = "```lua\n" + SIMPLE_SCRIPT + "\n```"
REASONING = "SECRET_REASONING_TEXT"

# An answer that is about the script rather than the script: the failure the last
# "deepseek-web answered" turn had -- prose full of the tools it meant to call, which used to ship
# as if a turn had produced a script. The tool tokens are the ones its own client reads.
PROSE_ANSWER = """Let me try @@SOURCE@@ with a full path like game.ReplicatedStorage.Weapons
the tool says "path". Let me try @@GREP@@ FireServer
@@GREP@@ RemoteEvent
@@TREE@@ ReplicatedStorage.WeaponsFolder
@@SOURCE@@ game.ReplicatedStorage.WeaponsFolder.ThompsonScript
@@PROPS@@ game.Workspace.Baseplate"""

# Prose with no calls in it at all: no script, and nothing for the client to run either, so it is
# the one shape that has to be asked again and then refused.
PLAIN_PROSE = """Let me work out how this should be built before writing anything.
The player needs a faster walk speed, and it has to survive their character respawning.
The connection should be made once and cleaned up when the character dies.
I will use the humanoid, checked every frame, and nothing else."""

# A list of calls for the Roblox client -- the tokens it reads out of an answer and runs. The
# service cannot run these (there is no Roblox in the image), so an answer that is only these has
# to ship for the client to act on rather than being refused as prose.
CLIENT_CALLS = "@@DEEPSCAN@@\n@@GREP@@ remote\n@@SOURCE@@ game.ReplicatedStorage.Weapons"

# One script the model split across two fenced blocks ("part one", "part two"): both halves read
# as Lua on their own, and there is nothing but the blocks -- which is the shape that has to be put
# back together rather than half of it shipped.
PART_ONE = """local Players = game:GetService("Players")
local SPEED = 16
local function apply(character)
    local humanoid = character:WaitForChild("Humanoid", 10)"""
PART_TWO = """if humanoid then
    humanoid.WalkSpeed = SPEED
end
end
Players.PlayerAdded:Connect(apply)"""


# --- the stub provider --------------------------------------------------------------------

def sse(payload) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def delta_frame(delta, finish=None) -> bytes:
    return sse({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


def answer_stream(text, finish="stop", reasoning=REASONING) -> bytes:
    """One answer, with the thinking that precedes it on the same stream."""
    out = b""
    if reasoning:
        out += delta_frame({"reasoning_content": reasoning})
    out += delta_frame({"content": text}) + delta_frame({}, finish) + b"data: [DONE]\n\n"
    return out


def tool_stream(call_id="call_1", name="", arguments="", openai=False, xml="") -> bytes:
    """One answer that asks for a tool, in whichever shape the proxy uses."""
    out = delta_frame({"reasoning_content": REASONING})
    if openai and not xml:
        # OpenAI streams a tool call in fragments: the name first, then pieces of the arguments.
        out += delta_frame({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                            "function": {"name": name}}]})
        for piece in [arguments[i:i + 12] for i in range(0, len(arguments), 12)]:
            out += delta_frame({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})
        out += delta_frame({}, "tool_calls")
    else:
        out += delta_frame({"content": xml}) + delta_frame({}, "tool_calls")
    return out + b"data: [DONE]\n\n"


def requests_body(body):
    """The last user turn, and whether a tool result is already in the conversation."""
    asked = ""
    for message in body.get("messages") or []:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            asked = message["content"]
    has_tool = any(m.get("role") == "tool" for m in body.get("messages") or [])
    return asked, has_tool


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload, ctype="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _hold(self, first: bytes, seconds: float):
        """A stream that opens, sends one frame, and then goes quiet.

        No Content-Length and no end: the body runs until the connection closes, and the
        connection is held open for `seconds`. Nothing about this is an error, a close or an
        EOF -- which is the point: it is what a provider that dies mid-answer looks like from
        the other side, and without a read bound it is indistinguishable from a model thinking.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(first)
        self.wfile.flush()
        time.sleep(seconds)
        self.close_connection = True

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "qwen3.8-max"},
                                                               {"id": DEEPSEEK_MODEL},
                                                               {"id": "qwen3.7-plus"}]})
        return self._send(404, {"error": {"message": "no such route"}})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw) if raw else {}
        if self.path.endswith("/validate"):
            return self._send(200, {"valid": True})
        CALLS.append({"path": self.path, "body": body})
        if STUB["mode"] == "stall":
            # One frame, then silence, with the connection still open.
            return self._hold(delta_frame({"content": "local part = "}), STUB["hold"])
        if str(body.get("model") or "").startswith("deepseek") and STUB["plan"]:
            # The second model, in the one place it is used as a planner (agent mode) -- in
            # deepseek mode it is the writer, so no plan is configured and this call falls through
            # to the script below. It is never offered a tool and never asks for one, and the decoy
            # marker is deliberate: nothing about an answer from this model may install or clear
            # the Qwen continuation the session is holding.
            text = STUB["plan"] + (("\n" + STUB["decoy"]) if STUB["decoy"] else "")
            return self._send(200, answer_stream(text, STUB["finish"]), "text/event-stream")
        asked, has_tool = requests_body(body)
        args = json.dumps(STUB["arguments"] or {})
        call = ("<tool_calls>" + json.dumps([{"name": STUB["tool"],
                                            "arguments": STUB["arguments"] or {}}])
                + "</tool_calls>")
        if STUB["mode"] == "always_tool":
            # The model that will not stop asking: the round limit is what ends it.
            return self._send(200, tool_stream(xml=STUB["prose"] + call), "text/event-stream")
        # A conversation that already carries a tool result is the follow-up: the model has been
        # given the result and answers. Otherwise the mode decides what this call asks for.
        if STUB["mode"] == "tool" and not has_tool:
            if STUB["openai_tool"]:
                return self._send(200, tool_stream(name=STUB["tool"], arguments=args, openai=True),
                                  "text/event-stream")
            # Prose around the call, which must not end up in the answer either.
            return self._send(200, tool_stream(xml=STUB["prose"] + call),
                              "text/event-stream")
        if STUB["mode"] == "client_tools":
            # The Roblox client's own protocol: the answer is a list of calls for it to run, not a
            # script -- which is a turn doing its work, not a turn that failed to write one.
            return self._send(200, answer_stream(CLIENT_CALLS, STUB["finish"]), "text/event-stream")
        if STUB["mode"] == "always_prose":
            # A model that never reaches the script, however many times it is asked.
            return self._send(200, answer_stream(STUB["prose"], STUB["finish"]),
                              "text/event-stream")
        if STUB["mode"] == "prose_then_script":
            # Prose first, and the script only once it has been told what was wrong with that: the
            # correction is the newest user turn, so this is what "asking again" looks like.
            if "not a script" in asked:
                return self._send(200, answer_stream(FINAL, STUB["finish"]), "text/event-stream")
            return self._send(200, answer_stream(STUB["prose"], STUB["finish"]),
                              "text/event-stream")
        text = (STUB["script"] or FINAL) + (("\n" + STUB["meta"]) if STUB["meta"] else "")
        return self._send(200, answer_stream(text, STUB["finish"]), "text/event-stream")


class StubServer(ThreadingHTTPServer):
    # Without this a handler thread sits in readline() on a pooled keep-alive connection and the
    # process never exits, which looks exactly like a hung test.
    daemon_threads = True
    allow_reuse_address = True


PORT = 8143
stub = StubServer(("127.0.0.1", PORT), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()

# The API dump the tool reads, written outside the repo so a test run leaves nothing behind.
DUMP_FIXTURE = Path(tempfile.gettempdir()) / "bahs-api-dump.json"
DUMP_FIXTURE.write_text(json.dumps({"Classes": [
    {"Name": "Instance", "Superclass": "<<<ROOT>>>", "Members": [
        {"MemberType": "Property", "Name": "ClassName",
         "ValueType": {"Category": "Primitive", "Name": "string"}, "Tags": ["ReadOnly"]},
        {"MemberType": "Function", "Name": "FindFirstChild",
         "Parameters": [{"Name": "name", "Type": {"Category": "Primitive", "Name": "string"}}],
         "ReturnType": {"Category": "Class", "Name": "Instance"}, "Tags": []},
        {"MemberType": "Event", "Name": "Changed",
         "Parameters": [{"Name": "property", "Type": {"Category": "Primitive",
                                                      "Name": "string"}}]},
    ]},
    {"Name": "RemoteEvent", "Superclass": "Instance", "Members": [
        {"MemberType": "Function", "Name": "FireServer",
         "Parameters": [{"Name": "arguments", "Type": {"Category": "Group", "Name": "Tuple"}}],
         "ReturnType": {"Category": "Primitive", "Name": "null"}, "Tags": []},
        {"MemberType": "Function", "Name": "FireClient",
         "Parameters": [{"Name": "player", "Type": {"Category": "Class", "Name": "Player"}},
                        {"Name": "arguments", "Type": {"Category": "Group", "Name": "Tuple"}}],
         "ReturnType": {"Category": "Primitive", "Name": "null"}, "Tags": []},
    ]},
]}, indent=0), encoding="utf-8")

os.environ.update({
    "QWEN_URL": f"http://127.0.0.1:{PORT}/v1",
    "QWEN_TOKEN": "qwen-test-token",
    # The second model, on the same stub: an explicit URL, so the credential is not treated as a
    # chat.deepseek.com site token and the OpenAI-shaped transport is the one exercised.
    "DEEPSEEK_URL": f"http://127.0.0.1:{PORT}/v1",
    "DEEPSEEK_TOKEN": "deepseek-test-token",
    "DEEPSEEK_MODEL": DEEPSEEK_MODEL,
    "ROBLOX_API_DUMP": str(DUMP_FIXTURE),
    "HEARTBEAT": "0.2",
    # /health remembers a provider's answer for a minute so the page's polling does not turn into
    # a network call every 8 seconds; the test wants every check to be a fresh one.
    "TOKEN_CHECK_TTL": "0",
    "AGENT_RUN": "on",
    # A tool that waits for an executor is bounded by this; the fake executor answers in well
    # under a second, so a short one keeps the test quick.
    "RUN_TIMEOUT": "10",
    "EXECUTOR_IDLE": "30",
})
for name in ("QWEN_THINKING", "API_KEY", "AGENT_ROUNDS", "AGENT_TOOLS", "CHAIN_MODE",
             "DEEPSEEK_THINKING", "DEEPSEEK_SHAPE"):
    os.environ.pop(name, None)

# How the test starts, so a section that sets a variable cannot leak it into the next one: every
# reload begins from this, not from whatever the section before it happened to leave behind.
BASE_ENV = {name: os.environ.get(name) for name in (
    "QWEN_URL", "QWEN_TOKEN", "QWEN_THINKING", "AGENT_ROUNDS", "AGENT_TOOLS", "AGENT_RUN",
    "API_KEY", "ROBLOX_API_DUMP", "HEARTBEAT", "TOKEN_CHECK_TTL", "SESSION_TTL",
    "CHAIN_MODE", "DEEPSEEK_URL", "DEEPSEEK_TOKEN", "DEEPSEEK_MODEL", "DEEPSEEK_THINKING",
    "CHAT_IDLE",
    "DEEPSEEK_SHAPE", "AGENT_WEB", "SCRIPT_RETRIES", "CHAT_TIMEOUT", "CHAT_IDLE")}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402
import bridge  # noqa: E402
import luau  # noqa: E402
import state  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)
failures, count = [], [0]
UNSET = object()


def check(name, got, want):
    count[0] += 1
    if got != want:
        failures.append(name)
        print(f"  FAIL {name}: got {got!r}, wanted {want!r}")
    else:
        print(f"  ok   {name}")


def check_true(name, got):
    check(name, bool(got), True)


def reload_with(**env):
    """Reload the modules with these variables set (UNSET removes one)."""
    global server, client
    for name, value in BASE_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    for name, value in env.items():
        if value is UNSET:
            os.environ.pop(name, None)
        elif value is not None:
            os.environ[name] = value
    # The config and the provider live in bridge, the limits in state, the toolbox in luau, and
    # server binds all of their names at import -- so every one of them is reloaded, lowest first.
    importlib.reload(bridge)
    importlib.reload(luau)
    importlib.reload(state)
    server = importlib.reload(server)
    client = TestClient(server.app)


def fresh_toolbox():
    """Forget which executor clients were seen, so one test cannot look like another's."""
    luau._clients.clear()
    luau._queue.clear()
    luau._runs.clear()


def turn(question="make me a walk script", session="s-test", mode="plain", tool="luau_check",
         arguments=None, openai_tool=False, script=None, meta=None, finish="stop",
         messages=None, prose=":ASK:", asked_mode=None, plan=None, decoy="", thinking=""):
    """One turn, watched to the end, with the model stub configured for it.

    `mode` is what the stub does; `asked_mode` is the mode the request asks for, which is empty
    (the service's default) unless a test is about a specific chain; `thinking` is the writer's
    setting, sent only when a test is about it so everything else keeps the default.
    """
    CALLS.clear()
    STUB.update({"mode": mode, "tool": tool, "arguments": arguments, "openai_tool": openai_tool,
                 "script": script, "finish": finish, "plan": PLAN if plan is None else plan,
                 "decoy": decoy,
                 "prose": ("Let me check that first.\n" if prose == ":ASK:" else prose),
                 "meta": STUB["meta"] if meta is None else meta})
    body = {"messages": messages or [{"role": "user", "content": question}], "session": session}
    if asked_mode:
        body["mode"] = asked_mode
    if thinking:
        body["thinking"] = thinking
    started = client.post("/chat/stream", json=body)
    assert started.status_code == 200, started.text
    frames = []
    with client.stream("GET", f"/chat/stream/{started.json()['job']}") as r:
        for line in r.iter_lines():
            if line.strip():
                frames.append(json.loads(line))
    done = [f for f in frames if f.get("done")]
    return frames, (done[0] if done else {}), started.json(), CALLS[:]


def writer_calls(calls):
    return [c for c in calls if c["body"].get("model") == "qwen3.8-max"]


def planner_calls(calls):
    return [c for c in calls if str(c["body"].get("model") or "").startswith("deepseek")]


def channel(frames, name):
    """What a reader ends up seeing on one channel, after every reset it was sent.

    A reset is what the server sends when it throws a channel's text away and replaces it -- as
    the writer's answer is replaced once its fences are stripped, and as the plan is when the
    hidden marker after it is cut. A reader clears the bubble at that point, so this does too.
    """
    out: list = []
    for f in frames:
        if f.get("ch") != name:
            continue
        if f.get("reset"):
            out = []
        elif f.get("t"):
            out.append(f["t"])
    return "".join(out)


def system_of(call):
    for m in messages_of(call):
        if m.get("role") == "system":
            return m.get("content") or ""
    return ""


def messages_of(call):
    return call["body"].get("messages") or []


def tool_messages(call):
    return [m for m in messages_of(call) if m.get("role") == "tool"]


# --- the toolbox on its own --------------------------------------------------------------

def toolbox_checks():
    print("\nthe toolbox")
    clean = luau.check(SIMPLE_SCRIPT)
    check("a real script has no structural error", clean["errors"], [])
    check_true("and it is reported clean", clean["ok"])
    broken = luau.check(BROKEN_SCRIPT)
    check_true("an unclosed if is an error", any("never closed" in e for e in broken["errors"]))
    check_true("an extra end is an error",
               any("never opened" in e for e in luau.check("print(1)\nend\n")["errors"]))
    check_true("an unterminated string is an error",
               any("never closed" in e for e in luau.check('local s = "oops\n')["errors"]))
    check_true("an unbalanced bracket is an error",
               any("never closed" in e for e in luau.check("local t = {\n1,\n")["errors"]))
    check("a comment is not code", luau.check("-- end \" (\nprint('ok')\n")["errors"], [])
    check("a long string is not code",
          luau.check("local s = [[\nend end end\n]]\n")["errors"], [])
    check_true("wait() is flagged",
               any("wait()" in w for w in luau.check("wait(1)\n")["warnings"]))
    check_true("a Studio-only service is flagged",
               any("Studio-only" in w for w in
                   luau.check('game:GetService("ChangeHistoryService")\n')["warnings"]))
    check_true("a server-only service is flagged",
               any("server-only" in w for w in
                   luau.check('game:GetService("ServerStorage")\n')["warnings"]))
    check("a balanced loop is not an error",
          luau.check("for i = 1, 3 do\nif i == 2 then continue end\nprint(i)\nend\n")["errors"],
          [])
    formatted = luau.format_script("local function f(a)\nif a then\nprint(\"x\")\n"
                                   "local t = {\n1,\n}\nreturn t\nelse\nreturn nil\nend\nend\n")
    check("format_script indents by block depth", formatted.splitlines()[1], "    if a then")
    check("format_script lines up the else", formatted.splitlines()[7], "    else")
    check("format_script closes at column zero", formatted.splitlines()[-1], "end")
    edit = luau.run("apply_edit", {"script": SIMPLE_SCRIPT, "find": "SPEED = 16",
                                   "replace": "SPEED = 100"})
    check_true("apply_edit replaces one occurrence", edit["ok"] and "SPEED = 100" in edit["output"])
    check("apply_edit refuses a find that is not there",
          luau.run("apply_edit", {"script": SIMPLE_SCRIPT, "find": "nope"})["ok"], False)
    check("apply_edit refuses an ambiguous find",
          luau.run("apply_edit", {"script": "a\na\n", "find": "a"})["ok"], False)
    scan = luau.run("secret_scan", {
        "script": 'local hook = "https://discord.com/api/webhooks/123456789/abcdefghijkl"\n'})
    check_true("secret_scan finds a webhook and does not echo it",
               "1 credential" in scan["summary"] and "discord.com" in scan["output"])
    api = luau.run("roblox_api", {"class_name": "RemoteEvent"})
    check_true("roblox_api lists the real members", "FireServer" in api["output"]
               and "FireClient(player: Player, arguments: tuple)" in api["output"])
    search = luau.run("roblox_api", {"query": "FindFirstChild"})
    check_true("roblox_api searches members", "Instance:FindFirstChild" in search["output"])
    check("roblox_api says when a class does not exist",
          "is not a Roblox class" in luau.run("roblox_api", {"class_name": "Nonsense"})["output"],
          True)
    check_true("there is a tool for each name", all(callable(luau.run) for _ in luau.TOOL_NAMES))
    check("an unknown tool is a result, not a crash",
          luau.run("nope", {})["output"], "there is no tool 'nope'")

    print("\nthe tools that look outside the script")
    found = luau.run("luau_find", {"script": SIMPLE_SCRIPT, "pattern": "Humanoid", "context": 1})
    check("luau_find counts the lines that match", found["summary"], "1 line(s) match 'Humanoid'")
    check_true("and numbers them", found["output"].splitlines()[1].startswith("4: "))
    check_true("with the lines under them when asked",
               found["output"].splitlines()[2].startswith("5: "))
    check_true("a Lua class is a class", luau.find_lines(SIMPLE_SCRIPT, "%d+").splitlines()[1]
               .startswith("2: "))
    check("and a Lua literal is a literal", luau.lua_pattern("%.WalkSpeed"), r"\.WalkSpeed")
    check_true("no match is not an error",
               luau.find_lines(SIMPLE_SCRIPT, "Nonsense").startswith("no line"))
    check_true("nor is an empty pattern",
               luau.run("luau_find", {"script": SIMPLE_SCRIPT})["ok"] is False)
    check("a fetched page is reduced to its words",
          luau.page_text("<html><head><style>a{}</style></head><body><p>Hello <b>there</b></p>"
                         "<script>var x=1</script><p>Second</p></body></html>"),
          "Hello there\nSecond")
    check("web_get wants a URL", luau.run("web_get", {"url": "example.com"})["ok"], False)
    check_true("and says what one looks like",
               "http://" in luau.run("web_get", {"url": "example.com"})["output"])
    reload_with(AGENT_WEB="off")
    check("reading pages can be switched off",
          luau.run("web_get", {"url": "https://example.com"})["ok"], False)
    reload_with()


# --- is the answer a script? --------------------------------------------------------------

def script_checks():
    print("\nis the answer a script?")
    reload_with()
    check("a script is a script", bridge.script_verdict(SIMPLE_SCRIPT)[0], True)
    check("and nothing is held against it", bridge.script_verdict(SIMPLE_SCRIPT)[1], "")
    check("a fenced script still is one", bridge.script_verdict(FINAL)[0], True)
    check("one line of Lua is a script", bridge.script_verdict('print("hi")')[0], True)
    check("an empty answer is not", bridge.script_verdict("   \n\n")[0], False)
    check("and it says which kind of nothing",
          bridge.script_verdict("  ")[1], "the answer carried no script at all")
    check("prose about a script is not a script", bridge.script_verdict(PROSE_ANSWER)[0], False)
    check_true("and it says what was wrong with it",
               "reads as Lua" in bridge.script_verdict(PROSE_ANSWER)[1])
    check("its own words are not Lua", bridge.script_verdict(PLAIN_PROSE)[0], False)
    check("and a paragraph of English says so plainly",
          bridge.script_verdict(PLAIN_PROSE)[1], "no line of the answer reads as Lua")
    check("an if/end pair with no calls in it is still a script",
          bridge.script_verdict("if a then\nprint(1)\nend")[0], True)
    check_true("the rewrite asks for the whole script",
               "ONLY the complete Luau script" in bridge.prose_correction("it was prose"))

    print("\na turn that answers with prose")
    frames, done, started, calls = turn(mode="prose_then_script", prose=PLAIN_PROSE,
                                        session="s-prose")
    said = writer_calls(calls)
    check("the model is asked again", len(said), 2)
    check("the answer is the script in the end", done.get("text"), SIMPLE_SCRIPT)
    check("and the turn is done", done.get("status"), "done")
    check("the prose was not thrown away", len([m for m in messages_of(said[1])
                                                 if m.get("role") == "assistant"]), 1)
    correction = [m for m in messages_of(said[1]) if m.get("role") == "user"][-1]["content"]
    check_true("the writer is told what was wrong", "not a script" in correction)
    check_true("with the reason in it", "reads as Lua" in correction)
    check("the prose never reached the reader as the answer",
          channel(frames, "answer"), SIMPLE_SCRIPT)

    print("\na turn that never writes a script")
    frames, done, started, calls = turn(mode="always_prose", prose=PLAIN_PROSE,
                                        session="s-prose-stop")
    errors = [f["error"] for f in frames if f.get("error")]
    check("it is asked SCRIPT_RETRIES more times and no more", len(writer_calls(calls)),
          1 + bridge.SCRIPT_RETRIES)
    check_true("and the turn fails instead of reporting an answer", bool(errors))
    check_true("the failure says what the answer was", "instead of a script" in errors[0])
    check_true("and that it was asked again", "asked again" in errors[0])
    check("nothing was published as a finished answer", done, {})

    print("\nthe client's own tool calls")
    frames, done, started, calls = turn(mode="client_tools", session="s-client-tools")
    check("a list of calls for the client is not prose", bridge.tool_request(CLIENT_CALLS), True)
    check("and it is not a script either", bridge.script_verdict(CLIENT_CALLS)[0], False)
    check("the turn ships it instead of asking again", len(writer_calls(calls)), 1)
    check("the calls reach the reader", done.get("text"), CLIENT_CALLS)
    check("and the turn is done", done.get("status"), "done")
    check_true("with the header saying what is happening",
               "client's tools" in (done.get("note") or ""))
    check("prose with a call in it is a call, not an answer",
          bridge.tool_request(PROSE_ANSWER), True)
    check("and plain prose is nobody's tool request", bridge.tool_request(PLAIN_PROSE), False)
    check("and a plain script is nobody's tool request", bridge.tool_request(SIMPLE_SCRIPT), False)

    print("\nfast, or thinking first")
    frames, done, started, calls = turn(session="s-fast", thinking="fast", meta=STUB_META)
    check("fast is asked for by name", calls[0]["body"].get("thinking_mode"), "fast")
    check("the start response reports it", started.get("thinking"), "fast")
    check("the turn reports it too", done.get("thinking"), "fast")
    check("and the answer is still the script", done.get("text"), SIMPLE_SCRIPT)
    check("a fast turn still stores the session's marker", bridge.session_meta("s-fast"),
          STUB["meta"])
    # The same chat, continued. A fast turn is the same session, so the marker has to ride on the
    # next turn's messages: a writer outside QWEN_PROVIDERS would quietly open a new upstream chat
    # on every fast turn instead.
    history = [{"role": "user", "content": "make me a walk script"},
               {"role": "assistant", "content": SIMPLE_SCRIPT},
               {"role": "user", "content": "now make it 100"}]
    _, _, _, again = turn(session="s-fast", thinking="fast", messages=history)
    carried = [m for m in messages_of(writer_calls(again)[0])
               if m.get("role") == "assistant" and "qwen_metadata" in (m.get("content") or "")]
    check("and the next fast turn continues that chat", len(carried), 1)
    _, _, _, calls = turn(session="s-think", thinking="thinking")
    check("thinking is asked for by name too", calls[0]["body"].get("thinking_mode"), "thinking")
    _, _, _, calls = turn(session="s-default")
    check("a turn that says nothing gets the service's default",
          calls[0]["body"].get("thinking_mode"), bridge.QWEN_THINKING)
    check("a word this service does not know means thinking, not fast",
          bridge.writer_thinking("turbo"), "thinking")
    check("and an empty one is the default", bridge.writer_thinking(""),
          "fast" if bridge.QWEN_THINKING in bridge.FAST_WORDS else "thinking")


# --- the chain ---------------------------------------------------------------------------

def chain_checks():
    print("\nplain answer")
    frames, done, started, calls = turn()
    said = writer_calls(calls)
    check("one model call for a plain answer", len(said), 1)
    check("the model is pinned", said[0]["body"]["model"], "qwen3.8-max")
    check("thinking is on", said[0]["body"].get("thinking_mode"), "thinking")
    check("the tool schemas are attached", len(said[0]["body"].get("tools") or []),
          len(luau.TOOL_NAMES))
    check("the answer is the script, unwrapped", done.get("text"), SIMPLE_SCRIPT)
    check("the answer carries no metadata", "qwen_metadata" in (done.get("text") or ""), False)
    # The writer's chain of thought is the client's thinking pane, not the answer: it arrives on a
    # channel of its own -- a fragment mixed into `answer` would land inside the script -- and the
    # answer is the only channel that reaches `text`.
    check("the reasoning is never in the answer channel",
          REASONING in channel(frames, "answer"), False)
    check("nor in the finished text", REASONING in (done.get("text") or ""), False)
    check("the thinking arrives on a channel of its own", channel(frames, "thoughts"), REASONING)
    check("the finished turn hands it back", done.get("thoughts"), REASONING)
    check("and reports how much of it there was", done["phases"][0]["thought_chars"],
          len(REASONING))
    check("the poll reports it too",
          client.get(f"/chat/poll/{started['job']}").json().get("thoughts"), REASONING)
    check("the session is reported", started.get("session"), "s-test")
    check("no tool call is recorded", done["phases"][0]["tools"], [])
    check("the turn is a call and a phase", len(done["phases"]), 1)

    print("\nthe tool round trip (XML shape)")
    frames, done, started, calls = turn(mode="tool", tool="luau_check",
                                        arguments={"script": BROKEN_SCRIPT})
    said = writer_calls(calls)
    check("the model is asked twice", len(said), 2)
    check("the first call found a tool", done["phases"][0]["tools"], ["luau_check"])
    check("the tool result went back to the model", len(tool_messages(said[1])), 1)
    check_true("and it is the tool's own output",
               "never closed" in tool_messages(said[1])[0]["content"])
    asked_for = [c for m in messages_of(said[1]) if m.get("role") == "assistant"
                 for c in (m.get("tool_calls") or [])]
    check_true("the tool result is matched to the call it answers",
               bool(asked_for)
               and tool_messages(said[1])[0].get("tool_call_id") == asked_for[-1].get("id"))
    check("the interim prose is not the answer", done.get("text"), SIMPLE_SCRIPT)
    check("the tool XML never reached the reader",
          any("<tool_calls>" in (f.get("t") or "") for f in frames), False)
    check("the tool's own prose is not in the answer",
          "Let me check that first." in (done.get("text") or ""), False)
    check_true("the trace says what happened", "luau_check" in (done.get("tool") or ""))
    check_true("and it carries the result", "never closed" in (done.get("tool") or ""))

    print("\nthe tool round trip (OpenAI shape, fragmented)")
    frames, done, started, calls = turn(mode="tool", tool="luau_format", openai_tool=True,
                                        arguments={"script": SIMPLE_SCRIPT})
    said = writer_calls(calls)
    check("a fragmented tool call is reassembled", len(said), 2)
    check("the tool that ran is the one asked for", done["phases"][0]["tools"], ["luau_format"])
    check_true("a re-indented script came back",
               "    local humanoid" in tool_messages(said[1])[0]["content"])

    print("\nthe same chat, not a new one per question")
    bridge.session_remember("s-chat", "")
    _, done, _, calls = turn(session="s-chat")
    check_true("the marker was kept for the session",
               "qwen_metadata" in bridge.session_meta("s-chat"))
    # The follow-up, with the conversation the page would be keeping: the answer it streamed, and
    # then the next question.
    history = [{"role": "user", "content": "make me a walk script"},
               {"role": "assistant", "content": SIMPLE_SCRIPT},
               {"role": "user", "content": "now make it 100"}]
    _, done, _, calls = turn(session="s-chat", messages=history)
    carried = [m for m in messages_of(writer_calls(calls)[0])
               if m.get("role") == "assistant" and "qwen_metadata" in (m.get("content") or "")]
    check("the next turn of the session continues the chat", len(carried), 1)
    check_true("and it is the marker that was remembered",
               bridge.session_meta("s-chat") in carried[0]["content"])
    check_true("the question is still the last turn the model sees",
               messages_of(writer_calls(calls)[0])[-1]["role"] == "user")
    check("the reader never saw the marker in the answer",
          "qwen_metadata" in (done.get("text") or ""), False)
    fresh = [{"role": "assistant", "content": SIMPLE_SCRIPT},
             {"role": "user", "content": "and another thing"}]
    _, _, _, calls = turn(session="s-other", messages=fresh)
    other = [m for m in messages_of(writer_calls(calls)[0])
             if m.get("role") == "assistant" and "qwen_metadata" in (m.get("content") or "")]
    check("another session does not get it", len(other), 0)
    check_true("the session store is what /health reports", server.session_count() >= 2)

    print("\nrunning the script in the executor")
    fresh_toolbox()
    frames, done, started, calls = turn(mode="tool", tool="run_script", openai_tool=True,
                                        arguments={"script": "print('hello')"})
    said = writer_calls(calls)
    check("the model is asked again after the refusal", len(said), 2)
    check_true("with no executor the tool says so instead of waiting",
               "no Roblox client is polling" in tool_messages(said[1])[0]["content"])
    check_true("the turn still ended with a script", bool(done.get("text")))

    fresh_toolbox()
    # A listening executor: it polls /agent/pull the way client.lua does, runs what it is given,
    # and posts the result back. It has to be polling before the tool call, because a client that
    # was never seen is a client that cannot be waited for.
    seen = {"script": "", "answered": False}
    listening = {"on": True}

    key = {"X-API-Key": "qwen-test-token"}

    def fake_executor():
        while listening["on"]:
            try:
                pulled = client.get("/agent/pull", params={"client": "test-client"},
                                   headers=key)
                task = pulled.json() if pulled.status_code == 200 else {}
                if task.get("run"):
                    seen["script"] = task["script"]
                    client.post("/agent/push", json={"run": task["run"], "ok": True,
                                                      "output": "hello from roblox"},
                                headers=key)
                    seen["answered"] = True
            except Exception:  # the server may be closing a connection under it
                pass
            time.sleep(0.1)

    executor = threading.Thread(target=fake_executor, daemon=True)
    executor.start()
    time.sleep(0.3)                     # one poll, so the client is live before the turn starts
    frames, done, started, calls = turn(mode="tool", tool="run_script", openai_tool=True,
                                        arguments={"script": "print('hello')"},
                                        session="s-exec", prose="Let me run it first.\n")
    listening["on"] = False
    check("the model's script was handed to the executor", seen["script"], "print('hello')")
    check_true("the executor answered", seen["answered"])
    check_true("the executor's output is in the tool trace",
               "hello from roblox" in (done.get("tool") or ""))
    said = writer_calls(calls)
    check("the model is asked again with the run in front of it", len(said), 2)
    check_true("and the output was given back to it",
               any("hello from roblox" in m["content"] for m in tool_messages(said[1])))
    check_true("the run is reported as clean", "ran the script without error" in
               tool_messages(said[1])[0]["content"])
    fresh_toolbox()

    print("\nthe end of the tool rounds")
    frames, done, started, calls = turn(
        mode="always_tool", tool="luau_check", arguments={"script": BROKEN_SCRIPT},
        prose="Here is the script:\n```lua\n" + SIMPLE_SCRIPT + "\n```\n")
    check("a model that will not stop still ships the script", done.get("text"), SIMPLE_SCRIPT)
    check("and it is not carrying a tool call or a fence", "<tool_calls>" in
          (done.get("text") or "") or "```" in (done.get("text") or ""), False)
    check("it did not run more rounds than it is allowed",
          len(writer_calls(calls)), bridge.AGENT_ROUNDS + 1)

    print("\nthe guard rails")
    frames, done, started, calls = turn(finish="length")
    error = [f for f in frames if f.get("error")]
    check_true("a cut-off answer is refused, not shipped", bool(error))
    # And the same refusal through the blocking endpoint.
    CALLS.clear()
    STUB.update({"mode": "plain", "finish": "length", "script": None, "meta": ""})
    blocked = client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}],
                                        "session": "s-len"},
                          headers={"X-API-Key": "qwen-test-token"})
    check("the blocking endpoint reports it too", blocked.status_code, 502)
    check_true("and says why", "token ceiling" in blocked.text)
    # Put the stub back the way it was found: a section that switches the marker off and leaves it
    # off makes every marker check after it compare nothing to nothing, and pass.
    STUB.update({"finish": "stop", "script": "", "meta": STUB_META})

    print("\nno tools, when they are switched off")
    reload_with(AGENT_TOOLS="off")
    frames, done, started, calls = turn()
    check("one call, no schemas", len(writer_calls(calls)[0]["body"].get("tools") or []), 0)
    check("and the answer still arrives", done.get("text"), SIMPLE_SCRIPT)
    reload_with()


def fence_checks():
    print("\nthe fences")
    # The prompt asks for the script with no fence around it, and a model that adds one anyway is
    # not refused: the fence comes off, because it is a syntax error where the answer is going.
    wrapped = "Here is the script:\n\n```lua\n" + SIMPLE_SCRIPT + "\n```\n\nIt should work now."
    _, done, _, _ = turn(script=wrapped)
    check("a fenced script with talk around it ships as the script", done.get("text"), SIMPLE_SCRIPT)
    check("and no fence survives into the answer", "```" in (done.get("text") or ""), False)
    _, done, _, _ = turn(script=SIMPLE_SCRIPT + "\n```\n")
    check("a stray fence line is dropped", done.get("text"), SIMPLE_SCRIPT)
    check("a one-line block is still unwrapped",
          bridge.strip_fences("```lua\nprint(1)\n```"), "print(1)")
    check("a whole answer in one block is unwrapped",
          bridge.strip_fences(FINAL), SIMPLE_SCRIPT)
    check("the largest script wins when several are fenced",
          bridge.strip_fences("```lua\nprint(1)\n```\nand\n```lua\n" + SIMPLE_SCRIPT + "\n```"),
          SIMPLE_SCRIPT)
    # The shape that used to ship half a script looking whole: the model fenced "part one" and
    # "part two" of one script, with nothing between them.
    check("a script split across two blocks is put back together",
          bridge.strip_fences("```lua\n" + PART_ONE + "\n```\n```lua\n" + PART_TWO + "\n```"),
          PART_ONE + "\n" + PART_TWO)
    check("but a block of prose is not glued onto the script",
          bridge.strip_fences("```lua\n" + SIMPLE_SCRIPT + "\n```\n```\nThat is the whole script.\n```"),
          SIMPLE_SCRIPT)
    check("prose in fences is kept by unwrap_fences",
          bridge.unwrap_fences("Use this:\n\n```lua\nA = 1\n```\n\nThat is all."),
          "Use this:\n\nA = 1\n\nThat is all.")
    check("and the answer rule says so in as many words",
          "never write ```" in bridge.ANSWER_RULE, True)


def mode_checks():
    print("\nthe three modes")
    reload_with()

    # --- agent: deepseek plans once, qwen writes, and nothing is negotiated
    frames, done, started, calls = turn(session="s-agent", asked_mode="agent", decoy=DECOY)
    check("agent mode calls the two models in order", [c["body"]["model"] for c in calls],
          [DEEPSEEK_MODEL, "qwen3.8-max"])
    check("and exactly twice -- no negotiation round", len(calls), 2)
    check("the first call is the planner's", "planner" in system_of(calls[0]), True)
    check("the planner is told not to write the script", "no full script" in system_of(calls[0]),
          True)
    check("the writer is handed the plan",
          any(PLAN in (m.get("content") or "") for m in messages_of(calls[1])), True)
    check("the writer still writes", "executor" in system_of(calls[1]), True)
    check("the plan streams on its own channel", channel(frames, "plan"), PLAN)
    check("the answer is the script, not the plan", done.get("text"), SIMPLE_SCRIPT)
    check("the plan is not inside it", PLAN.splitlines()[0] in done.get("text"), False)
    check("the finished turn reports the plan separately", done.get("plan"), PLAN)
    check("and the reader's answer channel holds the script", channel(frames, "answer"),
          SIMPLE_SCRIPT)
    check("and reports its mode", done.get("mode"), "agent")
    check("the phase record names both models", [p["model"] for p in done["phases"]],
          [DEEPSEEK_MODEL, "qwen3.8-max"])
    check("the answer still went out with thinking on",
          calls[1]["body"].get("thinking_mode"), "thinking")
    # The decoy: a second model's answer may not install a continuation marker for Qwen, and may
    # not clear the one that is there either.
    check("a second model cannot take over the session's chat",
          bridge.session_meta("s-agent"), STUB["meta"])
    check("the decoy never reached the reader", DECOY in channel(frames, "plan"), False)

    # --- agent with tools: still one planner call, and the writer's rounds are its own
    frames, done, started, calls = turn(session="s-agent-tools", mode="tool", asked_mode="agent",
                                        tool="luau_check", script=None)
    check("the planner is asked once even when the writer uses tools", len(planner_calls(calls)), 1)
    check("and the writer is asked twice", len(writer_calls(calls)), 2)
    check("the first writer call carries the tool schemas",
          len(writer_calls(calls)[0]["body"].get("tools") or []), len(luau.TOOL_NAMES))
    check("the tool result comes back to the writer", len(tool_messages(writer_calls(calls)[1])), 1)
    check("and the answer is still the script", done.get("text"), SIMPLE_SCRIPT)

    # --- qwen on its own
    frames, done, started, calls = turn(session="s-qwen", asked_mode="qwen")
    check("qwen mode calls one model", [c["body"]["model"] for c in calls], ["qwen3.8-max"])
    check("with no plan asked for", any("planner" in system_of(c) for c in calls), False)
    check("and no plan on any channel", channel(frames, "plan"), "")

    # --- deepseek on its own. `plan=""` because here the second model is the writer: the stub
    # answers its call with a script rather than with a plan.
    frames, done, started, calls = turn(session="s-deepseek", asked_mode="deepseek", plan="")
    check("deepseek mode calls one model", [c["body"]["model"] for c in calls], [DEEPSEEK_MODEL])
    check("no tools are attached to it", calls[0]["body"].get("tools"), None)
    check("it is told nothing will check its answer", "No tool runs your script" in system_of(calls[0]),
          True)
    check("its thinking is on", calls[0]["body"].get("thinking"), {"type": "enabled"})
    check("the answer is still the script", done.get("text"), SIMPLE_SCRIPT)
    check("and the job says which model wrote it", done.get("mode"), "deepseek")
    check("the start response offers no tools in this mode", started.get("tools"), [])
    check("and names the mode", started.get("mode"), "deepseek")
    # The model the user complained about specifically: it fences its scripts too, and the fence is
    # taken off on this path exactly as it is on Qwen's.
    _, done, _, _ = turn(session="s-deepseek-fence", asked_mode="deepseek", plan="",
                         script="```lua\n" + SIMPLE_SCRIPT + "\n```")
    check("deepseek mode ships its script without a fence", done.get("text"), SIMPLE_SCRIPT)

    # --- a mode this service cannot run is refused by name, never served by the other model
    reload_with(DEEPSEEK_TOKEN=UNSET)
    body = {"messages": [{"role": "user", "content": "hi"}], "mode": "deepseek"}
    refused = client.post("/chat/stream", json=body)
    check("a mode with no credential is refused", refused.status_code, 503)
    check_true("and says which variable it wants", "DEEPSEEK_TOKEN" in refused.text)
    refused = client.post("/chat/stream", json={"messages": body["messages"], "mode": "agent"})
    check("agent mode needs both", refused.status_code, 503)
    check_true("and names the one that is missing", "DEEPSEEK_TOKEN" in refused.text)
    unknown = client.post("/chat/stream", json={"messages": body["messages"], "mode": "gpt-5"})
    check("an unknown mode is a 400", unknown.status_code, 400)
    check_true("and lists the real ones", "agent, qwen, deepseek" in unknown.text)
    check("with no mode asked for, qwen is what runs", bridge.resolve_mode(""), "qwen")

    # --- the default follows the service's own setting, and falls back when it cannot run
    reload_with(CHAIN_MODE="deepseek")
    check("CHAIN_MODE decides the default", client.get("/health").json()["mode"], "deepseek")
    reload_with(CHAIN_MODE="agent")
    check("and it can be the agent", client.get("/health").json()["mode"], "agent")
    reload_with(CHAIN_MODE="agent", DEEPSEEK_TOKEN=UNSET)
    state = client.get("/health").json()
    check("a default that cannot run falls back to one that can", state["mode"], "qwen")
    check("the picker reports all three", [(m["id"], m["on"]) for m in state["modes"]],
          [("agent", False), ("qwen", True), ("deepseek", False)])
    check("and what the off ones need",
          sorted({n for m in state["modes"] if not m["on"] for n in m["needs"]}),
          ["DEEPSEEK_TOKEN"])
    reload_with()


def surface_checks():
    print("\nthe surface")
    body = client.get("/health").json()
    check("health names the writer", body["model"], "qwen3.8-max")
    check("health names the chain the default mode runs", body["chain"], bridge.mode_label("qwen"))
    check("health lists the three modes", [m["id"] for m in body["modes"]],
          ["agent", "qwen", "deepseek"])
    check("all three can run here", [m["on"] for m in body["modes"]], [True, True, True])
    check("health reports the second credential", body["deepseek"]["configured"], True)
    check("and that its key works, without a reviewer anywhere", body["deepseek"]["ok"], True)
    check("health has no reviewer", any("reviewer" in body for _ in [0]), False)
    check("health has no second reader", any("second" in body for _ in [0]), False)
    check("health lists the toolbox", sorted(body["tools"]["names"]), sorted(luau.TOOL_NAMES))
    check("the dump state is reported without fetching it", "dump_classes" in body["tools"], True)
    check("the tool rounds are reported", body["rounds"], bridge.AGENT_ROUNDS)
    models = client.get("/v1/models", headers={"X-API-Key": "qwen-test-token"}).json()
    ids = [m["id"] for m in models["data"]]
    check("both models are listed, the writer first", ids[:2], ["qwen3.8-max", DEEPSEEK_MODEL])
    check("no glm anywhere", [i for i in ids if "glm" in i], [])
    page = client.get("/").text
    check("every placeholder was replaced", sorted(set(re.findall(r"__[A-Z_]+__", page))), [])
    check("the page has the chips it needs", sorted(re.findall(r'data-chip="(\w+)"', page)),
          ["api", "bridge", "deepseek", "executor", "mode", "model", "roblox", "token",
           "tools"])
    check("the page is wired for a session", 'localStorage.getItem(SESSION_KEY)' in page, True)
    check("the page has a mode picker", 'id="modePick"' in page, True)
    picker = re.search(r'data-modes="([^"]*)"', page)
    # The JSON sits in an HTML attribute, so the quotes in it are entities until they are read back.
    check("and it is fed the three modes",
          json.loads(html.unescape(picker.group(1))) if picker else None,
          client.get("/health").json()["modes"])
    check("the page sends the mode it picked", "mode: MODE" in page, True)
    check("health reports the writer's default", body["thinking_default"], bridge.writer_thinking(""))
    check("and offers the two settings", [c["id"] for c in body["thinking_choices"]],
          ["thinking", "fast"])
    check("the page has a thinking picker", 'id="thinkPick"' in page, True)
    check("and sends what it picked", "thinking: THINK" in page, True)
    check("an unknown job is a 404", client.get("/chat/poll/nope").status_code, 404)
    check("a key is required when one is set",
          client.get("/agent/pull").status_code, 401)


# The client's own tool names, and the machine-reading ones it must not have: the Roblox client
# reads the game it was executed in, never the player's files.
GAME_TOOLS = {"DEEPSCAN", "REMOTES", "SCRIPTS", "MODULES", "GREP", "SOURCE", "DECOMPILE",
              "TREE", "PROPS", "FIND", "DUMP_STRINGS", "FIRE", "HOOKFN", "UPVALUES",
              "CONSTANTS", "GETGC", "ENV", "EXEC", "RUN", "CONSOLE", "PLAYERS", "SELF"}
DEVICE_TOOLS = {"FILES", "READ", "WRITE", "LISTFILES", "READFILE", "WRITEFILE", "APPEND",
                "GETCUSTOMASSET", "MAKEFOLDER", "DELETEFILE"}


def idle_checks():
    """The ceiling on silence, which is the failure that reads as a model thinking.

    Two clocks, and they cover different halves of it. The service holds an HTTP client against a
    provider that can stop sending mid-answer without ever closing: the read bound is what turns
    that into a 504 with the reason in it. The client's own clock is the other half -- a service
    that is up, answering polls, and no longer moving -- and there is no Luau here to run, so it is
    read out of ghaith.lua in client_checks.
    """
    print("\nthe ceiling on silence")
    reload_with()
    idle = bridge.client_timeout(bridge.CHAT_TIMEOUT)
    check("no ceiling on a turn by default", bridge.CHAT_TIMEOUT, 0.0)
    check("but a bound on the silence", idle.read, 120.0)
    check("which is CHAT_IDLE", idle.read, bridge.CHAT_IDLE)
    check("connecting is still bounded", idle.connect, 10.0)
    check("and the answer as a whole still is not", idle.write, None)
    check("a per-call ceiling is still a ceiling", bridge.client_timeout(30.0).read, 30.0)
    check("and 0 puts waiting forever back", bridge.client_timeout(0, idle=0).read, None)

    # A provider that opens a stream and then says nothing: the stub holds the connection wide
    # open, so nothing here is an EOF, a close or an error -- only silence -- and the turn has to
    # end anyway. The bound is turned down to a second so the check is a second long.
    reload_with(CHAT_IDLE="1")
    began = time.time()
    frames, done, started, calls = turn(mode="stall", session="s-stall")
    took = time.time() - began
    errors = [f["error"] for f in frames if f.get("error")]
    check_true("a stream that goes quiet fails the turn", bool(errors))
    check("nothing was published as a finished answer", done, {})
    check_true("and the reason is the silence, in seconds", "sent nothing for 1s" in errors[0])
    check_true("naming the model rather than the host", bridge.QWEN_MODEL in errors[0])
    check("so it is not passed off as a host that cannot be reached",
          "cannot reach" in errors[0] or "cannot connect" in errors[0], False)
    check_true("and the turn ends before the connection does", took < STUB["hold"])
    reload_with()


def client_checks():
    """The Roblox client, read as the other half of the tool protocol.

    The service hands the model the brief the client sends it, so the two halves only work while
    they agree: a token the client has no tool for comes back "there is no such tool", and a tool
    the client has and the brief does not mention is never called. What is checked here is the
    client's own table, that everything in it reads the *game* rather than the machine the client
    is running on, the two places where an answer is read for a script -- the pane and the copier,
    which must never hold prose and must both read a short script as one -- and the console error
    that becomes a turn of its own for the script that printed it.
    """
    print("\nthe Roblox client")
    source = Path(__file__).with_name("ghaith.lua").read_text(encoding="utf-8")
    table = source.split("local TOOLS = {", 1)[1].split("\n}", 1)[0]
    names = re.findall(r'\{name = "([A-Z_]+)"', table)
    check("the client's tool table was read", len(names) > 20, True)
    check("every game-reading tool is in it", sorted(GAME_TOOLS - set(names)), [])
    check("and nothing that reads or writes the player's machine",
          sorted(DEVICE_TOOLS & set(names)), [])
    check("the count the header claims is the count there is", len(names), 29)
    check("and the header claims it in words", "Twenty-nine of them" in source, True)
    check("the brief goes out with every turn",
          'MSGS = {{role = "system", content = SYSTEM .. "\\n\\n" .. tool_brief()}}' in source,
          True)
    check("the brief says a token is not code",
          "never put one inside the script you send back" in source, True)
    check("the pane holds what copy would copy", "local sofar = only_code(text)" in source, True)
    check("and only_code's fallback has to read as code",
          "if all_code(bare) then code = bare end" in source, True)
    check("the scanner reads both token shapes", "@@([A-Z_]+)" in source, True)
    # Only as the comment that records what it was: `@@([A-Z_]+)%s*([^@]*)@@` ate the next call's
    # opening token as its own closing one, so half the model's calls were never run.
    stale = [line for line in source.splitlines()
             if "@@([A-Z_]+)%s*([^@]*)@@" in line and not line.lstrip().startswith("--")]
    check("the one-pattern reader that ate the next call is gone", stale, [])
    check("a path is read however it was written", "SERVICE_NAME" in source, True)
    check("and a path that is not there says what is",
          "names in the game closest to" in source, True)
    # Where the client says what the turn is doing. The line used to be a second row of the header;
    # it is the last row of the transcript now -- under the newest answer, in the same run of the
    # page as the buttons that copy and run it -- and the header's freed row went to the script box.
    check("the status line is the last row of the transcript",
          "LayoutOrder = 1000000, ZIndex = 52, Parent = feed" in source, True)
    check("and not a second line in the header", "pulse_dot" in source, False)
    check("the script box takes the row it gave up, and a share of the screen",
          "CODE_H = math.clamp(math.floor(panel.h * 0.32)" in source, True)
    check("and the ask box is measured from the screen the same way",
          "local INPUT_H = math.clamp(math.floor(panel.h *" in source, True)
    check("which is the height the ask box is built at",
          "input_size = UDim2.new(1, -156, 0, INPUT_H)" in source, True)
    check("and it is legible rather than a sliver",
          "PlaceholderColor3 = C.dim, TextColor3 = C.text, Font = SANS, TextSize = 16,"
          in source, True)
    # Stacked, the two things that grow -- the script box and the transcript -- split the room the
    # fixed rows leave, because a 120-pixel floor on the transcript used to push it down into the
    # ask box on a short screen: 'neither can push the other' is a claim, and this is it tested.
    check("stacked, the script box and the transcript share what is left",
          "(panel.h - NARROW_TOP - NARROW_BELOW) * 0.45" in source, True)
    check("and the stacked transcript is measured from that box",
          source.count("NARROW_TOP + CODE_H + NARROW_BELOW"), 2)
    check("with no floor left that could land on the ask box",
          source.count("math.max(0, panel.h - (NARROW_TOP + CODE_H + NARROW_BELOW))"), 1)
    # copy code, run and full were rail buttons that had to answer "no script yet"; they are built
    # under the answer that carries a script now, so the rail never shows one with nothing to do.
    check("copy, run and full are not buttons on the rail",
          [word for word in ('Text = "run last"', 'Text = "copy code"', 'Text = "full script"')
           if word in source], [])
    check("they are built under an answer that carries a script",
          'if kind == "answer" and only_code(tostring(code or "")) ~= "" then' in source, True)
    check("and the SCRIPT label says how much is in the box",
          '" chars  ·  " ..' in source, True)
    check("the status line stays in view while it updates",
          "then scroll_down() end" in source, True)
    # An answer that is nothing but code is a script however short it is. A three-word one sat in
    # the pane while the turn's own status line said "no script", because extract's 80-character
    # floor turned it down and only_code's fallback did not: the two readers agree on it now.
    check("a whole answer that is code is read as a script whatever its length",
          "if all_code(bare) then return bare end" in source, True)
    # The console error nobody asked for, turned into a turn: the error is the question, the script
    # that produced it is already the newest assistant turn of that chat, and what comes back is
    # read by the same reader as any other answer.
    check("a console error becomes a turn of its own",
          [want for want in ('local ERROR_LINE = "^[%w_%.]+:%d+:"', "error_fix = function(line)",
                             "task.spawn(error_fix,", "process(table.concat({")
           if want not in source], [])
    check("the same error twice is one question, not two",
          "if problem == last_error then return end" in source, True)
    check("and a script that fails on every frame cannot spend the conversation",
          "if error_fix_rounds >= ERR_FIX_MAX then" in source, True)
    check("a clean run, or a question the player asked, puts the budget back",
          source.count('error_fix_rounds, last_error = 0, ""'), 2)
    check("and the button that stops the watching is on the rail",
          '"errors: ON"' in source, True)
    # The other half of the same failure, and the one that was actually seen: a service that is
    # up, answering polls, and no longer moving. The status line still says "thinking", the clock
    # reads 555s, and nothing is coming -- so the turn is dropped rather than watched forever,
    # with the silence said while it lasts and the last thing the service said in the reason.
    check_true("the client gives up on a turn that has stopped moving",
               re.search(r"local STALL\s+= 150", source))
    check("and what counts as moving is anything the turn produced, thinking included",
          "local produced = #text + #thoughts + #plan + #tools_done + #last_note" in source,
          True)
    check("so a turn that produced nothing new is the one that is quiet",
          "quiet = (produced > progress) and 0 or (quiet + POLL)" in source, True)
    check("the silence is said before it is fatal",
          'nothing new for " .. math.floor(quiet) .. "s' in source, True)
    check("and giving up carries what the service last said",
          '(last_note ~= "" and last_note or "nothing")' in source, True)


def main():
    toolbox_checks()
    reload_with()
    chain_checks()
    fence_checks()
    script_checks()
    mode_checks()
    surface_checks()
    client_checks()
    idle_checks()
    print(f"\n{count[0] - len(failures)}/{count[0]} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
