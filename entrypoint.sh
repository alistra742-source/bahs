#!/bin/sh
# Serves the FastAPI app on $PORT. The model runs on Hugging Face's inference router
# (HF_API) and the database is Postgres (DATABASE_URL), so there is nothing to boot first.
set -eu

PORT="${PORT:-8000}"

echo "[entrypoint] api on 0.0.0.0:${PORT}"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT}"
