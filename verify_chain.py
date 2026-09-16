"""Check the chain and the chat.deepseek.com guard against stubbed providers.

Run from the project root:  .venv/bin/python verify_chain.py
"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALLS = []
DRAFT = "```lua\n-- draft\nprint('hi')\n```"
REVIEW = ("VERDICT: ISSUES\n"
          "1. the loop never ends | where: line 4 | why it fails: it runs forever | fix: add a break")
REFINED = "```lua\n-- refined\nprint('hi')\n```"


def frame(piece, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": {"content": piece} if piece else {},
                                                "finish_reason": finish}]}) + "\n\n"


def stream(text):
    return (frame(text) + frame(None, "stop") + "data: [DONE]\n\n").encode()


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
        return self._send(404, {"error": {"message": "no such route"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.endswith("/validate"):
            return self._send(200, {"valid": True})
        CALLS.append({"path": self.path, "body": body})
        if body.get("model") == "deepseek-v4-flash":
            return self._send(200, stream(REVIEW), "text/event-stream")
        if "A reviewer checked the script you just wrote" in content_of(body):
            return self._send(200, stream(REFINED), "text/event-stream")
        return self._send(200, stream(DRAFT), "text/event-stream")


class StubServer(ThreadingHTTPServer):
    # Without this a handler thread sits in readline() on a pooled keep-alive connection and
    # the process never exits, which looks exactly like a hung test.
    daemon_threads = True
    allow_reuse_address = True


PORT = 8141
stub = StubServer(("127.0.0.1", PORT), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()

os.environ.update({
    "QWEN_URL": f"http://127.0.0.1:{PORT}/v1",
    "QWEN_TOKEN": "qwen-test-token",
    "REVIEW_URL": f"http://127.0.0.1:{PORT}/deepseek",
    "DEEPSEEK_TOKEN": "review-test-key",
    "HEARTBEAT": "0.2",
})
os.environ.pop("API_KEY", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


print("\n-- the chain still runs --")
out, done, calls = turn("make me a walk script")
reviewer = [c for c in calls if c["body"].get("model") == "deepseek-v4-flash"]
check("three phases", [p["phase"] for p in done["phases"]], ["draft", "review", "refine"])
check("the answer is the rewrite", done.get("text"), "-- refined\nprint('hi')")
check("the brief is first in the review request",
      reviewer[0]["body"]["messages"][0]["content"].startswith(server.BRIEF[:60]), True)
check("thinking is off", reviewer[0]["body"].get("thinking"), {"type": "disabled"})
check("no search parameter", [k for k in reviewer[0]["body"] if "search" in k.lower()], [])
check("the brief is send.txt", server.BRIEF_PATH.name, "send.txt")

print("\n-- pointed at chat.deepseek.com itself --")
os.environ["REVIEW_URL"] = "https://chat.deepseek.com"
import importlib  # noqa: E402
server = importlib.reload(server)
client = TestClient(server.app)
check("the chain turns itself off", server.review_enabled(), False)
out, done, calls = turn("solo please")
check("no reviewer call is spent", [c for c in calls if "deepseek" in c["path"]], [])
check("the draft is the answer", done.get("text"), "-- draft\nprint('hi')")
health = client.get("/health").json()
check("the chip is red", health["reviewer"]["ok"], False)
check("and says why", "proof of work" in health["reviewer"]["detail"], True)
check("health says the review is off", health["review"], False)
check("the page still renders", "__CHIPS__" in client.get("/").text, False)
check("no leftover __REVIEWER__", "__REVIEWER__" in client.get("/").text, False)

print(f"\n{count[0]} checks, {len(failures)} failed")
if failures:
    print("  - " + "\n  - ".join(failures))
    sys.exit(1)
print("all checks passed")
