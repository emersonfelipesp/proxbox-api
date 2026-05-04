# Installing proxbox-api (Plugin backend made using FastAPI)

## Tooling: uv + Ruff + ty

This repo uses [uv](https://docs.astral.sh/uv/) to install Python and dependencies, [Ruff](https://docs.astral.sh/ruff/) for linting and formatting, and [ty](https://github.com/astral-sh/ty) for type checking.

```bash
# Runtime only
uv sync

# Tests + Ruff (matches CI)
uv sync --extra test --group dev

# Documentation (MkDocs)
uv sync --extra docs --group dev
```

```bash
uv run ruff check .
uv run ruff format .
uv run ty check proxbox_api/types proxbox_api/utils/retry.py
uv run pytest tests
uv run mkdocs serve   # after syncing with --extra docs
```

## Documentation (MkDocs Material)

Project documentation is available under `docs/` and built with MkDocs Material.

### Local docs build

```bash
uv sync --extra docs --group dev
uv run mkdocs serve
```

### Languages

- English (default)
- Brazilian Portuguese (`pt-BR`) as optional translation

## Using docker (recommended)

All images are **Alpine-based** (smaller footprint), built from this repository with **uv** and **`uv.lock`** in a multi-stage Dockerfile. Three variants are published to Docker Hub:

| Variant | Tags | Description |
|---------|------|-------------|
| **Raw** (default) | `latest`, `<version>` | Pure uvicorn, HTTP only. Smallest image. |
| **Nginx** | `latest-nginx`, `<version>-nginx` | nginx terminates HTTPS via mkcert; proxies to uvicorn. |
| **Granian** | `latest-granian`, `<version>-granian` | [Granian](https://github.com/emmett-framework/granian) (Rust ASGI server) with native TLS via mkcert. No nginx. |

> **Upgrade note:** before v0.0.7, `latest` was the nginx+HTTP image. It is now the raw uvicorn image. Pull `latest-nginx` for the previous behavior.

### Raw image (default)

Plain uvicorn on HTTP — the simplest option for local dev or when you put your own proxy in front.

```bash
docker pull emersonfelipesp/proxbox-api:latest
docker run -d -p 8000:8000 --name proxbox-api emersonfelipesp/proxbox-api:latest
```

Build from source:

```bash
docker build -t proxbox-api:raw .
docker run -d -p 8000:8000 proxbox-api:raw
```

### Nginx image (nginx + mkcert HTTPS + uvicorn)

**nginx** terminates HTTPS on `PORT` (default **8000**) using certificates from [mkcert](https://github.com/FiloSottile/mkcert) and proxies to **uvicorn** on `127.0.0.1:8001`. **supervisord** manages both processes. The nginx config disables proxy buffering so chunked / SSE responses flow through unmodified.

```bash
docker pull emersonfelipesp/proxbox-api:latest-nginx
docker run -d -p 8443:8000 --name proxbox-api-nginx \
  emersonfelipesp/proxbox-api:latest-nginx
```

Build from source:

```bash
docker build --target nginx -t proxbox-api:nginx .
docker run -d -p 8443:8000 proxbox-api:nginx
```

### Granian image (granian + mkcert HTTPS)

[Granian](https://github.com/emmett-framework/granian) is a Rust-based ASGI server with native HTTP/2, WebSocket, and TLS support. This variant eliminates nginx and supervisord — a single granian process handles everything.

```bash
docker pull emersonfelipesp/proxbox-api:latest-granian
docker run -d -p 8443:8000 --name proxbox-api-granian \
  emersonfelipesp/proxbox-api:latest-granian
```

Build from source:

```bash
docker build --target granian -t proxbox-api:granian .
docker run -d -p 8443:8000 proxbox-api:granian
```

### Docker runtime environment variables

Common to all images (`raw`, `nginx`, `granian`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Port the server listens on |
| `PROXBOX_BIND_HOST` | `0.0.0.0` | Bind address for the API server. Set to `::` for IPv4 + IPv6 dual-stack. Honored by the `raw` and `granian` images; the `nginx` image listens on both stacks unconditionally. |

mkcert-specific (only for `nginx` and `granian`):

| Variable | Default | Description |
|----------|---------|-------------|
| `MKCERT_CERT_DIR` | `/certs` | Directory where certs are stored |
| `MKCERT_EXTRA_NAMES` | — | Extra SANs (commas or spaces), e.g. `proxbox.lan,10.0.0.5` |
| `CAROOT` | — | Mount a volume here to persist the local CA across container restarts |

```bash
docker run -d -p 8443:8000 --name proxbox-api-tls \
  -e MKCERT_EXTRA_NAMES='myhost.local,192.168.1.10' \
  emersonfelipesp/proxbox-api:latest-nginx
```

### Binding to IPv6 / dual-stack

```bash
docker run -d -p 8000:8000 -e PROXBOX_BIND_HOST=:: \
  emersonfelipesp/proxbox-api:latest
```

In Docker Compose `environment:` **list-form**, the value is taken verbatim — quotes are NOT stripped — so `- PROXBOX_BIND_HOST="::"` arrives in the container as the literal string `"::"`. The container sanitizes surrounding quotes defensively, but the recommended forms are:

```yaml
environment:
  - PROXBOX_BIND_HOST=::          # list-form: NO quotes
```

```yaml
environment:
  PROXBOX_BIND_HOST: "::"         # map-form: YAML strips the quotes
```

To run a shell instead of starting the server, pass a command (the entrypoint delegates to it):

```bash
docker run --rm emersonfelipesp/proxbox-api:latest-nginx sh
```

## Installing from PyPI

The package is published to [PyPI](https://pypi.org/project/proxbox-api/) as `proxbox-api`.

```bash
pip install proxbox-api
```

Or with `uv`:

```bash
uv add proxbox-api
```

Start the server after installing:

```bash
python -m uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000
```

## Using git repository

### Clone the repository

```
git clone https://github.com/emersonfelipesp/proxbox-api.git
```

### Change to project root folder

```
cd proxbox-api
```

### Install dependencies

From the repository root (where `pyproject.toml` lives):

```
uv sync
```

### Start the FastAPI app (recommended)

From the repository root:

```
uv run fastapi run proxbox_api.main:app --host 0.0.0.0 --port 8000
```

- `--host 0.0.0.0` will make the app available on all host network interfaces, which my not be recommended.
Just pass your desired IP like `--host <YOUR-IP>` and it will also work.

- `--port 8000` is the default port, but you can change it if needed. Just to remember to update it on NetBox also, at FastAPI Endpoint model.

### Cache Configuration (optional)

Control NetBox API request caching to optimize sync performance:

```bash
# 5-minute TTL (default is 60 seconds)
export PROXBOX_NETBOX_GET_CACHE_TTL=300

# Disable caching entirely
export PROXBOX_NETBOX_GET_CACHE_TTL=0

# Increase max entries (default 4096)
export PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES=8192

# Set max cache size in bytes (default 52428800 = 50MB)
export PROXBOX_NETBOX_GET_CACHE_MAX_BYTES=104857600  # 100MB

# Enable debug logging
export PROXBOX_DEBUG_CACHE=1

uv run fastapi run proxbox_api.main:app --host 0.0.0.0 --port 8000
```

Cache metrics are available at `GET /cache` and `GET /cache/metrics/prometheus`.

### Backup Sync Throttling (optional)

Control backup synchronization batch size and delay to prevent overwhelming NetBox's PostgreSQL connection pool:

```bash
# Batch size for backup sync (default 5, was 10 before fix)
export PROXBOX_BACKUP_BATCH_SIZE=5

# Delay between batches in milliseconds (default 200ms)
export PROXBOX_BACKUP_BATCH_DELAY_MS=200

uv run fastapi run proxbox_api.main:app --host 0.0.0.0 --port 8000
```

**Why adjust these?**
- **Smaller batch size (3-5)**: Use when NetBox has limited PostgreSQL connections or many concurrent users
- **Larger batch size (10-20)**: Safe if NetBox has a large PostgreSQL pool (50+ connections) and dedicated hardware
- **Longer delay (500-1000ms)**: Helps when "database unavailable" errors appear during full sync
- **Shorter delay (0-100ms)**: Faster sync when NetBox is lightly loaded

**Symptoms of incorrect tuning:**
- HTTP 500 "database unavailable" during backup/VM sync → decrease batch size, increase delay
- HTTP 502 "Response ended prematurely" → decrease batch size, increase delay
- Slow sync performance on powerful hardware → increase batch size, decrease delay

### Alternative: pip editable install

```
pip install -e .
fastapi run proxbox_api.main:app --host 0.0.0.0 --port 8000
```

Or with uvicorn:

```
uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000
```

## HTTPS without Docker

### Local development: mkcert

Install the local CA in your system trust store, generate a cert, then point uvicorn at the PEM files:

```
mkcert -install
mkcert proxbox.backend.local localhost 127.0.0.1 ::1
```

From the repository root (adjust paths to the files mkcert printed):

```
uv run uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000 --reload \
  --ssl-keyfile=./proxbox.backend.local+3-key.pem \
  --ssl-certfile=./proxbox.backend.local+3.pem
```

**NetBox plugin layout** (paths differ): example with `--app-dir` and module path as in your install:

```
/opt/netbox/venv/bin/uvicorn netbox-proxbox.proxbox_api.proxbox_api.main:app \
  --host 127.0.0.1 --port 8000 --app-dir /opt/netbox/netbox \
  --ssl-keyfile=/path/to/localhost+2-key.pem \
  --ssl-certfile=/path/to/localhost+2.pem
```

Optional **nginx** in front of that uvicorn: copy or adapt a site config that `proxy_pass`es to `http://127.0.0.1:8000` and terminates TLS on 443 (same idea as [TLS with a real certificate](#tls-with-a-real-certificate-no-docker) below).

### TLS with a real certificate (no Docker)

Use **Let’s Encrypt**, a **corporate CA**, or any PEM **full chain + private key**. Prefer terminating TLS in **nginx or Caddy** and keeping the app on plain HTTP on localhost; uvicorn TLS is fine for small setups if you accept Python as the TLS endpoint.

**1. Certificate files**

- **Let’s Encrypt (Certbot):** typically `/etc/letsencrypt/live/<your-domain>/fullchain.pem` and `privkey.pem`. Renew with `certbot renew`; reload nginx (or your proxy) after renewal.
- **Corporate / manual:** use the PEM the CA gave you: **certificate file** must include the **full chain** (leaf + intermediates) in one file, plus the **unencrypted private key** (or use `--ssl-keyfile-password` with uvicorn if the key is encrypted).

**2. Recommended: reverse proxy on the same host**

Run the API on HTTP bound to loopback only, proxy from 443:

```
uv run uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000
```

Example **nginx** `server` block (replace domain and paths):

```nginx
server {
    listen 443 ssl http2;
    server_name api.example.com;

    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
        proxy_buffering off;
    }
}
```

For `Connection $connection_upgrade`, add at `http` level:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Reload nginx after editing. Point NetBox’s FastAPI endpoint at `https://api.example.com` (and port **443** or your chosen HTTPS port).

**3. Alternative: uvicorn serves TLS directly**

```
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8443 \
  --ssl-certfile=/etc/letsencrypt/live/api.example.com/fullchain.pem \
  --ssl-keyfile=/etc/letsencrypt/live/api.example.com/privkey.pem
```

Ensure the user running uvicorn can read the key (often `root` owns `/etc/letsencrypt`; use `group` ACLs, a deploy user + copied certs, or **hitch**/proxy instead). Use **`fullchain.pem`** for `--ssl-certfile` so clients receive the full chain.
