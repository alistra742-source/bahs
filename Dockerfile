# The `bahs` service: one writer and two readers over one script, plus one chat page.
#
#   Railway -> this service -> Settings -> Source  -> GitHub repo, branch main
#   Railway -> this service -> Settings -> Build   -> Dockerfile Path = Dockerfile (default)
#   Railway -> this service -> Settings -> Volumes -> none (it holds no state)
#   Railway -> this service -> Settings -> Variables -> QWEN_TOKEN (Qwen access token)
#                                                      DEEPSEEK_TOKEN (userToken or sk- key)
#                                                      ZAI_TOKEN (z.ai API key, for GLM)
#                                                      API_KEY (optional, gates the API)
#
# There is no model in this image. Drafts go to a qwen-api instance (QWEN_URL), which turns
# chat.qwen.ai into OpenAI-compatible endpoints; the first reader is DeepSeek with thinking and
# search off, through api.deepseek.com for an `sk-...` key or straight through
# chat.deepseek.com for a `userToken` (that path needs the proof of work the site asks for on
# every message, solved with the site's own sha3 module, pow_solver.py, fetched below); and the
# second reader is GLM-5.3 Flash through z.ai's platform API with deep think at its strongest
# setting. So there is no Ollama service, no GPU, no model volume and nothing to pull or warm.
# There is no database either: the conversation lives in the browser and is sent back with
# every turn.
#
# The briefs handed to the readers before anything else are part of the image. Each goes out on
# its own first -- that message is the brief and nothing else -- and the request for a script only
# follows once that reader has answered it. Every message to either reader opens with the warning
# that the target is a Roblox executor script and not Roblox Studio (REVIEW_WARNING), because
# that is the one assumption that would make its advice wrong.
#
# send.txt is the first reader's brief; there are two files in the repo (send.txt and Send.txt,
# differing only in case), send.txt is the newer and the one used, and Send.txt is the fallback.
# send2.txt is the second reader's, and it does not have to exist: without it that reader is sent
# send.txt and /health says so. Both are read at boot, so editing either one on GitHub takes
# effect on the next deploy.
#
# Four Python modules: bridge.py (tokens, config, both transports, the briefs, a request and its
# stream), state.py (the health checks and the limits), peers.py (the second reader and its brief)
# and server.py, which is the service on top: the chain, the endpoints and the page.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY bridge.py ./
COPY state.py ./
COPY peers.py ./
COPY pow_solver.py ./
COPY web ./web
# The glob matches send.txt alone, or send.txt and send2.txt once you add the second reader's
# brief -- the build never depends on which of the two the second reader got.
COPY send*.txt Send.txt ./
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# The sha3 module chat.deepseek.com loads (26 KB of wasm), so the proof of work can be solved from
# the first review instead of after a fetch. A build without network does not fail: the service
# fetches the same copy on first use and says so in its log.
RUN python pow_solver.py || true

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
