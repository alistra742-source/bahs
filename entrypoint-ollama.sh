#!/bin/sh
# Standalone Ollama server: serves the API and keeps $MODEL pulled in the
# background. Used with Dockerfile.ollama.
set -eu

MODEL="${MODEL:-qwen2.5-coder:3b}"
OLLAMA_HOST="${OLLAMA_HOST:-0.0.0.0:11434}"
OLLAMA_MODELS="${OLLAMA_MODELS:-/data/ollama}"
export OLLAMA_HOST OLLAMA_MODELS

echo "[ollama] serving on ${OLLAMA_HOST} (models: ${OLLAMA_MODELS})"
ollama serve &

server_pid=$!

i=0
until OLLAMA_HOST=127.0.0.1:11434 ollama list >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -ge 60 ] && echo "[ollama] server not ready after 60s" >&2 && break
    sleep 1
done

if ! OLLAMA_HOST=127.0.0.1:11434 ollama list 2>/dev/null | grep -q "^${MODEL}"; then
    echo "[ollama] pulling ${MODEL}"
    OLLAMA_HOST=127.0.0.1:11434 ollama pull "${MODEL}" || echo "[ollama] pull failed, will retry on demand" >&2
fi

wait "$server_pid"
