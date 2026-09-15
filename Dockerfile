# The `bahs` service: an OpenAI-compatible bridge to Qwen, plus one chat page.
#
#   Railway -> this service -> Settings -> Source  -> GitHub repo, branch main
#   Railway -> this service -> Settings -> Build   -> Dockerfile Path = Dockerfile (default)
#   Railway -> this service -> Settings -> Volumes -> none (it holds no state)
#   Railway -> this service -> Settings -> Variables -> QWEN_TOKEN (Qwen access token)
#                                                      API_KEY (optional, gates callers)
#
# There is no model in this image. Requests go to a qwen-api instance (QWEN_URL), which
# turns chat.qwen.ai into OpenAI-compatible endpoints, so there is no Ollama service, no
# GPU, no model volume and nothing to pull or warm. There is no database either: the
# conversation lives in the browser and is sent back with every turn.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY web ./web
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
