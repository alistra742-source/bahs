# bahs

FastAPI service that generates Roblox Lua scripts with Ollama (`qwen2.5-coder:3b`),
learns from feedback, and ships a Roblox executor GUI in [`client.lua`](client.lua).

## Endpoints

| Method | Path        | Purpose                                            |
| ------ | ----------- | -------------------------------------------------- |
| GET    | `/`         | Service info                                       |
| GET    | `/health`   | Always `200`; body reports Postgres and Ollama state |
| POST   | `/generate` | `{ "prompt": "...", "temperature": 0.7 }`          |
| POST   | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }`  |

Scripts and feedback are stored in **Postgres** (`scripts` table, created on first
use), so the API container is stateless and needs no volume.

## Environment variables

| Variable       | Default                  | Notes                                                    |
| -------------- | ------------------------ | -------------------------------------------------------- |
| `PORT`         | `8000`                   | Injected by Railway; the API binds it                     |
| `MODEL`        | `qwen2.5-coder:3b`       | Pulled by the `ollama` service, requested by `bahs`       |
| `OLLAMA_URL`   | `http://localhost:11434` | On Railway: `http://ollama.railway.internal:11434`        |
| `DATABASE_URL` | —                        | Injected by the Railway Postgres plugin                   |
| `POSTGRES_URL` | —                        | Older alias, accepted as a fallback for `DATABASE_URL`    |

## Railway layout

Three pieces, **one volume total** — the API keeps its data in Postgres and the
volume on `ollama` covers the model weights.

| Piece    | Source             | Dockerfile path     | Volume                           | Env |
| -------- | ------------------ | ------------------- | -------------------------------- | --- |
| `bahs`   | this repo (`main`) | `Dockerfile`        | none (stateless)                 | `OLLAMA_URL=http://ollama.railway.internal:11434` |
| `ollama` | this repo (`main`) | `Dockerfile.ollama` | one, mounted at **`/root/.ollama`** | optional `MODEL` |
| Postgres | Railway plugin     | —                   | —                                | injects `DATABASE_URL` |

Mount the volume at `/root/.ollama`, not `/data`: that is the model root the
`ollama` image already uses, so no `OLLAMA_MODELS` override is required and the
weights survive redeploys.

Delete any leftover `data` service — the API no longer reads a SQLite file, so it
has nothing to share. A Railway volume also attaches to exactly one service, which
is why the database moved to the Postgres plugin instead.

### Why a `data` service used to fail with a pull error

```
The image "docker.io/library/ollama:latest" could not be pulled from the registry.
```

That message means the service is set to **Deploy from a Docker image** whose name is
just the service name — it is not building this repository at all (there is no build
step, only `Initialization → Create container`). Pushing to `main` therefore cannot
change the result, because no commit is ever checked out.

Fix it on the service itself — **Settings → Source → change from Docker Image to the
GitHub repo `alistra742-source/bahs`, branch `main`** (then set Dockerfile Path to
`Dockerfile.ollama` for the Ollama service) — or simply delete the service.

## Local run

Needs a Postgres to point at (any instance works; the `scripts` table is created on
first request) and an Ollama server:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
OLLAMA_URL=http://localhost:11434 \
uvicorn server:app --port 8000
```

The API can be up before the model finishes downloading — `/health` reports
`"model_ready": false` until the pull completes, and `/generate` returns `503`
until then. `/health` also reports `"database": false` while Postgres is unreachable,
and `/generate` returns `503` instead of crashing.

> Note: `client.lua` still points at the placeholder `https://your-app.up.railway.app`.
> Replace it with the real domain of the `bahs` service.
