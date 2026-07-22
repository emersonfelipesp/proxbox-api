# Installing proxbox-api (Plugin backend made using FastAPI)

## Integrations Architecture

<p align="center">
  <img
    src="https://emersonfelipesp.com/proxbox-api/integrations-netbox-dark.svg"
    alt="proxbox-api integrations architecture: consumer NetBox plugins funnel into proxbox-api on :8000, which forks into netbox-sdk (write target) and proxmox-sdk (read source)"
    width="900"
  />
</p>

`proxbox-api` sits between **consumer NetBox plugins** (top tier) and the
**downstream REST surfaces** it talks to (bottom tier):

- **Top — consumer plugins**: `netbox-ceph`, `netbox-pbs`, **`netbox-proxbox`** (base plugin),
  `netbox-pdm`, `netbox-packer`. They reach `proxbox-api` over **HTTP REST / SSE / WebSocket**,
  authenticated with the `X-Proxbox-API-Key` header.
- **Middle — `proxbox-api`**: FastAPI app on `:8000`. Owns the SSE and WebSocket
  sync streams, runtime tunables, and the API-key auth surface.
- **Bottom — downstream SDKs**:
  - **Write target** — `netbox-sdk` → `netbox · REST API` (4.5.x / 4.6.x). Async with a
    cached GET layer (60s TTL); concurrency capped by `PROXBOX_NETBOX_MAX_CONCURRENT`.
    Each worker also owns one current `netbox-sdk` client per configured endpoint.
    Credential, URL, timeout, or TLS changes atomically rotate that client and await
    closure of the retired transport. Creating an enabled endpoint publishes its
    lifecycle-owned client immediately; disabling or deleting it clears the default
    runtime reference. Endpoint mutation closes retired transports before the
    response completes, and terminal lifespan shutdown drains active and already
    retiring clients even when startup or request handling fails. Partial endpoint
    updates preserve the exact stored ciphertext for every omitted credential. A
    stalled asynchronous transport close is bounded to 10 seconds so mutation and
    shutdown cannot hang indefinitely.

  - **Read source** — `proxmox-sdk` → `proxmox · REST API` (8.1 / 8.2 / 8.3 / latest, per `SUPPORTED_PROXMOX_VERSIONS` in `proxbox_api/constants.py`). PVE 9.x route groups (HA rules, firewall writes, SDN controllers/zones/VNets/subnets/fabrics, datacenter CPU models, access token regeneration, CRS config) are implemented and degrade gracefully on older clusters. Async, read-only for discovery, `mock | real` modes; concurrency capped by `PROXBOX_VM_SYNC_MAX_CONCURRENCY`.
  - **Firecracker host-agent** — `/cloud/firecracker/provision` and
    `/cloud/firecracker/provision/stream` call the selected host-agent VM to
    health-check KVM, read capacity, prepare kernel/rootfs assets, create the
    micro-VM, and optionally start it. NetBox inventory still lives in
    `netbox-proxbox`; this service owns the host-agent HTTP contract and
    validates caller-supplied host-agent URLs with its SSRF guard before making
    outbound requests.

Python callers should keep using the synchronous public export
`proxbox_api.get_netbox_session(...)` outside an event loop. Internal imports from
`proxbox_api.session.netbox` now use the awaitable `get_netbox_session()` /
`get_netbox_async_session()` providers; async callers should migrate to those named
providers rather than invoking the synchronous facade from a running loop.

The interactive version of this diagram lives at
[emersonfelipesp.com/proxbox-api](https://emersonfelipesp.com/proxbox-api).

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

The VM reconciliation engine is documented in
[`docs/sync/reconciliation-architecture.md`](docs/sync/reconciliation-architecture.md).
Python is the default engine; the optional Rust engine is for compare-mode validation and explicit
opt-in testing.

Firecracker host-agent provisioning is documented in
[`docs/operations/firecracker.md`](docs/operations/firecracker.md), including
the Cloud endpoints, SSE events, request shape, and response shape.

PBS, PDM, Ceph, intent, SSH, and the broader NMS Cloud route groups are
indexed in [`docs/api/service-routes.md`](docs/api/service-routes.md), including
`PROXBOX_FEATURES` sidecar-only behavior.

### VM interface sync strategy

VM network sync accepts `vm_interface_sync_strategy` on the VM sync and
interface/IP stream routes. The default `guest_os_model` keeps core NetBox
`virtualization.VMInterface` records named from Proxmox config (`net0`,
`net1`, ...) and writes guest OS interfaces (`ens18`, `eth0`, ...) to the
netbox-proxbox plugin endpoints when QEMU guest-agent data is available. Guest
address rows reference the same core `ipam.IPAddress` IDs already reconciled on
the VMInterface; proxbox-api does not create duplicate IPAM records for the
guest side.

`legacy_rename` preserves the deprecated behavior where
`use_guest_agent_interface_name=true` renames the core VMInterface to the guest
OS name. The backend logs a deprecation warning when that strategy is selected.

### Local docs build

```bash
uv sync --extra docs --group dev
uv run mkdocs serve
```

### Languages

- English (default)
- Brazilian Portuguese (`pt-BR`) as optional translation

## Using docker (recommended)

All images are **Alpine-based** (smaller footprint), built from this repository with **uv** and **`uv.lock`** in a multi-stage Dockerfile. Three Python-only variants are published to Docker Hub by default, plus opt-in experimental PyO3/Rust variants:

| Variant | Tags | Description |
|---------|------|-------------|
| **Raw** (default) | `latest`, `<version>` | Pure uvicorn, HTTP only. Smallest image. |
| **Nginx** | `latest-nginx`, `<version>-nginx` | nginx terminates HTTPS via mkcert; proxies to uvicorn. |
| **Granian** | `latest-granian`, `<version>-granian` | [Granian](https://github.com/emmett-framework/granian) (Rust ASGI server) with native TLS via mkcert. No nginx. |
| **Raw PyO3/Rust** (experimental) | `experimental`, `pyo3-rust`, `<version>-pyo3-rust` | Raw image with the optional PyO3 reconciliation engine installed and enabled. |
| **Nginx PyO3/Rust** (experimental) | `experimental-nginx`, `pyo3-rust-nginx`, `<version>-pyo3-rust-nginx` | nginx image with the optional PyO3 reconciliation engine installed and enabled. |
| **Granian PyO3/Rust** (experimental) | `experimental-granian`, `pyo3-rust-granian`, `<version>-pyo3-rust-granian` | granian image with the optional PyO3 reconciliation engine installed and enabled. |

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

Plain HTTP requests to the TLS port return a structured JSON `400` body
(`{"error":"plain_http_on_https_port", ...}`) instead of nginx's stock 400 page,
so clients can detect the misconfiguration. When wiring this image into the
NetBox `netbox-proxbox` plugin (>= 0.0.16), set **Use HTTPS** ✓ and (if using
the bundled mkcert cert) **Verify SSL** ✗ on the FastAPI endpoint —
[netbox-proxbox#352](https://github.com/emersonfelipesp/netbox-proxbox/issues/352).

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

### Experimental PyO3/Rust images

The default images above continue to use the Python reconciliation engine. To opt in to the native PyO3/Rust implementation, run one of the experimental tags. The raw alias is the easiest path:

```bash
docker pull emersonfelipesp/proxbox-api:pyo3-rust
docker run -d -p 8000:8000 --name proxbox-api-rust \
  emersonfelipesp/proxbox-api:pyo3-rust
```

Equivalent HTTPS variants are available as `pyo3-rust-nginx` and
`pyo3-rust-granian`. These images set
`PROXBOX_RECONCILIATION_ENGINE=rust` and include the local
`proxbox-reconcile-rs` native extension. Use the standard Python-only tags to
roll back immediately.

### Docker runtime environment variables

Common to all images, including the experimental PyO3/Rust variants:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Port the server listens on |
| `PROXBOX_BIND_HOST` | `0.0.0.0` | Bind address for the API server. Set to `::` for IPv4 + IPv6 dual-stack. Honored by the `raw` and `granian` images; the `nginx` image listens on both stacks unconditionally. |
| `PROXBOX_LOG_LEVEL` | `INFO` | Console log verbosity. Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Set to `DEBUG` for verbose tracing (also enables full `netbox_sdk.client` request tracing). The in-memory log buffer and rotating file handler are unaffected. |

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

### Database Persistence

The SQLite database is stored at `/data/database.db` by default. The `/data` directory is declared as a Docker volume mount point, allowing you to persist the database across container restarts and image upgrades.

**Mount a volume for persistence:**

```bash
docker run -d -p 8000:8000 \
  -v proxbox-data:/data \
  --name proxbox-api \
  emersonfelipesp/proxbox-api:latest
```

**Or mount a host directory:**

```bash
docker run -d -p 8000:8000 \
  -v /host/path/to/data:/data \
  --name proxbox-api \
  emersonfelipesp/proxbox-api:latest
```

**Override the database path (optional):**

If you prefer a custom database location, set `PROXBOX_DATABASE_PATH`:

```bash
docker run -d -p 8000:8000 \
  -e PROXBOX_DATABASE_PATH=/custom/path/database.db \
  -v /custom/path:/custom/path \
  --name proxbox-api \
  emersonfelipesp/proxbox-api:latest
```

**With Docker Compose:**

```yaml
services:
  proxbox-api:
    image: emersonfelipesp/proxbox-api:latest
    ports:
      - "8000:8000"
    volumes:
      - proxbox-data:/data
    environment:
      - PROXBOX_BIND_HOST=0.0.0.0

volumes:
  proxbox-data:
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

### NetBox PostgreSQL Connection Pool

proxbox-api holds at most `PROXBOX_NETBOX_MAX_CONCURRENT` in-flight NetBox
requests per worker at a time. Peak PostgreSQL connections across the whole
deployment equal roughly:

```
peak_connections ≈ PROXBOX_NETBOX_MAX_CONCURRENT × uvicorn_workers
```

**Key concurrency tunables:**

| Env var | Default | Effect |
|---------|---------|--------|
| `PROXBOX_NETBOX_MAX_CONCURRENT` | 1 | Caps concurrent NetBox HTTP requests per worker. This is the primary lever for PostgreSQL connection usage. |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | 8 | Caps simultaneous VM create/update operations per sync pass (semaphore-bounded). |
| `PROXBOX_NETBOX_GET_CACHE_TTL` | 60 s | GET cache TTL. Raising this reduces total NetBox requests — a cache hit costs zero connections. |

**When `netbox_overwhelmed` errors appear:**

```bash
# Primary fix: reduce write concurrency (VM sync)
export PROXBOX_NETBOX_WRITE_CONCURRENCY=4

# Extend GET cache to reduce read traffic
export PROXBOX_NETBOX_GET_CACHE_TTL=300

# Already the default — keep max concurrent at 1
export PROXBOX_NETBOX_MAX_CONCURRENT=1
```

For deployments with many concurrent clients, placing **PgBouncer in
transaction mode** in front of PostgreSQL significantly raises the effective
connection headroom. With PgBouncer active, set `CONN_MAX_AGE=0` in NetBox's
`configuration.py` and you can safely raise `PROXBOX_NETBOX_MAX_CONCURRENT` to
2–4.

See [docs/getting-started/configuration.md](docs/getting-started/configuration.md#netbox-postgresql-connection-pool)
for the full tunables table, peak connection formula, sizing guidance by cluster
size, and PgBouncer sample configuration.

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

## LLM Agent Safety

> **Before calling any destructive verb, read `AGENTS.md` §"LLM Agent Safety Guardrails".**

README is intentionally the short first-read pointer for agents. All write verbs (`start`, `stop`, `snapshot`, `migrate`, `delete`) require `ProxmoxEndpoint.allow_writes=True` (database default: `False`) and an `X-Proxbox-Actor` attribution header. A `403 writes_disabled_for_endpoint` response is a hard stop; the full protocol lives in `AGENTS.md` §"LLM Agent Safety Guardrails".

**LLM agents MUST NOT:**
- Autonomously set `allow_writes=True` on any endpoint
- Invoke `DELETE /proxmox/{vm_type}/{vmid}` or any snapshot/backup delete without explicit human confirmation

The enforcement point is `proxbox_api/database.py::ProxmoxEndpoint.allow_writes` (field default `False`) and `proxbox_api/routes/proxmox_actions.py::_gate` (403 gate). Pinned by `tests/test_static_guardrails.py`.
