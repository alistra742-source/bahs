#!/bin/sh
# Entrypoint for the `ollama` service (Dockerfile.ollama): serves Ollama on
# 0.0.0.0:11434 and keeps $MODEL pulled. The `bahs` API service talks to it over
# private networking at http://ollama.railway.internal:11434.
set -eu

MODEL="${MODEL:-qwen2.5-coder:3b}"
OLLAMA_HOST="${OLLAMA_HOST:-0.0.0.0:11434}"
OLLAMA_MODELS="${OLLAMA_MODELS:-/root/.ollama}"
export OLLAMA_HOST OLLAMA_MODELS

echo "[ollama] serving on ${OLLAMA_HOST} (models: ${OLLAMA_MODELS})"
ollama serve &
server_pid=$!

# Pull the model in the background: the service is reachable immediately and the
# API reports "pulling" through /health until the weights are in the volume.
(
    i=0
    until OLLAMA_HOST=127.0.0.1:11434 ollama list >/dev/null 2>&1; do
        i=$((i + 1))
        [ "$i" -ge 60 ] && echo "[ollama] server not ready after 60s" >&2 && exit 0
        sleep 1
    done
    if OLLAMA_HOST=127.0.0.1:11434 ollama list 2>/dev/null | grep -q "^${MODEL}"; then
        echo "[ollama] ${MODEL} already in the volume"
    else
        echo "[ollama] pulling ${MODEL}"
        OLLAMA_HOST=127.0.0.1:11434 ollama pull "$MODEL" \
            || echo "[ollama] pull failed; /generate returns 503 until it succeeds" >&2
    fi
) &

wait "$server_pid"
