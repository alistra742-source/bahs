#!/bin/sh
# Serves the FastAPI app on $PORT. Ollama is a separate service (OLLAMA_URL) and
# the database is Postgres (DATABASE_URL), so there is nothing to boot first.
set -eu

PORT="${PORT:-8000}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
export OLLAMA_URL

echo "[entrypoint] api on 0.0.0.0:${PORT} (ollama: ${OLLAMA_URL})"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT}"
