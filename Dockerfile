FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# tor: the fallback pool for an account that has pasted no proxies (see tor.py
# and the Tor section of README.md). A client only -- the service writes its own
# torrc with ClientOnly, no control port, and every SocksPort bound to loopback.
# `--no-install-recommends` keeps it to what the binary actually needs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tor \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# Railway injects PORT; default matches Railway's own fallback for local runs.
ENV PORT=8080
EXPOSE 8080

# Bind 0.0.0.0 so the platform proxy can reach the process. Exec form via sh
# so $PORT is expanded at runtime.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --no-proxy-headers --port ${PORT:-8080}"]
