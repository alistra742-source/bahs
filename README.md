# bahs

FastAPI service that generates Roblox Lua scripts with Ollama (`qwen2.5-coder:3b`),
learns from feedback, and ships a Roblox executor GUI in [`client.lua`](client.lua).

## Endpoints

| Method | Path        | Purpose                                            |
| ------ | ----------- | -------------------------------------------------- |
| GET    | `/`         | Service info                                       |
| GET    | `/health`   | Always `200`; body reports whether Ollama is ready |
| POST   | `/generate` | `{ "prompt": "...", "temperature": 0.7 }`          |
| POST   | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }`  |

## Environment variables

| Variable          | Default                     | Notes                                             |
| ----------------- | --------------------------- | ------------------------------------------------- |
| `PORT`            | `8000`                      | Injected by Railway; the API binds it             |
| `MODEL`           | `qwen2.5-coder:3b`          | Pulled on first start                             |
| `OLLAMA_URL`      | `http://localhost:11434`    | Point at the Ollama service when running split    |
| `OLLAMA_MODELS`   | `/data/ollama`              | Model cache, put it on a volume                   |
| `DB_PATH`         | `/data/learning.db`         | Falls back to `./learning.db` if `/data` is absent |
| `START_OLLAMA`    | `1`                         | Set `0` to skip the bundled Ollama server          |

## Railway layout

All three services can deploy from this repository. Each one just needs a different
Dockerfile path in **Settings → Build → Dockerfile Path** (or `RAILWAY_DOCKERFILE_PATH`):

| Service  | Dockerfile path   | Volume mount    | Env                                                                 |
| -------- | ----------------- | --------------- | ------------------------------------------------------------------- |
| `bahs`   | `Dockerfile`      | `/data`         | — (runs its own Ollama by default)                                   |
| `ollama` | `Dockerfile.ollama` | `/data`       | optional `MODEL`                                                    |
| `data`   | *see below*       | —               | —                                                                   |

If `bahs` is meant to use the separate `ollama` service instead of the bundled one,
set `OLLAMA_URL=http://ollama.railway.internal:11434` and `START_OLLAMA=0` on `bahs`.

### About the `data` service

Railway volumes are attached to **one** service, so a separate `data` service cannot
share storage with `bahs`. Two valid options:

1. **Drop the `data` service** and attach its volume to `bahs` at `/data`. The API
   creates and uses `/data/learning.db` automatically. (Simplest — the app is
   self-contained.)
2. **Make `data` a real database** (Railway PostgreSQL) if you want the scripts and
   feedback to live outside the app container.

## Local run

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
OLLAMA_URL=http://localhost:11434 uvicorn server:app --port 8000
```

The API can be up before the model finishes downloading — `/health` reports
`"model_ready": false` until the pull completes, and `/generate` returns `503`
until then.

> Note: `client.lua` still points at the placeholder `https://your-app.up.railway.app`.
> Replace it with the real domain of the `bahs` service.
