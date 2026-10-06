FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT; default matches Railway's own fallback for local runs.
ENV PORT=8080
EXPOSE 8080

# Bind 0.0.0.0 so the platform proxy can reach the process. Exec form via sh
# so $PORT is expanded at runtime.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]
