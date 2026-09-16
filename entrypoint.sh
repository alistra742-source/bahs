#!/bin/sh
# Serves the FastAPI app on $PORT. The model is called over HTTP (QWEN_TOKEN + QWEN_URL), so there
# is nothing to boot, pull or warm first -- and the toolbox (luau.py) runs in this process.
set -eu

PORT="${PORT:-8000}"

echo "[entrypoint] api on 0.0.0.0:${PORT}"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT}"
