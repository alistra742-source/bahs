FROM ollama/ollama:latest

# The base image is Ubuntu based and sets ENTRYPOINT ["/bin/ollama"] CMD ["serve"].
# Anything we put in CMD is therefore passed to the ollama binary as arguments
# (ollama would receive "/bin/sh" and exit with `unknown command`), so we must
# clear the inherited entrypoint and start an explicit shell command.
ENV DEBIAN_FRONTEND=noninteractive \
    OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/data/ollama \
    MODEL=qwen2.5-coder:3b \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/venv/bin:$PATH"

COPY server.py .
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8000 11434

ENTRYPOINT []
# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
