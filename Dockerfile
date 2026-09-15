# API service for the `bahs` project.
#
# Ollama runs in its own service (see Dockerfile.ollama) and is reached through
# OLLAMA_URL, while scripts and feedback live in Railway Postgres (DATABASE_URL).
# This container therefore holds no state and needs no volume.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
