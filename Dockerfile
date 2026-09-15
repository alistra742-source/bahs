# API service for the `bahs` project — this is the `bahs` service.
#
#   Railway -> this service -> Settings -> Source  -> GitHub repo, branch main
#   Railway -> this service -> Settings -> Build   -> Dockerfile Path = Dockerfile (default)
#   Railway -> this service -> Settings -> Volumes -> none (it holds no state)
#   Railway -> this service -> Settings -> Variables -> DATABASE_URL (reference Postgres)
#                                                      HF_API (Hugging Face token)
#
# There is no model in this image. Generations go to Hugging Face's Inference Providers
# router (INFERENCE_URL overrides the endpoint, INFERENCE_MODEL the model), so there is
# no Ollama service, no model volume and nothing to pull. Scripts and feedback live in
# Railway Postgres.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY web ./web
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 8000

# `sh` is used so the script does not depend on the executable bit surviving the copy.
CMD ["sh", "/usr/local/bin/entrypoint.sh"]
