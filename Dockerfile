# API service for the `bahs` project — this is the `bahs` service.
#
#   Railway -> this service -> Settings -> Source  -> GitHub repo, branch main
#   Railway -> this service -> Settings -> Build   -> Dockerfile Path = Dockerfile (default)
#   Railway -> this service -> Settings -> Volumes -> none (it holds no state)
#   Railway -> this service -> Settings -> Variables -> DATABASE_URL (reference Postgres)
#
# Ollama runs as a separate service deployed from the stock `ollama/ollama` image with
# a volume at /root/.ollama, reached over Railway private networking. This API pulls
# MODEL into it on startup. Scripts and feedback live in Railway Postgres.
#
# Set INFERENCE_URL + INFERENCE_KEY (+ INFERENCE_MODEL) to answer from a hosted
# OpenAI-compatible model instead; the image then never touches Ollama, so the ollama
# service is optional. See "Hosted inference" in README.md.
FROM python:3.12-slim

# OLLAMA_URL points at the `ollama` service's private hostname by default, so the API
# works with no variables set. Override it only if that service is named differently.
ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    OLLAMA_URL=http://ollama.railway.internal:11434

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY web ./web
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
