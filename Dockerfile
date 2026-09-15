FROM ollama/ollama:latest

RUN apt-get update && apt-get install -y python3 python3-pip python3-venv && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 11434 8000

CMD ollama serve & \
    sleep 10 && \
    ollama pull qwen2.5-coder:3b && \
    uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
