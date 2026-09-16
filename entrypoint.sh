#!/bin/sh
# Serves the FastAPI app on $PORT. All three models are called over HTTP (QWEN_TOKEN + QWEN_URL
# for the draft, REVIEW_KEY + REVIEW_URL for the first reader, ZAI_TOKEN + ZAI_URL for the
# second), so there is nothing to boot, pull or warm first.
set -eu

PORT="${PORT:-8000}"

echo "[entrypoint] api on 0.0.0.0:${PORT}"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT}"
