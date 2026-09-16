"""Check the reviewer against stubbed providers, including chat.deepseek.com's own endpoints.

Run from the project root:  .venv/bin/python verify_chain.py
"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = []
STUB = {"users_code": 0, "api_code": 0}   # what /users/current and a rejected API key answer
DRAFT = "```lua\n-- draft\nprint('hi')\n```"
REVIEW = ("VERDICT: ISSUES\n"
          "1. the loop never ends | where: line 4 | why it fails: it runs forever | fix: add a break")
REFINED = "```lua\n-- refined\nprint('hi')\n```"
WEB_HEAD = "VERDICT: ISSUES\n"
WEB_LIST = ("1. the loop never ends | where: line 4 | why it fails: it runs forever | "
            "fix: add a break")
THINKING = "SECRET_THINKING_TEXT"


def frame(piece, finish=None, kind="RESPONSE"):
    delta = {"content": piece, "type": kind} if piece else {}
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def raw_frame(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def stream(text):
    return (frame(text) + frame(None, "stop") + "data: [DONE]\n\n").encode()


def ds_stream():
    """The review split across both frame shapes the site has used.

    A thinking frame and a status frame ride along, and neither may reach the review text.
    """
    return "".join([
        frame(WEB_HEAD),
        frame(THINKING, kind="thinking"),
        raw_frame({"v": WEB_LIST}),
        raw_frame({"p": "response/status", "v": "FINISHED"}),
        "data: [DONE]\n\n",
    ]).encode()


def content_of(body):
    return "\n".join(m["content"] for m in body.get("messages", [])
                     if isinstance(m.get("content"), str))


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
            return self._send(200, {"code": 0, "data": {"biz_data": {"challenge": {
                "algorithm": "DeepSeekHashV1", "challenge": "ch", "salt": "sa",
                "difficulty": 144000, "expire_at": 1, "signature": "sig",
                "target_path": "/api/v0/chat/completion"}}}})
        if self.path.endswith("/chat/completion"):
            return self._send(200, ds_stream(), "text/event-stream")
        if body.get("model") == "deepseek-v4-flash":
            return self._send(200, stream(REVIEW), "text/event-stream")
        if "A reviewer checked the script you just wrote" in content_of(body):
            return self._send(200, stream(REFINED), "text/event-stream")
        return self._send(200, stream(DRAFT), "text/event-stream")


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
for name in ("REVIEW_SHAPE", "API_KEY", "DEEPSEEK_COOKIE"):
    os.environ.pop(name, None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)
failures, count = [], [0]


def check(name, got, want):
    count[0] += 1
    if got != want:
        failures.append(name)
        print(f"  FAIL {name}: got {got!r}, wanted {want!r}")
    else:
        print(f"  ok   {name}")


def turn(question):
    CALLS.clear()
    start = client.post("/chat/stream", json={"messages": [{"role": "user", "content": question}]})
    out = []
    with client.stream("GET", f"/chat/stream/{start.json()['job']}") as r:
        for line in r.iter_lines():
            if line.strip():
                out.append(json.loads(line))
    done = [f for f in out if f.get("done")]
    return out, (done[0] if done else {}), CALLS[:]


def reload_with(**env):
    global server, client
    os.environ.update(env)
    server = importlib.reload(server)
    client = TestClient(server.app)


print("\n-- the chain, reviewer on an OpenAI-shaped endpoint --")
out, done, calls = turn("make me a walk script")
reviewer = [c for c in calls if c["body"].get("model") == "deepseek-v4-flash"]
check("three phases", [p["phase"] for p in done["phases"]], ["draft", "review", "refine"])
check("the answer is the rewrite", done.get("text"), "-- refined\nprint('hi')")
check("the brief is first in the review request",
      reviewer[0]["body"]["messages"][0]["content"].startswith(server.BRIEF[:60]), True)
check("thinking is off", reviewer[0]["body"].get("thinking"), {"type": "disabled"})
check("no search parameter", [k for k in reviewer[0]["body"] if "search" in k.lower()], [])
check("the brief is send.txt", server.BRIEF_PATH.name, "send.txt")

print("\n-- chat.deepseek.com, driven by the userToken --")
reload_with(REVIEW_URL=f"http://127.0.0.1:{PORT}/api/v0", REVIEW_SHAPE="deepseek-web",
            DEEPSEEK_TOKEN="user-token-xyz")
check("the web transport is in use", server.REVIEWER.web is not None, True)
check("and the reviewer is on", server.review_enabled(), True)
out, done, calls = turn("make me a walk script")
web = [c for c in calls if c["path"].endswith("/api/v0/chat/completion")]
check("the site's completion endpoint was used", len(web), 1)
check("a session was created for the review",
      len([c for c in calls if c["path"].endswith("/chat_session/create")]), 1)
check("the challenge was asked for",
      len([c for c in calls if c["path"].endswith("/chat/create_pow_challenge")]), 1)
check("no proof of work header was invented", "x-ds-pow-response" in web[0]["headers"], False)
check("the userToken is the bearer", web[0]["headers"].get("authorization"),
      "Bearer user-token-xyz")
check("thinking is switched off in the payload", web[0]["body"].get("thinking_enabled"), False)
check("search is switched off in the payload", web[0]["body"].get("search_enabled"), False)
check("the attempt looks like the site's", web[0]["headers"].get("x-client-platform"), "web")
check("the brief leads the prompt",
      web[0]["body"]["prompt"].startswith(server.BRIEF[:60]), True)
check("the request is in the prompt", "make me a walk script" in web[0]["body"]["prompt"], True)
check("both frame shapes were read",
      [WEB_HEAD in done["review"], WEB_LIST in done["review"]], [True, True])
check("a thinking frame never reaches the review", THINKING in done["review"], False)
check("a status frame never arrives as text", "FINISHED" in done["review"], False)
check("the rewrite still ran in the same chat", done.get("text"), "-- refined\nprint('hi')")
health = client.get("/health").json()
check("the chip says the token works", health["reviewer"]["detail"], "token accepted")
check("and names the shape", health["reviewer"]["shape"], "deepseek-web")

print("\n-- the userToken is rejected --")
STUB["users_code"] = 40003
health = client.get("/health").json()
check("the chip is red", health["reviewer"]["ok"], False)
check("and says how to get a new one", "userToken" in health["reviewer"]["detail"], True)
STUB["users_code"] = 0

print("\n-- pointing REVIEW_URL at the site picks the transport --")
reload_with(REVIEW_URL="https://chat.deepseek.com")
check("deepseek-web is chosen without being asked", server.REVIEW_SHAPE, "deepseek-web")
check("the transport is attached", server.REVIEWER.web is not None, True)
check("the reviewer is on", server.review_enabled(), True)
check("and labelled as the site", server.REVIEWER.web.label, "chat.deepseek.com")

print("\n-- a session token with no endpoint chosen goes to the site, not the API --")
os.environ.pop("REVIEW_URL", None)
os.environ.pop("REVIEW_SHAPE", None)
reload_with(DEEPSEEK_TOKEN="user-token-xyz")
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
out, done, calls = turn("make me a walk script")
review = "".join(f.get("t", "") for f in out if f.get("ch") == "review")
check("the draft still ships, unrefined", "-- draft" in (done.get("text") or ""), True)
check("the reason names the fix",
      "REVIEW_URL=https://chat.deepseek.com" in review, True)
check("and says which credential it is", "session token, not an API key" in review, True)
check("the api's own words are kept", "Authentication Fails" in review, True)
STUB["api_code"] = 0

print(f"\n{count[0]} checks, {len(failures)} failed")
if failures:
    print("  - " + "\n  - ".join(failures))
    sys.exit(1)
print("all checks passed")
