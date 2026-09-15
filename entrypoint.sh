#!/bin/sh
# Serves the FastAPI app on $PORT. Qwen is called over HTTP (QWEN_TOKEN, QWEN_URL), so
# there is nothing to boot, pull or warm first.
set -eu

PORT="${PORT:-8000}"

echo "[entrypoint] api on 0.0.0.0:${PORT}"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT}"
