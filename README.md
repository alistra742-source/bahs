# bahs

FastAPI service that generates Roblox Lua scripts with Ollama (`qwen2.5-coder:3b`),
learns from feedback, and ships a Roblox executor GUI in [`client.lua`](client.lua).

## Endpoints

| Method | Path        | Purpose                                               |
| ------ | ----------- | ----------------------------------------------------- |
| GET    | `/`         | Status page (live Postgres / Ollama / model state)     |
| GET    | `/health`   | Always `200`; body reports Postgres and Ollama state   |
| POST   | `/generate` | `{ "prompt": "...", "temperature": 0.7 }`             |
| POST   | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }`      |

Scripts and feedback are stored in **Postgres** (`scripts` table, created on first
use), so the app container holds no data of its own.

## One service does everything

`Dockerfile.ollama` builds a single image that runs **both** Ollama and the API:

- Ollama listens on `127.0.0.1:11434` — loopback only, it is not the public surface.
- The API binds `$PORT`, which is what Railway's edge proxy routes the service domain
  to, so the site is served at the service's own URL.
- The volume at `/root/.ollama` holds the model weights, so the pull happens once.
- `server.py` reaches the model over loopback; `OLLAMA_URL` is baked into the image.

## Railway layout

Two pieces, **one volume total**.

| Piece    | Source             | Dockerfile path     | Volume                                | Env |
| -------- | ------------------ | ------------------- | ------------------------------------- | --- |
| app      | this repo (`main`) | `Dockerfile.ollama` | one, mounted at **`/root/.ollama`**   | `DATABASE_URL` (reference Postgres) |
| Postgres | Railway plugin     | —                   | (its own volume)                      | injects `DATABASE_URL` |

Setup, on the app service:

1. **Settings → Build → Dockerfile Path** = `Dockerfile.ollama`
   (leave it as the default `Dockerfile` and you get the API-only image instead).
2. **Settings → Volumes → + New Volume**, mount path `/root/.ollama`.
   Not `/data`: that is the model root Ollama already uses, so no `OLLAMA_MODELS`
   override is needed and the weights survive redeploys.
3. **Settings → Variables → Add Reference → Postgres → `DATABASE_URL`.**
4. **Settings → Networking → Generate Domain** — that URL is the site *and* the API
   base URL for `client.lua`.

`Dockerfile` (the API-only image, no Ollama) still exists if you would rather run the
model in a second service, but then that split needs its own `OLLAMA_URL`. The combined
image above is the supported layout and it is what the domain serves.

### If the domain says "Application failed to respond"

Railway's proxy reaches your app on the `PORT` it injects. The combined image binds
that port, so this should not happen — if it does, the service's public domain is
still pointing at a stale port:

1. Open the service → **Variables** and read the `PORT` value Railway injected.
2. **Settings → Networking** → edit the domain → set its port to that same value.
3. Redeploy.

Do not "fix" it by moving Ollama onto `$PORT`: the API expects it on `127.0.0.1:11434`.

### If `ollama pull` fails

```
The image "docker.io/library/ollama:latest" could not be pulled from the registry.
```

That message means the service is set to **Deploy from a Docker image** whose name is
just the service name — it is not building this repository at all (there is no build
step, only `Initialization → Create container`). Pushing to `main` cannot change the
result, because no commit is ever checked out. Fix it on the service itself:
**Settings → Source → change from Docker Image to the GitHub repo, branch `main`**,
then set Dockerfile Path to `Dockerfile.ollama`.

## Environment variables

| Variable       | Default                  | Notes                                                   |
| -------------- | ------------------------ | ------------------------------------------------------- |
| `PORT`         | `8000`                   | Injected by Railway; the API binds it                    |
| `MODEL`        | `qwen2.5-coder:3b`       | Pulled on first boot, requested by the API               |
| `OLLAMA_URL`   | `http://127.0.0.1:11434` | Baked into the combined image; override to split services |
| `OLLAMA_MODELS`| `/root/.ollama`          | Where the volume is mounted                              |
| `DATABASE_URL` | —                        | Injected by the Railway Postgres plugin                  |
| `POSTGRES_URL` | —                        | Older alias, accepted as a fallback for `DATABASE_URL`   |

## Local run

Needs a Postgres to point at (the `scripts` table is created on first request) and an
Ollama server:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
OLLAMA_URL=http://localhost:11434 \
uvicorn server:app --port 8000
```

The API can be up before the model finishes downloading — `/` and `/health` report
`"model_ready": false` until the pull completes, and `/generate` returns `503` until
then. Both also report `"database": false` while Postgres is unreachable, and
`/generate` returns `503` instead of crashing.
