"""Check the chain against stubbed providers, including chat.deepseek.com's own endpoints.

Run from the project root:  .venv/bin/python verify_chain.py

What the chain is now: the reviewer is sent Send.txt on its own and its answer is waited for,
then the writer drafts, the reviewer writes its own version of that script, the writer merges
the two in the chat it drafted in, and the reviewer says whether it would ship the merge.
"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = []
STUB = {"users_code": 0, "api_code": 0, "bad_challenge": False, "always_better": False,
        "message_seq": 0}

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


def fenced(code):
    return "```lua\n" + code + "\n```"


SEED_ACK = "kanha:ready"
DRAFT = fenced(DRAFT_CODE)
PEER = "VERDICT: BETTER\n" + fenced(PEER_CODE)
MERGED = fenced(MERGED_CODE)
AGREE = "VERDICT: AGREE"
# The sentence that only a peer request carries, and the one only an agreement question carries.
PEER_ASK = "Write the version of this script you would ship"
VERIFY_ASK = "Would you ship this exactly as it is?"
SEED_ASK = "That is your standing instruction set"
MERGE_ASK = "and wrote its own version of it"
THINKING = "SECRET_THINKING_TEXT"


def frame(piece, finish=None, kind="RESPONSE"):
    delta = {"content": piece, "type": kind} if piece else {}
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def raw_frame(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def stream(text):
    return (frame(text) + frame(None, "stop") + "data: [DONE]\n\n").encode()


def reply_to(prompt):
    """What a provider answers, decided by what it was asked -- the ask is the whole contract."""
    if SEED_ASK in prompt:
        return SEED_ACK
    if PEER_ASK in prompt or (VERIFY_ASK in prompt and STUB["always_better"]):
        return PEER
    if VERIFY_ASK in prompt:
        return AGREE
    if MERGE_ASK in prompt:
        return MERGED
    return DRAFT


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
        return self._send(200, stream(reply_to(latest_ask(body))), "text/event-stream")


class StubServer(ThreadingHTTPServer):
    # Without this a handler thread sits in readline() on a pooled keep-alive connection and the
    # process never exits, which looks exactly like a hung test.
    daemon_threads = True
    allow_reuse_address = True


PORT = 8143
stub = StubServer(("127.0.0.1", PORT), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()

os.environ.update({
    "QWEN_URL": f"http://127.0.0.1:{PORT}/v1",
    "QWEN_TOKEN": "qwen-test-token",
    "REVIEW_URL": f"http://127.0.0.1:{PORT}/deepseek",
    "DEEPSEEK_TOKEN": "review-test-key",
    "HEARTBEAT": "0.2",
    # /health remembers a provider's answer for a minute so the page's polling does not turn into
    # a network call every 8 seconds; the test wants every check to be a fresh one.
    "TOKEN_CHECK_TTL": "0",
})
for name in ("REVIEW_SHAPE", "API_KEY", "DEEPSEEK_COOKIE", "SEED_BRIEF", "NEGOTIATE_ROUNDS"):
    os.environ.pop(name, None)

# How the test starts, so a section that sets a variable cannot leak it into the next one: every
# reload begins from this, not from whatever the section before it happened to leave behind.
BASE_ENV = {name: os.environ.get(name) for name in (
    "QWEN_URL", "QWEN_TOKEN", "REVIEW_URL", "DEEPSEEK_TOKEN", "REVIEW_SHAPE", "API_KEY",
    "SEED_BRIEF", "NEGOTIATE_ROUNDS", "CHAT_TIMEOUT", "TOKEN_CHECK_TTL")}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402
import base64  # noqa: E402
import pow_solver  # noqa: E402
import bridge  # noqa: E402
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
    # The config and the providers live in bridge, and server binds its names at import, so both
    # are reloaded -- otherwise a reloaded server would still hold the previous providers.
    importlib.reload(bridge)
    server = importlib.reload(server)
    client = TestClient(server.app)


def reviewer_calls(calls):
    """Only the reviewer's calls, in order, whichever shape its endpoint takes."""
    return [c for c in calls if c["body"].get("model") == "deepseek-v4-flash"
            or c["path"].endswith("/chat/completion")]


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
check("the draft and the brief are read together, then the negotiation",
      [sorted(phases[:2]), phases[2:]], [["draft", "seed"], ["peer", "merge", "agree"]])
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
check("the writer merged the two in the chat it drafted in",
      [p["phase"] for p in done["phases"]].count("merge"), 1)
check("the answer is the merged script", done.get("text"), MERGED_CODE)
check("the reviewer was asked whether it would ship the merge",
      "Would you ship this exactly as it is?" in asked_in(review[2]), True)
check("and its verdict is kept", done.get("review"), AGREE)
check("thinking is off in every reviewer call",
      [c["body"].get("thinking") for c in review], [{"type": "disabled"}] * 3)
check("no search parameter is ever sent",
      sorted(k for c in review for k in c["body"] if "search" in k.lower()), [])

print("\n-- the merge is a call, and it carries the other version --")
merge_calls = [c for c in calls if MERGE_ASK in content_of(c["body"])]
check("the merge happened once", len(merge_calls), 1)
check("the merged script is asked for in that same chat",
      merge_calls[0]["body"]["messages"][-2]["content"], DRAFT_CODE)
check("the other model's version is in the instruction",
      PEER_CODE in merge_calls[0]["body"]["messages"][-1]["content"], True)
check("the writer's own script was not resent as history",
      len([m for m in merge_calls[0]["body"]["messages"] if m["role"] == "user"]), 2)

print("\n-- a reviewer that never agrees is bounded by the rounds --")
STUB["always_better"] = True
out, done, calls, _ = turn()
phases = [p["phase"] for p in done["phases"]]
# Round one is the reviewer writing its own version; every round after it is an agreement
# question that came back with another version, and every one of them is followed by a merge.
check("five rounds: a version and a merge each, then it stops anyway",
      [sorted(phases[:2]), phases.count("peer"), phases.count("agree"), phases.count("merge")],
      [["draft", "seed"], 1, 4, 5])
check("the draft is not counted as a round",
      [p["phase"] for p in done["phases"]].count("draft"), 1)
check("and the last merged script is what ships", done.get("text"), MERGED_CODE)
check("the turn still ends", done.get("status"), "done")
check("no more calls than the rounds allow", len(done["phases"]), 12)
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
check("no seed phase", [p["phase"] for p in done["phases"]], ["draft", "peer", "merge", "agree"])
check("and one call fewer", len(done["phases"]), 4)
check("the brief is still in front of the first request",
      server.BRIEF[:60] in asked_in(reviewer_calls(calls)[0]), True)
check("and the warning is in front of the brief",
      asked_in(reviewer_calls(calls)[0]).startswith(server.REVIEW_WARNING), True)
check("and the contract rides with it",
      "VERDICT: BETTER" in asked_in(reviewer_calls(calls)[0]), True)
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
check("the merge still happened, in the writer's chat", done.get("text"), MERGED_CODE)
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
check("and the turn still ran", done.get("text"), MERGED_CODE)
STUB["bad_challenge"] = False

print("\n-- a reviewer that is down costs the draft, never the turn --")
STUB["users_code"] = 0
reload_with(REVIEW_URL="http://127.0.0.1:9/deepseek", REVIEW_SHAPE="openai",
            DEEPSEEK_TOKEN="review-test-key")
out, done, calls, _ = turn()
check("the draft still ships", done.get("text"), DRAFT_CODE)
check("the failure is recorded rather than silence", "did not happen" in (
    done.get("review") or ""), True)
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
check("and carries the answer", polled.get("text"), MERGED_CODE)
check("with the draft, the other version and the review as well",
      [bool(polled.get("draft")), bool(polled.get("peer")), bool(polled.get("review"))],
      [True, True, True])
check("and the record of what each phase cost", len(polled.get("phases") or []), 5)
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

print("\n-- the page shows the brief going out first --")
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/deepseek")
page = client.get("/")
check("the page renders", page.status_code, 200)
html = page.text
check("with every placeholder filled",
      [token for token in ("__CHIPS__", "__MODEL__", "__REVIEWER__", "__REVIEW_ON__",
                           "__SEED_ON__", "__GREETING__") if token in html], [])
check("and the brief marked as going out on its own", 'data-seed="true"' in html, True)
reload_with()

print(f"\n{count[0]} checks, {len(failures)} failed")
if failures:
    print("  - " + "\n  - ".join(failures))
    sys.exit(1)
print("all checks passed")
