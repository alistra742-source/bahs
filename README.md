# bahs

FastAPI service that generates Roblox Lua scripts with Ollama (`qwen2.5-coder:3b`),
learns from feedback, and ships a Roblox executor GUI in [`client.lua`](client.lua).

The repo builds exactly one thing: the API. Ollama is a stock Docker-image service.

## Layout

| Service  | Source                       | Build                          | Volume                        | Domain            |
| -------- | ---------------------------- | ------------------------------ | ----------------------------- | ----------------- |
| `bahs`   | this repo, branch `main`     | Dockerfile Path = `Dockerfile` | none (stateless)              | yes — the site    |
| `ollama` | Docker Image `ollama/ollama` | none, the image runs as-is     | one, at **`/root/.ollama`**   | none              |
| Postgres | Railway plugin               | —                              | (its own)                     | none              |

`bahs` reaches Ollama over private networking at `http://ollama.railway.internal:11434`
and **pulls `MODEL` into Ollama's volume itself** on startup, so the Ollama service needs
nothing but the image and the volume — no start command, no variables, no repo build.

## Setting up the `ollama` service

1. **Settings → Source → Deploy from Docker Image**, image **`ollama/ollama`**.
   It must be the namespaced name. A bare `ollama` resolves to
   `docker.io/library/ollama`, which does not exist — that is the
   `could not be pulled from the registry` failure.
2. **Settings → Volumes → + New Volume**, mount path **`/root/.ollama`**.
   That is the model root the image already uses, so the weights persist across
   redeploys and the ~2 GB download happens once.
3. **Do not** set `OLLAMA_HOST` (the image already binds `0.0.0.0:11434`), **do not**
   set `PORT`, and **do not** set a custom start command.
4. No public domain — leave the service unexposed. Only `bahs` needs a domain.
5. Name it exactly `ollama`: the API resolves it as `ollama.railway.internal`.

## Setting up the `bahs` service

1. **Settings → Source** = GitHub repo, branch `main`.
2. **Settings → Build → Dockerfile Path** = `Dockerfile`.
3. **Settings → Variables → Add Reference → Postgres → `DATABASE_URL`.**
   `OLLAMA_URL` is baked into the image as `http://ollama.railway.internal:11434`, so
   there is nothing else to set unless you rename that service.
4. **Settings → Networking → Generate Domain** — that URL is the site and the value
   `client.lua` needs.
5. No volume.

## Model pull

Whoever starts first, the API sorts it out: on startup it checks Ollama's `/api/tags`
and, if `MODEL` is missing, calls `/api/pull` and streams the download on a background
thread. Until the weights land, `/` and `/health` report `"model_ready": false` and
`/generate` returns `503`. After that the model is served from the volume.

To use a different model, set `MODEL` on the **`bahs`** service — that is where the
model name is read. Leave the Ollama service untouched.

## Endpoints

| Method | Path        | Purpose                                                    |
| ------ | ----------- | ---------------------------------------------------------- |
| GET    | `/`         | The site: type a prompt, get a script, mark it works/broken |
| GET    | `/health`   | Always `200`; body reports Postgres and Ollama state        |
| POST   | `/generate` | `{ "prompt": "...", "temperature": 0.7 }`                  |
| POST   | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }`           |

Once `API_KEY` is set on the service, `/generate` and `/feedback` require it; `/`,
`/health` and `/docs` stay open. Scripts and feedback live in Postgres (`scripts`
table, created on first use).

## Using the site

Open the `bahs` domain, paste the API key if the page asks for it (the field only
appears when the service requires one), describe the script, and hit **Generate**. Once
you have run it in the executor, mark it **works** or **broken**: working scripts are
fed into later prompts as examples, broken ones as mistakes to avoid. The chips in the
header poll `/health`, so the page also tells you whether Postgres, Ollama and the model
are up.

`client.lua` calls the same endpoints and sends the same key — paste it into `API_KEY`
near the top of that file.

## API key

Set `API_KEY` on the **`bahs`** service (Settings → Variables) to lock down the model
endpoints. Callers then send it as a header:

```
X-API-Key: <the value>            # or: Authorization: Bearer <the value>
```

With `API_KEY` unset the endpoints are open, which is what a fresh deploy or a local run
gets. The key is never written into the page: the site stores it in that browser's
localStorage and sends it with each request.

## Environment variables

| Variable       | Default                                | Notes                                            |
| -------------- | -------------------------------------- | ------------------------------------------------ |
| `PORT`         | `8000`                                 | Injected by Railway; the API binds it             |
| `MODEL`        | `qwen2.5-coder:3b`                     | Pulled by the API into Ollama's volume            |
| `OLLAMA_URL`   | `http://ollama.railway.internal:11434` | Baked into the `bahs` image; override to move it  |
| `API_KEY`      | —                                      | When set, `/generate` and `/feedback` need `X-API-Key` |
| `DATABASE_URL` | —                                      | Injected by the Railway Postgres plugin           |
| `POSTGRES_URL` | —                                      | Older alias, accepted as a fallback               |

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

Against a local Ollama the API is also what pulls `MODEL` the first time. `/` and
`/health` report `"database": false` while Postgres is unreachable, and `/generate`
returns `503` instead of crashing.
