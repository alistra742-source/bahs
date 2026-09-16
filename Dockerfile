# The `bahs` service: a two-model bridge, plus one chat page.
#
#   Railway -> this service -> Settings -> Source  -> GitHub repo, branch main
#   Railway -> this service -> Settings -> Build   -> Dockerfile Path = Dockerfile (default)
#   Railway -> this service -> Settings -> Volumes -> none (it holds no state)
#   Railway -> this service -> Settings -> Variables -> QWEN_TOKEN (Qwen access token)
#                                                      REVIEW_KEY (DeepSeek key)
#                                                      API_KEY (optional, gates the API)
#
# There is no model in this image. Drafts go to a qwen-api instance (QWEN_URL), which turns
# chat.qwen.ai into OpenAI-compatible endpoints; the review goes to DeepSeek (REVIEW_URL,
# api.deepseek.com by default) with thinking off. So there is no Ollama service, no GPU, no
# model volume and nothing to pull or warm. There is no database either: the conversation
# lives in the browser and is sent back with every turn.
#
# The brief handed to the reviewer before anything else is part of the image. There are two in
# the repo (send.txt and Send.txt, differing only in case); send.txt is the newer and the one
# used, and Send.txt is the fallback. A missing one is not fatal -- the built-in rubric still
# applies -- but which one was used is reported on /health so it is visible rather than silent.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY web ./web
COPY send.txt ./
COPY Send.txt ./
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
