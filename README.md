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

The project is **one service**. This container runs its own Ollama server *and* the
FastAPI app, and the SQLite database lives on the same volume.

| Service | Source             | Dockerfile path | Volume  | Env |
| ------- | ------------------ | --------------- | ------- | --- |
| `bahs`  | this repo (`main`) | `Dockerfile`    | `/data` | —   |

Delete the `ollama` and `data` services in Railway. Neither can work as configured:

- A Railway volume attaches to exactly **one** service, so a separate `data` service
  can never share `/data` with `bahs`. The API already creates `/data/learning.db`
  on the `bahs` volume.
- `bahs` already starts Ollama itself, so a second Ollama service just duplicates the
  model weights in RAM.

If you want scripts and feedback outside the container, replace the `data` service
with a **Railway PostgreSQL** plugin instead of a Docker image.

### Why `ollama` / `data` fail with a pull error

```
The image "docker.io/library/ollama:latest" could not be pulled from the registry.
```

That message means the service is set to **Deploy from a Docker image** whose name is
just the service name — it is not building this repository at all (there is no build
step, only `Initialization → Create container`). Pushing to `main` therefore cannot
change the result, because no commit is ever checked out.

Fix it on the service itself — **Settings → Source → change from Docker Image to the
GitHub repo `alistra742-source/bahs`, branch `main`** (then set Dockerfile Path) — or
simply delete the service as described above.

### Optional: split Ollama into its own service

Only do this on a plan with enough RAM for a dedicated model server.

| Service  | Source             | Dockerfile path     | Volume  | Env              |
| -------- | ------------------ | ------------------- | ------- | ---------------- |
| `ollama` | this repo (`main`) | `Dockerfile.ollama` | `/data` | optional `MODEL` |

Then on `bahs` set `OLLAMA_URL=http://ollama.railway.internal:11434` and
`START_OLLAMA=0` so it uses the remote server instead of its bundled one.
Alternatively leave the source as a Docker image, but use a real image name:
`ollama/ollama:latest` (there is no `library/ollama` image on Docker Hub).

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
