#!/bin/sh
# Entrypoint for Dockerfile.ollama: Ollama and the FastAPI app in one container.
# Ollama is bound to loopback because it is not the public surface — the API is,
# and it listens on $PORT so Railway's proxy can reach it on the service domain.
set -eu

MODEL="${MODEL:-qwen2.5-coder:3b}"
PORT="${PORT:-8000}"
OLLAMA_INTERNAL="127.0.0.1:11434"
OLLAMA_MODELS="${OLLAMA_MODELS:-/root/.ollama}"
export OLLAMA_HOST="$OLLAMA_INTERNAL" OLLAMA_MODELS
export OLLAMA_URL="${OLLAMA_URL:-http://$OLLAMA_INTERNAL}"

echo "[ollama] serving on ${OLLAMA_INTERNAL} (models: ${OLLAMA_MODELS})"
ollama serve &

# Pull the model in the background so the API answers immediately: /health and the
# status page report "pulling" until the weights are in the volume.
(
    i=0
    until OLLAMA_HOST="$OLLAMA_INTERNAL" ollama list >/dev/null 2>&1; do
        i=$((i + 1))
        [ "$i" -ge 60 ] && echo "[ollama] server not ready after 60s" >&2 && exit 0
        sleep 1
    done
    if OLLAMA_HOST="$OLLAMA_INTERNAL" ollama list 2>/dev/null | grep -q "^${MODEL}"; then
        echo "[ollama] ${MODEL} already in the volume"
    else
        echo "[ollama] pulling ${MODEL}"
        OLLAMA_HOST="$OLLAMA_INTERNAL" ollama pull "$MODEL" \
            || echo "[ollama] pull failed; /generate returns 503 until it succeeds" >&2
    fi
) &

echo "[api] uvicorn on 0.0.0.0:${PORT} (ollama: ${OLLAMA_URL})"
# exec so uvicorn is PID 1 and gets Railway's SIGTERM for a clean shutdown.
exec uvicorn server:app --host 0.0.0.0 --port "$PORT"
