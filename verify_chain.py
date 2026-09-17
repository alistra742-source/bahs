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

No keys and no network: both models are one local stub, and the Roblox API dump is a fixture.
"""
import contextlib, html, io, json, os, re, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CALLS = []
STUB = {"mode": "plain", "tool": "luau_check", "arguments": None, "openai_tool": False,
        "finish": "stop", "script": "", "finite": 0, "prose": "Let me check that first.\n",
        "plan": "", "decoy": "",
        "meta": '<!-- qwen_metadata: {"response_id":"r1"} -->'}

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
    "DEEPSEEK_SHAPE")}

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
         messages=None, prose=":ASK:", asked_mode=None, plan=None, decoy=""):
    """One turn, watched to the end, with the model stub configured for it.

    `mode` is what the stub does; `asked_mode` is the mode the request asks for, which is empty
    (the service's default) unless a test is about a specific chain.
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
    check("the reasoning is dropped",
          any(REASONING in (f.get("t") or "") for f in frames), False)
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

    print("\nno tools, when they are switched off")
    reload_with(AGENT_TOOLS="off")
    frames, done, started, calls = turn()
    check("one call, no schemas", len(writer_calls(calls)[0]["body"].get("tools") or []), 0)
    check("and the answer still arrives", done.get("text"), SIMPLE_SCRIPT)
    reload_with()


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
    check("an unknown job is a 404", client.get("/chat/poll/nope").status_code, 404)
    check("a key is required when one is set",
          client.get("/agent/pull").status_code, 401)


def main():
    toolbox_checks()
    reload_with()
    chain_checks()
    mode_checks()
    surface_checks()
    print(f"\n{count[0] - len(failures)}/{count[0]} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
