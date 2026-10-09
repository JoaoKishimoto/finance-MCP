# Docker, explained with this project

This guide explains each Docker idea using the files in this repository.

## 1. The problem Docker solves

On the VPS you need a specific Python version, about 40 libraries, and a web server that handles HTTPS. Installing those directly on Ubuntu works until another project needs different versions, or until a system upgrade breaks something.

Docker packages each app together with everything it needs into one sealed unit. Each unit runs isolated from the others and from the host, and the host only needs Docker installed.

## 2. Image vs container

| | What it is | Analogy |
|---|---|---|
| **Image** | A read-only snapshot of a filesystem: OS libraries, Python, your code | A class / an `.iso` file |
| **Container** | A running process started from an image, with its own filesystem layer, network and limits | An object instance / a running VM, but far lighter |

You can start many containers from one image. When a container is deleted, anything it wrote inside itself is lost, unless that data lives in a **volume** (section 5).

## 3. The Dockerfile: how the image is built

Open [`../Dockerfile`](../Dockerfile). Each line is an instruction:

- `FROM python:3.13-slim` starts from an official image that has Python installed. Docker Hub publishes it for several CPU types, so your **ARM** Oracle VPS automatically gets the ARM build.
- `WORKDIR /app` is like `cd /app` inside the image.
- `COPY src ./src` copies files from your folder into the image. [`.dockerignore`](../.dockerignore) lists the files that must **not** be copied, such as `.env` with your secrets.
- `RUN pip install .` runs a command **while building** and saves the result in the image.
- `USER app` makes the container run as a normal user, not root. If someone ever exploited the app, they'd have no admin rights.
- `ENV ...` sets default environment variables.
- `HEALTHCHECK` is a command Docker runs every 30 s to check the app is alive. `docker ps` then shows it as `healthy`.
- `CMD [...]` is the command that runs when the container **starts**.

**Layers and cache.** Each instruction creates a layer. When you rebuild, Docker reuses layers whose inputs haven't changed. That's why the slow steps (`pip install`) come before the steps that change often.

Build it: `docker compose build`, or the raw command `docker build -t finance-mcp .`.

## 4. docker compose: how the container runs

Writing long `docker run` commands by hand is error-prone. [`../docker-compose.yml`](../docker-compose.yml) writes the same settings down in a file:

```yaml
services:
  finance-mcp:
    build: .                 # build from ./Dockerfile
    restart: unless-stopped  # auto-restart after crashes and VPS reboots
    env_file: .env           # inject your secrets as environment variables at runtime
    volumes: [finance-data:/data]
    networks: [proxy]
    mem_limit: 256m          # hard RAM cap: if exceeded, the container is killed and restarted
```

Secrets are injected **at runtime** through `env_file`. They're never baked into the image, so the image has no secrets in it and could be shared without leaking anything.

## 5. Volumes: data that survives

A container's own filesystem is temporary, and ours is even `read_only: true` for safety. Anything that must survive goes in a **volume**:

- **Named volume** (`finance-data:/data`): Docker manages where it's stored (under `/var/lib/docker/volumes/`). It holds `config.yaml`, its backups, and the OAuth login state. It survives `docker compose down`, rebuilds and upgrades.
- **Bind mount** (`./Caddyfile:/etc/caddy/Caddyfile:ro` in the proxy): mounts a specific file or folder from the VPS into the container. Use it for files **you** edit by hand.

`docker compose down -v` **deletes** the named volumes. Don't add `-v` unless you really mean it.

## 6. Networks and ports: who can talk to whom

```
Internet ──443──▶ [caddy] ──(proxy network)──▶ [finance-mcp:8000]
                     ▲                               ▲
          ports: "443:443"                  no "ports:" → unreachable from outside
```

- `ports: "443:443"` means "VPS port : container port" and opens the container to the outside world. **Only Caddy** has this.
- Containers on the same Docker **network** can reach each other **by container name**. That's why the Caddyfile says `reverse_proxy finance-mcp:8000`. Docker runs a small internal DNS that resolves `finance-mcp`.
- The `proxy` network is created by the proxy stack and marked `external: true` in this stack, so separate projects (this one, your other MCP, your website) can all join it without sharing a compose file.

## 7. The reverse proxy (Caddy)

One VPS, one IP address, many sites. Caddy listens on 80 and 443, reads the domain each request is for (`finance.joaokishimoto.dev`, `joaokishimoto.dev`, and so on), and forwards the request to the right container. It also gets **free HTTPS certificates from Let's Encrypt automatically** and renews them. claude.ai requires HTTPS, and `.dev` domains only work over HTTPS.

## 8. Commands you'll actually use

Run these inside the folder that has the `docker-compose.yml`:

```bash
docker compose up -d --build     # build (if needed) and start in background (-d = detached)
docker compose ps                # what's running + health
docker compose logs -f           # follow logs (Ctrl+C to stop following)
docker compose restart           # restart (e.g. after editing .env)
docker compose down              # stop and remove containers (volumes are kept)
docker compose exec finance-mcp sh          # open a shell INSIDE the running container
docker compose exec finance-mcp python -m finance_mcp.check   # run our .env checker inside it

docker stats                     # live CPU/RAM per container (check your 12 GB budget)
docker system df                 # disk used by images/volumes
docker image prune               # delete old unused images after upgrades
```

After editing `.env`, run `docker compose up -d`. Compose sees the change and recreates the container. A plain `restart` doesn't reload `env_file`.

## 9. Memory budget on your VPS

| Container | Typical RAM | Cap |
|---|---|---|
| caddy | ~20–40 MB | 128 MB |
| finance-mcp | ~110–150 MB | 256 MB |
| your other MCP | ? | set a `mem_limit` too |
| website + API | ~100–300 MB | set a `mem_limit` too |

That's well under 12 GB. `docker stats` shows the real numbers.
