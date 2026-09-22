# The `bahs` service: two models, three modes and a toolbox, behind one API, with a chat page on
# top.
#
#   Railway -> this service -> Settings -> Source  -> GitHub repo, branch main
#   Railway -> this service -> Settings -> Build   -> Dockerfile Path = Dockerfile (default)
#   Railway -> this service -> Settings -> Volumes -> none (it holds no state)
#   Railway -> this service -> Settings -> Variables -> QWEN_TOKEN (Qwen access token)
#                                                      DEEPSEEK_TOKEN (optional: enables the
#                                                        agent and deepseek modes)
#                                                      API_KEY (optional, gates the API)
#
# There is no model in this image. Every call goes to a qwen-api instance (QWEN_URL), which turns
# chat.qwen.ai into OpenAI-compatible endpoints; thinking is on, and the reasoning is kept out of
# the answer -- it goes to the caller's `thoughts` channel instead -- so what comes back is the
# script. DeepSeek is the second model, called only in the "agent" and
# "deepseek" modes: either its OpenAI-shaped API (an `sk-...` key) or chat.deepseek.com itself
# (the site's userToken, which needs the proof of work solved -- see pow_solver.py). So there is no
# Ollama service, no GPU, no model volume and nothing to pull or warm. There is no database
# either: the conversation lives in the browser (or in client.lua) and is sent back every turn.
#
# The conversation is one chat, not one per question, on both sides: qwen-api continues the
# upstream chat when a request carries the hidden `<!-- qwen_metadata: ... -->` it put in the last
# answer, and chat.deepseek.com threads a chat by the id of the message before this one. So the
# service cuts that marker out of everything it streams, keeps it (and the site's message id) per
# session, and puts it back on the next request for that session.
#
# The tools the model calls on itself are all local to this image (luau.py): a structural check of
# the script, the real Roblox API dump (fetched at runtime, not baked in), a targeted edit, a
# credential scan, a re-indent, and -- when a Roblox client is polling -- running the script in
# the executor it is injected into and reading the traceback back.
#
# Six Python modules, and all six are copied below: bridge.py (the tokens, the config, one request
# and its stream, the two transports, and the sessions that keep one chat each), thoughts.py (the
# same writer's stream with its chain of thought kept, for a client's thinking pane), state.py (the
# health checks and the limits), luau.py (the toolbox, including the queue the executor polls),
# pow_solver.py (the proof of work chat.deepseek.com asks for) and server.py, which is the service
# on top: the mode, the turn, the tool rounds, the endpoints and the page.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kanha's complete DeepSeek system prompt must be present at runtime.
COPY send.txt ./send.txt
COPY server.py ./
COPY bridge.py ./
COPY state.py ./
COPY luau.py ./
COPY pow_solver.py ./
COPY thoughts.py ./
COPY web ./web
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Every module the app imports has to be in the image. One that is not is invisible at build time
# and fatal at run time: uvicorn dies on the import error, nothing is listening, and every request
# gets a 502 from the platform with a body that names no reason at all -- which reads like the
# service refusing rather than the service never having started. This is that check.
RUN python -c "import server; print('modules present,', len(server.app.routes), 'routes')"

# The sha3 module chat.deepseek.com loads (26 KB of wasm), so the proof of work can be solved
# without fetching it at runtime. `|| true`: the service runs without it (and says so) for a
# deployment that never calls DeepSeek, so a build must not fail over it.
RUN python pow_solver.py || true

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
