# The `bahs` service: a two-model bridge, plus one chat page.
#
#   Railway -> this service -> Settings -> Source  -> GitHub repo, branch main
#   Railway -> this service -> Settings -> Build   -> Dockerfile Path = Dockerfile (default)
#   Railway -> this service -> Settings -> Volumes -> none (it holds no state)
#   Railway -> this service -> Settings -> Variables -> QWEN_TOKEN (Qwen access token)
#                                                      DEEPSEEK_TOKEN (userToken or sk- key)
#                                                      API_KEY (optional, gates the API)
#
# There is no model in this image. Drafts go to a qwen-api instance (QWEN_URL), which turns
# chat.qwen.ai into OpenAI-compatible endpoints; the review goes to DeepSeek with thinking and
# search off, through api.deepseek.com for an `sk-...` key or straight through
# chat.deepseek.com for a `userToken`. That second path needs the proof of work the site asks
# for on every message, which is solved with the site's own sha3 module (pow_solver.py, fetched
# below). So there is no Ollama service, no GPU, no model volume and nothing to pull or warm.
# There is no database either: the conversation lives in the browser and is sent back with
# every turn.
#
# The brief handed to the reviewer before anything else is part of the image. It goes out on its
# own first -- that message is the brief and nothing else -- and the request for a script only
# follows once the reviewer has answered it. Every message to the reviewer opens with the warning
# that the target is a Roblox executor script and not Roblox Studio (REVIEW_WARNING), because
# that is the one assumption that would make its advice wrong. There
# are two briefs in the repo (send.txt and Send.txt, differing only in case); send.txt is the
# newer and the one used, and Send.txt is the fallback. A missing one is not fatal -- the built-in
# rubric still applies -- but which one was used is reported on /health so it is visible rather
# than silent.
#
# Two Python modules: bridge.py is everything that talks to a provider (tokens, config, the two
# transports, the brief, the job record) and server.py is the service on top of it (the chain, the
# endpoints, the page).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY bridge.py ./
COPY pow_solver.py ./
COPY web ./web
COPY send.txt ./
COPY Send.txt ./
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# The sha3 module chat.deepseek.com loads (26 KB of wasm), so the proof of work can be solved from
# the first review instead of after a fetch. A build without network does not fail: the service
# fetches the same copy on first use and says so in its log.
RUN python pow_solver.py || true

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
