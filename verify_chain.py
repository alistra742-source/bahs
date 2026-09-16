"""Check the chain against stubbed providers, including chat.deepseek.com's own endpoints.

Run from the project root:  .venv/bin/python verify_chain.py

What the chain is now: each reader is sent its own brief on its own and its answer is waited
for, the writer drafts, the first reader writes its own version of that script, the writer merges
the two in the chat it drafted in and the first reader says whether it would ship the merge;
then the second reader does all of that over the script the first two settled on.
"""
import contextlib, io, json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CALLS = []
STUB = {"users_code": 0, "api_code": 0, "bad_challenge": False, "always_better": False,
        "message_seq": 0, "choice_draft": False, "choice_merge": False, "choice_dud": False,
        "merges": 0, "zai_calls": 0, "zai_fail_after": 9999}

# The three things a model can be asked for in this chain, and what each one gets back. The
# scripts are small but real: a merge is only ever attempted on something that looks like code.
DRAFT_CODE = ("-- walk script\n"
              "local Players = game:GetService(\"Players\")\n"
              "local speed = 16\n"
              "local function speedUp(plr)\n"
              "    local humanoid = plr.Character and "
              "plr.Character:FindFirstChildOfClass(\"Humanoid\")\n"
              "    if humanoid then humanoid.WalkSpeed = speed end\n"
              "end\n"
              "Players.PlayerAdded:Connect(speedUp)")
PEER_CODE = ("-- walk script\n"
             "local Players = game:GetService(\"Players\")\n"
             "local SPEED = 16\n"
             "local function onPlayer(plr)\n"
             "    plr.CharacterAdded:Connect(function(char)\n"
             "        local humanoid = char:WaitForChild(\"Humanoid\")\n"
             "        humanoid.WalkSpeed = SPEED\n"
             "    end)\n"
             "end\n"
             "Players.PlayerAdded:Connect(onPlayer)")
MERGED_CODE = ("-- walk script, merged\n"
               "local Players = game:GetService(\"Players\")\n"
               "local SPEED = 16\n"
               "local function speed(plr)\n"
               "    plr.CharacterAdded:Connect(function(char)\n"
               "        char:WaitForChild(\"Humanoid\").WalkSpeed = SPEED\n"
               "    end)\n"
               "end\n"
               "Players.PlayerAdded:Connect(speed)")
# The second reader's own version of that script, and the merge that comes out of it. Two
# different scripts, so "which merge is what shipped" is answerable rather than assumed.
ZAI_CODE = ("-- walk script, second reader\n"
            "local Players = game:GetService(\"Players\")\n"
            "local SPEED = 16\n"
            "local function apply(character)\n"
            "    local humanoid = character:FindFirstChildOfClass(\"Humanoid\")\n"
            "    if humanoid then humanoid.WalkSpeed = SPEED end\n"
            "end\n"
            "local function onPlayer(plr)\n"
            "    if plr.Character then apply(plr.Character) end\n"
            "    plr.CharacterAdded:Connect(apply)\n"
            "end\n"
            "Players.PlayerAdded:Connect(onPlayer)")
MERGED2_CODE = ("-- walk script, merged twice\n"
                "local Players = game:GetService(\"Players\")\n"
                "local SPEED = 16\n"
                "local function apply(character)\n"
                "    local humanoid = character:WaitForChild(\"Humanoid\", 10)\n"
                "    if humanoid then humanoid.WalkSpeed = SPEED end\n"
                "end\n"
                "local function onPlayer(plr)\n"
                "    if plr.Character then apply(plr.Character) end\n"
                "    plr.CharacterAdded:Connect(apply)\n"
                "end\n"
                "Players.PlayerAdded:Connect(onPlayer)")


def fenced(code):
    return "```lua\n" + code + "\n```"


# Two scripts of very different lengths, offered with the question the writer asks when it cannot
# decide. The bridge answers that question itself, and the longer one is what has to ship.
CHOICE_SHORT = ("-- walk script, simple\n"
                "local p = game:GetService(\"Players\")\n"
                "p.PlayerAdded:Connect(function(plr) plr.CharacterAdded:Connect(function(c)\n"
                "    c:WaitForChild(\"Humanoid\").WalkSpeed = 16\n"
                "end) end)")
CHOICE_LONG = ("-- walk script, robust\n"
               "local Players = game:GetService(\"Players\")\n"
               "local SPEED = 16\n"
               "local function apply(character)\n"
               "    local humanoid = character:WaitForChild(\"Humanoid\", 10)\n"
               "    if not humanoid then return end\n"
               "    humanoid.WalkSpeed = SPEED\n"
               "end\n"
               "local function onPlayer(plr)\n"
               "    if plr.Character then apply(plr.Character) end\n"
               "    plr.CharacterAdded:Connect(apply)\n"
               "end\n"
               "Players.PlayerAdded:Connect(onPlayer)\n"
               "for _, plr in ipairs(Players:GetPlayers()) do onPlayer(plr) end")
CHOICE_QUESTION_TEXT = "Which choice do you prefer?"
CHOICE_DRAFT = ("Here are two ways to do it.\n\nOption 1 (simple):\n" + fenced(CHOICE_SHORT)
                + "\n\nOption 2 (robust):\n" + fenced(CHOICE_LONG)
                + "\n\n" + CHOICE_QUESTION_TEXT)
# The sentence only the reply to that question carries, which is how the stub routes it.
CHOICE_ASK = "I prefer option"

SEED_ACK = "kanha:ready"
DRAFT = fenced(DRAFT_CODE)
PEER = "VERDICT: BETTER\n" + fenced(PEER_CODE)
MERGED = fenced(MERGED_CODE)
MERGED2 = fenced(MERGED2_CODE)
AGREE = "VERDICT: AGREE"
# The second reader's two answers. The same asks carry the same words, so its answers are routed
# by the endpoint they arrive at (/zai/...) rather than by anything in the prompt.
ZAI_PEER = "VERDICT: BETTER\n" + fenced(ZAI_CODE)
# The sentence that only a peer request carries, and the one only an agreement question carries.
PEER_ASK = "Write the version of this script you would ship"
VERIFY_ASK = "Would you ship this exactly as it is?"
SEED_ASK = "That is your standing instruction set"
MERGE_ASK = "and wrote its own version of it"
THINKING = "SECRET_THINKING_TEXT"
REASONING = "SECRET_REASONING_TEXT"


def frame(piece, finish=None, kind="RESPONSE"):
    delta = {"content": piece, "type": kind} if piece else {}
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def raw_frame(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def reasoning_frame(text):
    """A thinking token on the writer's own stream, which is what thinking mode adds."""
    return "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": text}}]}) + "\n\n"


def stream(text, reasoning=REASONING):
    """An OpenAI-shaped answer, with the thinking that precedes it on the same stream."""
    return ((reasoning_frame(reasoning) if reasoning else "") + frame(text)
            + frame(None, "stop") + "data: [DONE]\n\n").encode()


def reply_to(prompt):
    """What a provider answers, decided by what it was asked -- the ask is the whole contract."""
    if CHOICE_ASK in prompt:
        # The bridge has just named the option it wants; the writer hands it over. `choice_dud`
        # is a writer that will not decide even when told which one, which must still end in a
        # script rather than in the question again.
        return "I cannot decide for you." if STUB["choice_dud"] else fenced(CHOICE_LONG)
    if SEED_ASK in prompt:
        return SEED_ACK
    if PEER_ASK in prompt or (VERIFY_ASK in prompt and STUB["always_better"]):
        return PEER
    if VERIFY_ASK in prompt:
        return AGREE
    if MERGE_ASK in prompt:
        if STUB["choice_merge"]:
            return CHOICE_DRAFT
        # There is a merge per reader, and they are told apart by the script they came out of:
        # the second one is over the second reader's version, and it is what should ship.
        STUB["merges"] += 1
        return MERGED if STUB["merges"] == 1 else MERGED2
    return CHOICE_DRAFT if STUB["choice_draft"] else DRAFT


def reply_to_second(prompt):
    """What the second reader answers: its own script, then its verdict on the merge.

    Routed by endpoint, not by the prompt: both readers are asked for a version of a script in
    exactly the same words, so there is nothing in the text to tell them apart.
    """
    if SEED_ASK in prompt:
        return SEED_ACK
    if PEER_ASK in prompt:
        return ZAI_PEER
    if VERIFY_ASK in prompt:
        return AGREE
    return AGREE


def ds_stream(piece):
    """A chat.deepseek.com answer split across both frame shapes the site has used.

    A thinking frame, a status frame and the id of the message being written ride along: the
    first two must never reach the text, and the id is what threads the next message onto this
    one in the same chat.
    """
    STUB["message_seq"] += 1
    message_id = f"msg-{STUB['message_seq']}"
    return "".join([
        frame(piece),
        frame(THINKING, kind="thinking"),
        raw_frame({"p": "response/message_id", "v": message_id}),
        raw_frame({"p": "response/status", "v": "FINISHED"}),
        "data: [DONE]\n\n",
    ]).encode()


def content_of(body):
    return "\n".join(m["content"] for m in body.get("messages", [])
                     if isinstance(m.get("content"), str))


def latest_ask(body):
    """The newest thing asked, which is what a real provider would be answering.

    The stub decides from this rather than from the whole conversation: on the API path the
    turns before it are still there (the brief, the acknowledgement), so matching against all of
    them would answer the question that was asked three calls ago.
    """
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


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
            # Each reader's own model list, so a chip that says "model served" is checking the
            # endpoint it will actually call.
            if "/zai/" in self.path:
                return self._send(200, {"object": "list",
                                        "data": [{"id": "glm-5.3-flash"},
                                                 {"id": "glm-5.3"}]})
            return self._send(200, {"object": "list", "data": [{"id": "deepseek-v4-flash"}]})
        if self.path.endswith("/users/current"):
            if STUB["users_code"]:
                return self._send(200, {"code": STUB["users_code"],
                                        "msg": "Authorization Failed (invalid token)"})
            return self._send(200, {"code": 0, "data": {"biz_data": {"user": {"id": "u1"}}}})
        return self._send(404, {"error": {"message": "no such route"}})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw) if raw else {}
        if self.path.endswith("/v1/validate"):
            return self._send(200, {"valid": True})
        record = {"path": self.path, "body": body,
                  "headers": {k.lower(): v for k, v in self.headers.items()}}
        CALLS.append(record)
        if STUB["api_code"] and self.path.endswith("/api-reject/chat/completions"):
            # The API's own wording when a chat.deepseek.com session token is presented as a key.
            return self._send(STUB["api_code"],
                              {"error": {"message": "Authentication Fails, Your api key: "
                                                        "****VEFK is invalid"}})
        if self.path.endswith("/chat_session/create"):
            return self._send(200, {"code": 0, "data": {"biz_data": {"id": "sess-1"}}})
        if self.path.endswith("/chat/create_pow_challenge"):
            challenge = {
                "algorithm": "DeepSeekHashV1",
                "challenge": POW_CHALLENGE or "cannot-be-solved-here",
                "salt": POW_SALT, "difficulty": POW_DIFFICULTY, "expire_at": POW_EXPIRE,
                "signature": "sig", "target_path": "/api/v0/chat/completion"}
            if STUB["bad_challenge"]:
                # A challenge with no answer below its difficulty: the solve has to come back
                # empty rather than the request inventing a header.
                challenge["challenge"] = "0" * 64
            return self._send(200, {"code": 0, "data": {"biz_data": {"challenge": challenge}}})
        if self.path.endswith("/chat/completion"):
            # The site takes one prompt, so one string is both the ask and the answer's routing.
            return self._send(200, ds_stream(reply_to(body.get("prompt") or "")),
                              "text/event-stream")
        if "/zai/" in self.path:
            # The second reader's calls are counted, so a failure can be planted at a chosen
            # point in its rounds rather than only before the first one.
            STUB["zai_calls"] += 1
            if STUB["zai_calls"] > STUB["zai_fail_after"]:
                return self._send(500, {"error": {"code": "1301", "message": "internal error"}})
            return self._send(200, stream(reply_to_second(latest_ask(body))), "text/event-stream")
        return self._send(200, stream(reply_to(latest_ask(body))), "text/event-stream")


class StubServer(ThreadingHTTPServer):
    # Without this a handler thread sits in readline() on a pooled keep-alive connection and the
    # process never exits, which looks exactly like a hung test.
    daemon_threads = True
    allow_reuse_address = True


PORT = 8143
stub = StubServer(("127.0.0.1", PORT), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()

# The second reader's brief is a real file, so those checks are about *its* brief rather than
# about the fallback. It is written outside the repo, so a test run leaves nothing behind.
BRIEF2_FIXTURE = Path(tempfile.gettempdir()) / "bahs-second-brief.txt"
BRIEF2_FIXTURE.write_text("SECOND READER BRIEF (fixture)\n"
                         + "Standing instructions for the second reader. " * 40, encoding="utf-8")

os.environ.update({
    "QWEN_URL": f"http://127.0.0.1:{PORT}/v1",
    "QWEN_TOKEN": "qwen-test-token",
    "REVIEW_URL": f"http://127.0.0.1:{PORT}/deepseek",
    "DEEPSEEK_TOKEN": "review-test-key",
    "ZAI_URL": f"http://127.0.0.1:{PORT}/zai",
    "ZAI_TOKEN": "zai-test-key",
    "ZAI_MODEL": "glm-5.3-flash",
    "SECOND_BRIEF": str(BRIEF2_FIXTURE),
    "HEARTBEAT": "0.2",
    # /health remembers a provider's answer for a minute so the page's polling does not turn into
    # a network call every 8 seconds; the test wants every check to be a fresh one.
    "TOKEN_CHECK_TTL": "0",
})
for name in ("REVIEW_SHAPE", "API_KEY", "DEEPSEEK_COOKIE", "SEED_BRIEF", "NEGOTIATE_ROUNDS",
             "QWEN_THINKING", "CHOICE_ROUNDS", "SECOND_ROUNDS", "SECOND_SEED", "ZAI_THINKING"):
    os.environ.pop(name, None)

# How the test starts, so a section that sets a variable cannot leak it into the next one: every
# reload begins from this, not from whatever the section before it happened to leave behind.
BASE_ENV = {name: os.environ.get(name) for name in (
    "QWEN_URL", "QWEN_TOKEN", "REVIEW_URL", "DEEPSEEK_TOKEN", "REVIEW_SHAPE", "API_KEY",
    "SEED_BRIEF", "NEGOTIATE_ROUNDS", "CHAT_TIMEOUT", "TOKEN_CHECK_TTL", "QWEN_THINKING",
    "CHOICE_ROUNDS", "ZAI_URL", "ZAI_TOKEN", "ZAI_MODEL", "ZAI_THINKING", "SECOND_ROUNDS",
    "SECOND_SEED", "SECOND_BRIEF", "ZAI_BRIEF")}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402
import base64  # noqa: E402
import pow_solver  # noqa: E402
import bridge  # noqa: E402
import state  # noqa: E402
import peers  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# A challenge the site would hand out, made the way the site makes it: a hash of the prefix plus a
# small integer. The solver has to recover that integer, and the stub answers only when it does.
POW_W, POW_SALT, POW_EXPIRE, POW_DIFFICULTY = 4242, "9f8e7d6c5b4a39281706f5e4d3c2b1a0", 1789548668, 144000
_solver = pow_solver.load()
POW_CHALLENGE = (_solver.digest(pow_solver.prefix_for(POW_SALT, POW_EXPIRE) + str(POW_W))
                 if _solver is not None else "")

client = TestClient(server.app)
failures, count = [], [0]


def check(name, got, want):
    count[0] += 1
    if got != want:
        failures.append(name)
        print(f"  FAIL {name}: got {got!r}, wanted {want!r}")
    else:
        print(f"  ok   {name}")


def turn(question="make me a walk script"):
    CALLS.clear()
    STUB["merges"] = 0
    STUB["zai_calls"] = 0
    start = client.post("/chat/stream", json={"messages": [{"role": "user", "content": question}]})
    out = []
    with client.stream("GET", f"/chat/stream/{start.json()['job']}") as r:
        for line in r.iter_lines():
            if line.strip():
                out.append(json.loads(line))
    done = [f for f in out if f.get("done")]
    channels = "".join(f.get("t", "") for f in out if f.get("ch") == "seed")
    return out, (done[0] if done else {}), CALLS[:], channels


# `reload_with(REVIEW_URL=UNSET)` is how a section asks for a variable to be *absent*, which is
# not the same as setting it to the value the test normally starts with.
UNSET = object()


def reload_with(**env):
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
    # The config and the providers live in bridge, the checks in state, the second reader in
    # peers, and server binds all of their names at import -- so every one of them is reloaded,
    # lowest first, or a reloaded server would still hold the previous providers.
    importlib.reload(bridge)
    importlib.reload(state)
    importlib.reload(peers)
    server = importlib.reload(server)
    client = TestClient(server.app)


def reviewer_calls(calls):
    """Only the reviewer's calls, in order, whichever shape its endpoint takes."""
    return [c for c in calls if c["body"].get("model") == "deepseek-v4-flash"
            or c["path"].endswith("/chat/completion")]


def writer_calls(calls):
    """Only the writer's calls, in order -- the draft, a merge, the answer to a choice."""
    return [c for c in calls if c["body"].get("model") == "qwen3.8-max"]


def second_calls(calls):
    """Only the second reader's calls, in order, whichever shape its endpoint takes."""
    return [c for c in calls if c["body"].get("model") == "glm-5.3-flash"
            or "/zai/" in c["path"]]


def phases_of(done, parallel=("draft", "seed", "seed2")):
    """The chain's phases, with the ones that run in parallel sorted first.

    Each brief is read on its own thread while the writer drafts, so those three can be recorded
    in any order. Everything after them is strictly ordered, which is what these checks are for.
    """
    got = [p["phase"] for p in done["phases"]]
    return [sorted(p for p in got if p in parallel),
            [p for p in got if p not in parallel]]


def streamed(out, channel="answer"):
    """What one channel ends up holding, resets included -- which is what a reader replays."""
    text = ""
    for item in out:
        if item.get("ch") != channel:
            continue
        if item.get("reset"):
            text = ""
        else:
            text += item.get("t", "")
    return text


def asked_in(call):
    """What one reviewer call was actually asked, as one string."""
    if call["path"].endswith("/chat/completion"):
        return call["body"].get("prompt") or ""
    return "\n".join(m.get("content") or "" for m in call["body"].get("messages") or [])


def asked_last(call):
    """The newest thing in one reviewer call: what the warning has to be on the front of.

    On the API path the earlier turns are carried with it, and those carry a warning of their
    own, so joining them all would pass even if the newest message had none.
    """
    if call["path"].endswith("/chat/completion"):
        return call["body"].get("prompt") or ""
    for message in reversed(call["body"].get("messages") or []):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


print("\n-- the brief goes first, on its own, and the request only after it --")
out, done, calls, seed_stream = turn()
review = reviewer_calls(calls)
# The brief is read on its own thread while the draft is written, so those two may be recorded
# in either order; everything after them is strictly ordered.
phases = [p["phase"] for p in done["phases"]]
check("the draft and both briefs are read together, then the first reader's rounds",
      phases_of(done), [["draft", "seed", "seed2"],
                        ["peer", "merge", "agree", "peer2", "merge", "agree2"]])
check("and the reviewer's own first message is the brief",
      review[0]["path"].endswith("/deepseek/chat/completions"), True)
first = asked_in(review[0])
check("the first message opens with the executor warning",
      first.startswith(server.REVIEW_WARNING), True)
check("and then carries send.txt whole, rather than a part of it",
      [server.BRIEF in first, len(server.BRIEF) > 1000], [True, True])
check("with nothing asked of it yet: no request, no contract",
      [ask in first for ask in ("make me a walk script", PEER_ASK, VERIFY_ASK, "VERDICT")],
      [False] * 4)
check("and its answer is waited for: the seed is its own call",
      [p["provider"] for p in done["phases"] if p["phase"] == "seed"], ["deepseek"])
check("the request goes out after it, in the same conversation",
      "make me a walk script" in asked_in(review[1]), True)
check("with the draft it is asking about", DRAFT_CODE in asked_in(review[1]), True)
check("the reviewer's acknowledgement is streamed to the reader", seed_stream, SEED_ACK)
check("what the reviewer is asked for is a script, not a list of complaints",
      "VERDICT: BETTER" in asked_in(review[1]), True)
check("every message to the reviewer opens with the warning",
      [asked_last(c).startswith(server.REVIEW_WARNING) for c in review], [True] * len(review))
check("and the target is named as an executor, not as Studio",
      ["Studio" in server.TARGET_RUNTIME, "executor" in server.TARGET_RUNTIME], [True, True])
check("and the version it wrote is shown as its own channel", done.get("peer"), PEER_CODE)
check("the writer merged, once per reader, in the chat it drafted in",
      [p["phase"] for p in done["phases"]].count("merge"), 2)
check("and the answer is the merge both readers settled on", done.get("text"), MERGED2_CODE)
check("the reviewer was asked whether it would ship the merge",
      "Would you ship this exactly as it is?" in asked_in(review[2]), True)
check("and its verdict is kept", done.get("review"), AGREE)
check("thinking is off in every reviewer call",
      [c["body"].get("thinking") for c in review], [{"type": "disabled"}] * 3)
check("no search parameter is ever sent",
      sorted(k for c in review for k in c["body"] if "search" in k.lower()), [])

print("\n-- each merge is a call, and it carries that reader's version --")
merge_calls = [c for c in calls if MERGE_ASK in content_of(c["body"])]
check("a merge per reader", len(merge_calls), 2)
check("the first merge is asked for in the chat the draft was written in",
      merge_calls[0]["body"]["messages"][-2]["content"], DRAFT_CODE)
check("and carries the first reader's version",
      PEER_CODE in merge_calls[0]["body"]["messages"][-1]["content"], True)
check("the second merge edits the script the first two settled on",
      merge_calls[1]["body"]["messages"][-2]["content"], MERGED_CODE)
check("and carries the second reader's version",
      ZAI_CODE in merge_calls[1]["body"]["messages"][-1]["content"], True)
check("naming the reader it came from, so the instruction is about the right model",
      "glm-5.3-flash" in merge_calls[1]["body"]["messages"][-1]["content"], True)
check("the writer's own script was not resent as history",
      len([m for m in merge_calls[0]["body"]["messages"] if m["role"] == "user"]), 2)

print("\n-- a reviewer that never agrees is bounded by the rounds --")
STUB["always_better"] = True
out, done, calls, _ = turn()
phases = [p["phase"] for p in done["phases"]]
# Round one is the reviewer writing its own version; every round after it is an agreement
# question that came back with another version, and every one of them is followed by a merge.
check("five rounds for the first reader: a version and a merge each, then it stops anyway",
      [sorted(p for p in phases if p in ("draft", "seed", "seed2")),
       phases.count("peer"), phases.count("agree"), phases.count("merge")],
      [["draft", "seed", "seed2"], 1, 4, 6])
check("and the second reader's own rounds are still just its two",
      [phases.count("peer2"), phases.count("agree2")], [1, 1])
check("the draft is not counted as a round",
      [p["phase"] for p in done["phases"]].count("draft"), 1)
check("and the last merged script is what ships", done.get("text"), MERGED2_CODE)
check("the turn still ends", done.get("status"), "done")
check("no more calls than both ceilings allow", len(done["phases"]), 16)
STUB["always_better"] = False

print("\n-- five is the ceiling, however the variable is set --")
reload_with(NEGOTIATE_ROUNDS="50")
check("a value past the ceiling is clamped", server.NEGOTIATE_ROUNDS, server.MAX_NEGOTIATE_ROUNDS)
reload_with(NEGOTIATE_ROUNDS="-3")
check("and a negative one means none", server.NEGOTIATE_ROUNDS, 0)
reload_with()
check("the default is five", server.NEGOTIATE_ROUNDS, 5)
check("and the ceiling is five as well", server.MAX_NEGOTIATE_ROUNDS, 5)

print("\n-- the competition can be switched off --")
reload_with(NEGOTIATE_ROUNDS="0")
out, done, calls, _ = turn()
check("only the draft runs", [p["phase"] for p in done["phases"]], ["draft"])
check("and it is the answer", done.get("text"), DRAFT_CODE)
check("no reviewer call at all", len(reviewer_calls(calls)), 0)
reload_with()

print("\n-- SEED_BRIEF=off keeps the brief, and drops only the extra call --")
reload_with(SEED_BRIEF="off")
out, done, calls, _ = turn()
check("no seed phase for the first reader", phases_of(done, ("draft", "seed2")),
      [["draft", "seed2"], ["peer", "merge", "agree", "peer2", "merge", "agree2"]])
check("and one call fewer than the default turn", len(done["phases"]), 8)
check("the brief is still in front of the first request",
      server.BRIEF[:60] in asked_in(reviewer_calls(calls)[0]), True)
check("and the warning is in front of the brief",
      asked_in(reviewer_calls(calls)[0]).startswith(server.REVIEW_WARNING), True)
check("and the contract rides with it",
      "VERDICT: BETTER" in asked_in(reviewer_calls(calls)[0]), True)
reload_with()

print("\n-- the second reader: its own brief first, then its own rounds --")
reload_with()
out, done, calls, seed2_stream = turn()
second = second_calls(calls)
check("the second reader is in the chain", server.second_enabled(), True)
check("and is named", done.get("second"), "glm-5.3-flash")
first2 = asked_in(second[0])
check("its first message opens with the executor warning",
      first2.startswith(server.REVIEW_WARNING), True)
check("and then carries send2.txt whole",
      [server.SECOND_BRIEF in first2, len(server.SECOND_BRIEF) > 1000], [True, True])
check("with nothing asked of it yet: no request, no contract",
      [ask in first2 for ask in ("make me a walk script", PEER_ASK, VERIFY_ASK, "VERDICT")],
      [False] * 4)
check("its answer is waited for before the request",
      [p["provider"] for p in done["phases"] if p["phase"] == "seed2"], ["zai"])
check("which the page streams as the second brief's own channel",
      streamed(out, "seed2"), SEED_ACK)
check("the request goes out after it, in the same conversation",
      "make me a walk script" in asked_in(second[1]), True)
check("and it is the script the first two settled on",
      MERGED_CODE in asked_in(second[1]), True)
check("with the contract riding on that request",
      "VERDICT: BETTER" in asked_in(second[1]), True)
check("every message to it opens with the warning",
      [asked_last(c).startswith(server.REVIEW_WARNING) for c in second], [True] * len(second))
check("the version it wrote is its own channel", done.get("peer2"), ZAI_CODE)
check("its verdict is kept with the turn", done.get("second_review"), AGREE)
check("and its merge is what ships", done.get("text"), MERGED2_CODE)
check("deep think is on, at the top of its ladder",
      [peers.zai_dialect().get("reasoning_effort"), peers.ZAI_THINKING], ["max", "max"])
check("thinking is enabled in every call to it",
      [c["body"].get("thinking") for c in second],
      [{"type": "enabled", "clear_thinking": True}] * len(second))
check("and no tools, so no search, in any of them",
      [k for c in second for k in c["body"]
       if k in ("tools", "tool_choice", "web_search", "web_search_options")], [])
check("what it is asked names the runtime and the checks, like the first reader's requests",
      ["TARGET:" in asked_in(second[1]), "CHECKS THIS SERVICE ALREADY RAN:" in asked_in(second[1])],
      [True, True])
check("and nothing of its thinking reaches the answer",
      [REASONING in (done.get("peer2") or ""), THINKING in (done.get("peer2") or "")],
      [False, False])
check("the page can see it before the turn starts",
      client.post("/chat/stream", json={"messages": [{"role": "user",
                                                      "content": "hi"}]}).json()["second"],
      "glm-5.3-flash")

print("\n-- the second reader's rounds are bounded, and clamped --")
reload_with(SECOND_ROUNDS="1")
out, done, calls, _ = turn()
check("one round: its own version and the merge, then it stops", phases_of(done)[1],
      ["peer", "merge", "agree", "peer2", "merge"])
reload_with(SECOND_ROUNDS="50")
check("a value past the ceiling is clamped", server.SECOND_ROUNDS, server.MAX_SECOND_ROUNDS)
reload_with(SECOND_ROUNDS="-3")
check("and a negative one means none", server.SECOND_ROUNDS, 0)
reload_with(SECOND_ROUNDS="0")
out, done, calls, _ = turn()
check("with it off the chain ends with the first reader", phases_of(done)[1],
      ["peer", "merge", "agree"])
check("the first reader's script is what ships", done.get("text"), MERGED_CODE)
check("nothing was sent to the second reader at all", len(second_calls(calls)), 0)
check("and no second reader is named", done.get("second"), "")
reload_with()
check("the default is two", server.SECOND_ROUNDS, 2)

print("\n-- SECOND_SEED=off keeps its brief and drops only the extra call --")
reload_with(SECOND_SEED="off")
out, done, calls, _ = turn()
check("no seed2 phase", "seed2" in [p["phase"] for p in done["phases"]], False)
check("but the brief is still in front of its first request",
      server.SECOND_BRIEF[:60] in asked_in(second_calls(calls)[0]), True)
check("and the contract rides with that request",
      "VERDICT: BETTER" in asked_in(second_calls(calls)[0]), True)
reload_with()

print("\n-- a second reader that cannot be reached leaves the agreed script --")
reload_with(ZAI_URL="http://127.0.0.1:9/zai")
out, done, calls, _ = turn()
check("the first reader's script still ships", done.get("text"), MERGED_CODE)
check("the turn is not an error", done.get("status"), "done")
check("and the reason is said out loud", "did not read" in (done.get("note") or ""), True)
check("the chip reports it", client.get("/health").json()["second"]["ok"], False)
reload_with()

print("\n-- a second reader that dies half-way leaves the last script as well --")
# Its first two calls land (the brief, then its version and the merge they started) and the
# verdict question is the one that fails, which is the case the guard exists for.
STUB["zai_fail_after"] = 2
out, done, calls, _ = turn()
STUB["zai_fail_after"] = 9999
check("the merge it already proposed ships", done.get("text"), MERGED2_CODE)
check("the failure is recorded", "stopped early" in (done.get("note") or ""), True)
check("and the turn is still done", done.get("status"), "done")

print("\n-- without send2.txt the first brief stands in, and says so --")
reload_with(SECOND_BRIEF=UNSET)
check("the fallback is the first reader's brief", server.SECOND_BRIEF, server.BRIEF)
health = client.get("/health").json()
check("and /health names the file that actually goes out", health["second"]["brief"],
      peers.FALLBACK_BRIEF_NAME)
check("marked as a fallback rather than passed off as send2.txt",
      health["second"]["brief_fallback"], True)
reload_with()
check("with a brief of its own, nothing is marked as a fallback",
      client.get("/health").json()["second"]["brief"], BRIEF2_FIXTURE.name)

print("\n-- the z.ai credential: a bad key and a wrong kind of token read differently --")
out, done, calls, _ = turn()
check("its model is what the chip looked for", client.get("/health").json()["second"]["model"],
      "glm-5.3-flash")
check("and the key is reported as good, model served",
      client.get("/health").json()["second"]["detail"], "key set, model served")
reload_with(ZAI_TOKEN="user-token-xyz")
refused = peers.zai_failure(401, '{"error": {"code": "1001", "message": "invalid"}}')
check("a chat.z.ai session token is refused with the fix named",
      ["chat.z.ai session token" in refused, "API key from" in refused, "invalid" in refused],
      [True, True, True])
reload_with(ZAI_TOKEN="abc.def")
check("an API key gets the platform's own words instead",
      "chat.z.ai session token" in peers.zai_failure(
          401, '{"error": {"code": "1001", "message": "invalid"}}'), False)
check("and a retired model id is named as that rather than as a bad key",
      "is not a model" in peers.zai_failure(404, '{"error": {"message": "not found"}}'), True)

print("\n-- the z.ai credential: a web session token is not a server credential --")
# The token chat.z.ai keeps in localStorage: a JWT, which is exactly what the other bridges take.
reload_with(ZAI_TOKEN="eyJhbGciOiJIUzI1NiJ9.eyJpZCI6InUifQ.c2ln")
check("a JWT is recognised as the site's session token, not as an API key",
      [peers.is_session_token(peers.ZAI_TOKEN), peers.looks_like_key(peers.ZAI_TOKEN)],
      [True, False])
check("so the stage is off, rather than spending a call per turn to be told so",
      [server.second_enabled(), server.second_state(True)["on"]], [False, False])
CALLS.clear()  # the turn above is still in here; this is about what the probe did not send
check("and nothing was sent anywhere to decide that", second_calls(CALLS), [])
check("the reason names the captcha as the wall, not the token",
      ["FRONTEND_CAPTCHA_REQUIRED" in peers.SESSION_TOKEN_NOTE,
       "API key from z.ai" in peers.SESSION_TOKEN_NOTE,
       "free models" in peers.SESSION_TOKEN_NOTE], [True, True, True])
second = client.get("/health").json()["second"]
check("the chip says which credential it is, and stays red",
      [second["ok"], second["on"], "chat.z.ai session token" in second["detail"]],
      [False, False, True])
out, done, calls, _ = turn()
check("a turn asks no reader that cannot answer", second_calls(calls), [])
check("no stage of the chain waits on it either",
      [p["phase"] for p in done["phases"] if "2" in p["phase"]], [])
check("and the answer still ships", bool(streamed(out)), True)
boot = io.StringIO()
with contextlib.redirect_stdout(boot):
    with client.__class__(server.app):
        pass
check("the boot line names it too",
      ["[second] ZAI_TOKEN is a chat.z.ai session token" in boot.getvalue(),
       "FRONTEND_CAPTCHA_REQUIRED" in boot.getvalue()], [True, True])
reload_with()

print("\n-- the brief is send.txt --")
check("send.txt is the one in use", server.BRIEF_PATH.name, "send.txt")
check("and /health names it", client.get("/health").json()["reviewer"]["brief"], "send.txt")

print("\n-- chat.deepseek.com, driven by the userToken --")
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/api/v0", REVIEW_SHAPE="deepseek-web",
            DEEPSEEK_TOKEN="user-token-xyz")
check("the web transport is in use", server.REVIEWER.web is not None, True)
check("and the reviewer is on", server.review_enabled(), True)
out, done, calls, seed_stream = turn()
web = [c for c in calls if c["path"].endswith("/api/v0/chat/completion")]
check("three reviewer messages: the brief, its version, the agreement", len(web), 3)
check("all three are in one chat", sorted({c["body"]["chat_session_id"] for c in web}), ["sess-1"])
check("and one session was opened for them",
      len([c for c in calls if c["path"].endswith("/chat_session/create")]), 1)
check("the site's message id threads the second onto the first",
      [c["body"]["parent_message_id"] for c in web], [None, "msg-1", "msg-2"])
check("the warning leads the first message, and then the whole brief",
      [web[0]["body"]["prompt"].startswith(server.REVIEW_WARNING),
       server.BRIEF in web[0]["body"]["prompt"]], [True, True])
check("the brief is not resent with the request that follows it",
      server.BRIEF[:60] in web[1]["body"]["prompt"], False)
check("and the contract rides with that request instead",
      "VERDICT: BETTER" in web[1]["body"]["prompt"], True)
check("the warning is on every message, not just the first",
      [c["body"]["prompt"].startswith(server.REVIEW_WARNING) for c in web],
      [True] * len(web))
check("the request is in that second message", "make me a walk script" in web[1]["body"]["prompt"],
      True)
check("each message asks for its own challenge",
      len([c for c in calls if c["path"].endswith("/chat/create_pow_challenge")]), 3)
check("thinking is switched off in every message",
      [c["body"]["thinking_enabled"] for c in web], [False, False, False])
check("search is switched off in every message",
      [c["body"]["search_enabled"] for c in web], [False, False, False])
check("the userToken is the bearer", web[0]["headers"].get("authorization"),
      "Bearer user-token-xyz")
check("the attempt looks like the site's", web[0]["headers"].get("x-client-platform"), "web")
check("both frame shapes were read", done.get("peer"), PEER_CODE)
check("a thinking frame never reaches the text",
      [THINKING in asked_in(c) or THINKING in (done.get("peer") or "") for c in web], [False] * 3)
check("a status frame never arrives as text", "FINISHED" in (done.get("peer") or ""), False)
check("the merge still happened, in the writer's chat", done.get("text"), MERGED2_CODE)
pow_header = web[0]["headers"].get("x-ds-pow-response")
if _solver is None:
    print("  (the proof-of-work checks are skipped: no sha3 module is available here)")
else:
    answered = json.loads(base64.b64decode(pow_header)) if pow_header else {}
    check("the proof of work is solved and sent", answered.get("answer"), POW_W)
    check("the challenge goes back with it", answered.get("challenge"), POW_CHALLENGE)
    check("and the signature that came with it", answered.get("signature"), "sig")
    check("no state the challenge did not carry", answered.get("target_path"),
          "/api/v0/chat/completion")
check("every message carries the proof of work",
      [bool(c["headers"].get("x-ds-pow-response")) for c in web], [True, True, True])
health = client.get("/health").json()
check("the chip says the token works", health["reviewer"]["detail"], "token accepted")
check("and names the shape", health["reviewer"]["shape"], "deepseek-web")
check("and how many rounds it may run", health["reviewer"]["rounds"], 5)

print("\n-- a challenge with no answer below its difficulty sends no header --")
STUB["bad_challenge"] = True
out, done, calls, _ = turn()
web = [c for c in calls if c["path"].endswith("/api/v0/chat/completion")]
check("nothing was invented for an unsolvable challenge",
      [("x-ds-pow-response" in c["headers"]) for c in web], [False, False, False])
check("and the turn still ran", done.get("text"), MERGED2_CODE)
STUB["bad_challenge"] = False

print("\n-- a reviewer that is down costs the draft, never the turn --")
STUB["users_code"] = 0
reload_with(REVIEW_URL="http://127.0.0.1:9/deepseek", REVIEW_SHAPE="openai",
            DEEPSEEK_TOKEN="review-test-key")
out, done, calls, _ = turn()
check("the draft still ships", done.get("text"), DRAFT_CODE)
check("the failure is recorded rather than silence", "did not happen" in (
    done.get("review") or ""), True)
check("and the second reader was not asked to review nothing",
      done.get("second_review"), "")
check("and the turn is not an error", done.get("status"), "done")
check("the chip reports it", client.get("/health").json()["reviewer"]["ok"], False)
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/deepseek")

print("\n-- the proof-of-work solver's own rules --")
check("the prefix spells an integer as an integer",
      pow_solver.prefix_for("sa", 1789548668), "sa_1789548668_")
check("and a whole float the same way", pow_solver.prefix_for("sa", 1789548668.0), "sa_1789548668_")
check("an unknown algorithm is not guessed",
      pow_solver.solve({"algorithm": "SomethingElse", "challenge": "x", "difficulty": 1000,
                        "salt": "s", "expire_at": 1}), "")
check("an absurd difficulty is refused, not stalled",
      pow_solver.solve({"algorithm": "DeepSeekHashV1", "challenge": "x",
                        "difficulty": 999999999999, "salt": "s", "expire_at": 1}), "")
check("no challenge, no header", pow_solver.solve({}), "")

print("\n-- the userToken is rejected --")
STUB["users_code"] = 40003
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/api/v0", REVIEW_SHAPE="deepseek-web",
            DEEPSEEK_TOKEN="user-token-xyz")
health = client.get("/health").json()
check("the chip is red", health["reviewer"]["ok"], False)
check("and says how to get a new one", "userToken" in health["reviewer"]["detail"], True)
STUB["users_code"] = 0
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/deepseek", REVIEW_SHAPE="openai",
            DEEPSEEK_TOKEN="review-test-key")

print("\n-- pointing REVIEW_URL at the site picks the transport --")
reload_with(REVIEW_URL="https://chat.deepseek.com")
check("deepseek-web is chosen without being asked", server.REVIEW_SHAPE, "deepseek-web")
check("the transport is attached", server.REVIEWER.web is not None, True)
check("the reviewer is on", server.review_enabled(), True)
check("and labelled as the site", server.REVIEWER.web.label, "chat.deepseek.com")

print("\n-- a session token with no endpoint chosen goes to the site, not the API --")
reload_with(DEEPSEEK_TOKEN="user-token-xyz", REVIEW_URL=UNSET, REVIEW_SHAPE=UNSET)
check("the endpoint follows the credential", server.REVIEW_URL, "https://chat.deepseek.com")
check("and it was not asked for", server.REVIEW_URL_AUTO, True)
check("so the shape is the site's", server.REVIEW_SHAPE, "deepseek-web")
check("the transport is attached", server.REVIEWER.web is not None, True)
check("and the reviewer is on", server.review_enabled(), True)

print("\n-- REVIEW_URL left pointing at the API with a session token also goes to the site --")
reload_with(REVIEW_URL="https://api.deepseek.com", DEEPSEEK_TOKEN="user-token-xyz")
check("the API is not used with a session token", server.REVIEW_URL, "https://chat.deepseek.com")
check("and the site's shape comes with it", server.REVIEW_SHAPE, "deepseek-web")

print("\n-- a bridge someone set by hand is left alone --")
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/deepseek", REVIEW_SHAPE="openai",
            DEEPSEEK_TOKEN="user-token-xyz")
check("an endpoint that is not the API is respected", server.REVIEW_URL_AUTO, False)
check("and the endpoint is the one that was set", server.REVIEW_URL,
      f"http://127.0.0.1:{PORT}/deepseek")

print("\n-- an sk- key still means the API --")
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/deepseek", REVIEW_SHAPE="openai",
            DEEPSEEK_TOKEN="sk-real-api-key")
check("nothing was switched for an API key", server.REVIEW_URL_AUTO, False)
check("and the shape stays OpenAI", server.REVIEW_SHAPE, "openai")

print("\n-- a session token the API rejects is blamed on the endpoint, not the token --")
STUB["api_code"] = 401
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/api-reject", REVIEW_SHAPE="openai",
            DEEPSEEK_TOKEN="user-token-xyz")
out, done, calls, _ = turn()
review = "".join(f.get("t", "") for f in out if f.get("ch") == "review")
check("the draft still ships", DRAFT_CODE in (done.get("text") or ""), True)
check("the reason names the fix", "REVIEW_URL=https://chat.deepseek.com" in review, True)
check("and says which credential it is", "session token, not an API key" in review, True)
check("the api's own words are kept", "Authentication Fails" in review, True)
STUB["api_code"] = 0

print("\n-- nothing cuts a call off --")
check("the draft has no ceiling", server.CHAT_TIMEOUT, 0)
check("and neither does the reviewer", server.REVIEW_TIMEOUT, 0)
check("unless one is asked for", bridge.client_timeout(300).read, 300)
check("no ceiling means no ceiling on the answer", bridge.client_timeout(0).read, None)
check("a negative one means the same", bridge.client_timeout(-1).read, None)
check("connecting is still bounded, so a dead host is not a slow model",
      bridge.client_timeout(0).connect, 10.0)
check("a blocking caller waits for the whole negotiation", server.job_wait(), 0.0)
reload_with(CHAT_TIMEOUT="60")
check("and with a ceiling set it multiplies out over the rounds", server.job_wait(),
      60 * (2 + 2 * 5) + 30)
reload_with()
check("so the default is back to no ceiling", (server.CHAT_TIMEOUT, server.job_wait()), (0, 0.0))

print("\n-- a page whose stream keeps being cut collects the answer by polling --")
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/deepseek")
started = client.post("/chat/stream", json={"messages": [{"role": "user",
                                                          "content": "make me a walk script"}]})
polled = {}
for _ in range(400):
    polled = client.get(f"/chat/poll/{started.json()['job']}").json()
    if polled.get("done") or polled.get("status") == "error":
        break
    time.sleep(0.02)
check("the poll says the turn is over", polled.get("done"), True)
check("and carries the answer", polled.get("text"), MERGED2_CODE)
check("with the draft, both other versions and both reviews as well",
      [bool(polled.get("draft")), bool(polled.get("peer")), bool(polled.get("peer2")),
       bool(polled.get("review")), bool(polled.get("second_review"))],
      [True, True, True, True, True])
check("and the record of what each phase cost", len(polled.get("phases") or []), 9)
check("the poll reports no ceiling on the turn", "timeout" in polled, False)
check("an unknown job is a 404, so the page can stop",
      client.get("/chat/poll/nope").status_code, 404)

print("\n-- polling needs no key, exactly like the stream --")
reload_with(API_KEY="a-key-the-page-never-has")
started = client.post("/chat/stream", json={"messages": [{"role": "user",
                                                          "content": "make me a walk script"}]})
job = started.json()["job"]
check("the page can poll", client.get(f"/chat/poll/{job}").status_code, 200)
check("while the API result stays gated", client.get(f"/chat/result/{job}").status_code, 401)
reload_with()

print("\n-- the writer thinks, and the thinking is not the answer --")
reload_with()
out, done, calls, _ = turn()
wrote = writer_calls(calls)
check("thinking is on by default", bridge.QWEN_THINKING, "thinking")
check("and every writer call asks for it", [c["body"].get("thinking_mode") for c in wrote],
      ["thinking"] * len(wrote))
check("a reasoning delta never reaches the answer", REASONING in (done.get("text") or ""), False)
check("nor the draft the reader is shown", REASONING in (done.get("draft") or ""), False)
check("and the answer is still the merged script", done.get("text"), MERGED2_CODE)
reload_with(QWEN_THINKING="fast")
check("it can be turned off as well", bridge.QWEN_THINKING, "fast")
reload_with()

print("\n-- the offered scripts are read, and the one with the most lines is the choice --")
options = server.code_options(CHOICE_DRAFT)
check("both offered scripts are read as options", len(options), 2)
check("with their own line counts", [o["lines"] for o in options],
      [server.code_line_count(CHOICE_SHORT), server.code_line_count(CHOICE_LONG)])
check("and the one with the most lines is the pick",
      server.longest_option(options)["code"], CHOICE_LONG)
check("a single script is not a choice",
      server.code_options(fenced(CHOICE_LONG) + "\n\n" + CHOICE_QUESTION_TEXT), [])
check("and neither is an answer with no question in it", server.code_options(
      fenced(CHOICE_SHORT) + "\n\n" + fenced(CHOICE_LONG) + "\n\nNothing to choose here."), [])

print("\n-- \"Which choice do you prefer?\" is answered, not shipped --")
reload_with(NEGOTIATE_ROUNDS="0")
STUB["choice_draft"] = True
out, done, calls, _ = turn()
STUB["choice_draft"] = False
wrote = writer_calls(calls)
check("the writer offered two scripts and asked which", len(wrote), 2)
check("the first call is the draft", "make me a walk script" in asked_last(wrote[0]), True)
check("the question is answered in the same chat",
      [m["role"] for m in wrote[1]["body"]["messages"]][-2:], ["assistant", "user"])
check("with the question itself as the turn before it",
      CHOICE_QUESTION_TEXT in wrote[1]["body"]["messages"][-2]["content"], True)
check("the option named is the one with the most lines",
      "option 2 (the second one)" in asked_last(wrote[1]), True)
check("by the count that decided it",
      f"{server.code_line_count(CHOICE_LONG)} lines" in asked_last(wrote[1]), True)
check("and no question is asked back", "no questions" in asked_last(wrote[1]), True)
check("what ships is the chosen script", done.get("text"), CHOICE_LONG)
check("the question never reaches the reader",
      CHOICE_QUESTION_TEXT in (done.get("text") or ""), False)
check("and the chosen script is what the reader replays as the answer",
      streamed(out), CHOICE_LONG)
check("the phase says which one was taken", "choose" in [p["phase"] for p in done["phases"]], True)
check("and the turn is done", done.get("status"), "done")

print("\n-- a writer that will not decide still ends in a script --")
reload_with(NEGOTIATE_ROUNDS="0")
STUB["choice_draft"] = True
STUB["choice_dud"] = True
out, done, calls, _ = turn()
STUB["choice_dud"] = False
STUB["choice_draft"] = False
check("the longest option ships as it stands", done.get("text"), CHOICE_LONG)
check("nothing from the refusal is left in the answer",
      "cannot decide" in (done.get("text") or ""), False)
check("and the turn is still done", done.get("status"), "done")
reload_with()

print("\n-- a merge that asks which one is preferred is answered as well --")
STUB["choice_merge"] = True
out, done, calls, _ = turn()
STUB["choice_merge"] = False
phases = [p["phase"] for p in done["phases"]]
check("both readers ran and then the question was settled",
      phases_of(done), [["draft", "seed", "seed2"],
                        ["peer", "merge", "agree", "peer2", "merge", "agree2", "choose"]])
check("the writer was asked to choose after the merges", len(writer_calls(calls)), 4)
check("and the longest option is what ships", done.get("text"), CHOICE_LONG)
check("the merge's question is not shipped",
      CHOICE_QUESTION_TEXT in (done.get("text") or ""), False)

print("\n-- the choice handling can be turned off, and is clamped --")
reload_with(NEGOTIATE_ROUNDS="0", CHOICE_ROUNDS="0")
STUB["choice_draft"] = True
out, done, calls, _ = turn()
STUB["choice_draft"] = False
check("with it off the question is what ships", done.get("text"), CHOICE_DRAFT)
check("and nothing was asked back", len(writer_calls(calls)), 1)
reload_with(CHOICE_ROUNDS="9")
check("a value past the ceiling is clamped", server.CHOICE_ROUNDS, server.MAX_CHOICE_ROUNDS)
reload_with(CHOICE_ROUNDS="-1")
check("and a negative one means none", server.CHOICE_ROUNDS, 0)
reload_with()
check("the default is two", server.CHOICE_ROUNDS, 2)

print("\n-- the page shows the brief going out first --")
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/deepseek")
page = client.get("/")
check("the page renders", page.status_code, 200)
html = page.text
check("with every placeholder filled",
      [token for token in ("__CHIPS__", "__MODEL__", "__REVIEWER__", "__SECOND__", "__SECOND_ON__",
                           "__REVIEW_ON__", "__SEED_ON__", "__GREETING__") if token in html], [])
check("and each brief marked as going out on its own",
      ['data-seed="true"' in html, 'data-second-on="true"' in html, 'send2.txt' in html],
      [True, True, True])
reload_with()

print(f"\n{count[0]} checks, {len(failures)} failed")
if failures:
    print("  - " + "\n  - ".join(failures))
    sys.exit(1)
print("all checks passed")
