#!/bin/sh
# Starts the bundled Ollama server, then serves the FastAPI app on $PORT.
# The model download runs in the background so the API can answer as soon as
# uvicorn is up (Railway healthchecks would otherwise time out on a cold volume).
set -eu

MODEL="${MODEL:-qwen2.5-coder:3b}"
PORT="${PORT:-8000}"
OLLAMA_HOST="${OLLAMA_HOST:-0.0.0.0:11434}"
# The generated models live on a volume so they survive redeploys.
OLLAMA_MODELS="${OLLAMA_MODELS:-/data/ollama}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
export OLLAMA_HOST OLLAMA_MODELS

if [ "${START_OLLAMA:-1}" = "1" ]; then
    echo "[entrypoint] starting ollama on ${OLLAMA_HOST} (models: ${OLLAMA_MODELS})"
    ollama serve >/tmp/ollama.log 2>&1 &

    i=0
    # `ollama list` talks to the server, so it doubles as the readiness probe.
    until OLLAMA_HOST=127.0.0.1:11434 ollama list >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 60 ]; then
            echo "[entrypoint] ollama not ready after 60s, starting API anyway" >&2
            cat /tmp/ollama.log >&2 || true
            break
        fi
        sleep 1
    done

    if OLLAMA_HOST=127.0.0.1:11434 ollama list 2>/dev/null | grep -q "^${MODEL}"; then
        echo "[entrypoint] model ${MODEL} already present"
    else
        echo "[entrypoint] pulling ${MODEL} in the background"
        (OLLAMA_HOST=127.0.0.1:11434 ollama pull "${MODEL}" >/tmp/ollama-pull.log 2>&1 &)
    fi
fi

echo "[entrypoint] starting api on 0.0.0.0:${PORT}"
exec uvicorn server:app --host 0.0.0.0 --port "${PORT}"
