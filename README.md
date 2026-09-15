# bahs

FastAPI service that generates Roblox Lua scripts with Ollama (`qwen2.5-coder:3b`),
learns from feedback, and ships a Roblox executor GUI in [`client.lua`](client.lua).

Two services, one volume:

- **`bahs`** — the FastAPI app, built from `Dockerfile`. Serves the site/API and takes
  the public domain. Stateless: scripts and feedback live in Postgres.
- **`ollama`** — the model server, built from `Dockerfile.ollama`. Pulls `MODEL` into
  its single volume at `/root/.ollama` and serves it on `0.0.0.0:11434` for `bahs` to
  reach over private networking. Needs no public domain.

## Endpoints

| Method | Path        | Purpose                                               |
| ------ | ----------- | ----------------------------------------------------- |
| GET    | `/`         | Status page (live Postgres / Ollama / model state)     |
| GET    | `/health`   | Always `200`; body reports Postgres and Ollama state   |
| POST   | `/generate` | `{ "prompt": "...", "temperature": 0.7 }`             |
| POST   | `/feedback` | `{ "script_id": 1, "worked": true, "notes": "" }`      |

## Railway layout

| Service  | Source             | Dockerfile path     | Domain           | Volume                             |
| -------- | ------------------ | ------------------- | ---------------- | ---------------------------------- |
| `bahs`   | this repo (`main`) | `Dockerfile`        | yes (public site)| none                               |
| `ollama` | this repo (`main`) | `Dockerfile.ollama` | none             | one, at **`/root/.ollama`**        |
| Postgres | Railway plugin     | —                   | none             | (its own)                          |

### `bahs` (the API and the site)

1. **Source** = GitHub repo `alistra742-source/bahs`, branch `main`.
2. **Build → Dockerfile Path** = `Dockerfile`.
3. **Variables → Add Reference → Postgres → `DATABASE_URL`.**
   `OLLAMA_URL` is already baked into the image as
   `http://ollama.railway.internal:11434`, so you do not have to set it.
4. **Networking → Generate Domain.** That URL is the site and the API base URL for
   `client.lua`. No volume.

### `ollama` (the model server)

1. **Source must be the GitHub repo** — not *Deploy from a Docker Image*. See the
   next section if it is.
2. **Build → Dockerfile Path** = `Dockerfile.ollama`.
3. **Volumes → + New Volume**, mount path `/root/.ollama`. Not `/data`: that is the
   model root Ollama already uses, so no `OLLAMA_MODELS` override is needed and the
   weights survive redeploys.
4. **Variables** (optional): `MODEL` — defaults to `qwen2.5-coder:3b`.
5. No public domain. Leave it "Unexposed"; `bahs` only needs the private hostname.

### The image "docker.io/library/ollama:latest" could not be pulled

That message means the service is set to **Deploy from a Docker Image** whose name is
just the service name, so there is no build step and no commit is ever checked out.
Pushing to `main` cannot change the result.

Fix it on the service: **Settings → Source**. If that page only shows the image and
offers no way to switch, the source cannot be re-pointed on an existing service — so
create a replacement instead:

1. **+ New → GitHub Repo → `alistra742-source/bahs`**, branch `main`.
2. Rename the new service to exactly **`ollama`** (`bahs` reaches it at
   `ollama.railway.internal`, so the name matters).
3. **Build → Dockerfile Path** = `Dockerfile.ollama`.
4. **Volumes → + New Volume** at `/root/.ollama`.
5. Delete the old image-based `ollama` service.

## Environment variables

| Variable       | Default                                  | Notes                                              |
| -------------- | ---------------------------------------- | -------------------------------------------------- |
| `PORT`         | `8000`                                   | Injected by Railway; the API binds it               |
| `MODEL`        | `qwen2.5-coder:3b`                       | Pulled by the `ollama` service                      |
| `OLLAMA_URL`   | `http://ollama.railway.internal:11434`   | Baked into the `bahs` image; override to change it  |
| `OLLAMA_MODELS`| `/root/.ollama`                          | Where the `ollama` volume is mounted                |
| `DATABASE_URL` | —                                        | Injected by the Railway Postgres plugin             |
| `POSTGRES_URL` | —                                        | Older alias, accepted as a fallback for `DATABASE_URL` |

## Order of operations

Start `ollama` first and wait for its log to show `[ollama] pulling qwen2.5-coder:3b`
followed by success — that is the ~2 GB download into the volume, and it happens once.
The `bahs` API can be up the whole time: `/` and `/health` report `"model_ready": false`
until the weights land, and `/generate` returns `503` rather than failing.

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

Both `/` and `/health` report `"database": false` while Postgres is unreachable, and
`/generate` returns `503` instead of crashing.
