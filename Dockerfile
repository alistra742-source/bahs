FROM ollama/ollama:latest

RUN apt-get update && apt-get install -y python3 python3-pip python3-venv && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN ollama pull qwen2.5-coder:3b

EXPOSE 11434 8000

CMD ollama serve & sleep 5 && uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
