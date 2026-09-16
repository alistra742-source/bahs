# The `bahs` service: one model and a toolbox, behind one API, with a chat page on top.
#
#   Railway -> this service -> Settings -> Source  -> GitHub repo, branch main
#   Railway -> this service -> Settings -> Build   -> Dockerfile Path = Dockerfile (default)
#   Railway -> this service -> Settings -> Volumes -> none (it holds no state)
#   Railway -> this service -> Settings -> Variables -> QWEN_TOKEN (Qwen access token)
#                                                      API_KEY (optional, gates the API)
#
# There is no model in this image. Every call goes to a qwen-api instance (QWEN_URL), which turns
# chat.qwen.ai into OpenAI-compatible endpoints; thinking is on, and the reasoning is dropped, so
# what comes back is the script. So there is no Ollama service, no GPU, no model volume and
# nothing to pull or warm. There is no database either: the conversation lives in the browser (or
# in client.lua) and is sent back with every turn.
#
# The conversation is one chat, not one per question: qwen-api continues the upstream chat when a
# request carries the hidden `<!-- qwen_metadata: ... -->` it put in the last answer, so the
# service cuts that marker out of everything it streams, keeps it per session, and puts it back on
# the next request for that session.
#
# The tools the model calls on itself are all local to this image (luau.py): a structural check of
# the script, the real Roblox API dump (fetched at runtime, not baked in), a targeted edit, a
# credential scan, a re-indent, and -- when a Roblox client is polling -- running the script in
# the executor it is injected into and reading the traceback back.
#
# Four Python modules: bridge.py (the token, the config, one request and its stream, and the
# session that keeps one chat), state.py (the health checks and the limits), luau.py (the toolbox,
# including the queue the executor polls) and server.py, which is the service on top: the turn,
# the tool rounds, the endpoints and the page.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY bridge.py ./
COPY state.py ./
COPY luau.py ./
COPY web ./web
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
