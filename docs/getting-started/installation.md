# Installation

This page documents supported ways to run `proxbox-api`.

## Requirements

- Python 3.11+
- `uv` (recommended) or `pip`
- Network access to NetBox and Proxmox targets

## Option 1: Docker (recommended for quick start)

All Docker images are **Alpine-based** (smaller footprint). The default tags use the Python reconciliation engine. Experimental PyO3/Rust tags are available for opt-in native-engine testing:

| Variant | Tags | Description |
|---------|------|-------------|
| **Raw** (default) | `latest`, `<version>` | Pure uvicorn, HTTP only. Smallest image. |
| **Nginx** | `latest-nginx`, `<version>-nginx` | nginx terminates HTTPS via mkcert; proxies to uvicorn. |
| **Granian** | `latest-granian`, `<version>-granian` | Granian (Rust ASGI server) with native TLS via mkcert. |
| **Raw PyO3/Rust** (experimental) | `experimental`, `pyo3-rust`, `<version>-pyo3-rust` | Raw image with the optional PyO3 reconciliation engine installed and enabled. |
| **Nginx PyO3/Rust** (experimental) | `experimental-nginx`, `pyo3-rust-nginx`, `<version>-pyo3-rust-nginx` | nginx image with the optional PyO3 reconciliation engine installed and enabled. |
| **Granian PyO3/Rust** (experimental) | `experimental-granian`, `pyo3-rust-granian`, `<version>-pyo3-rust-granian` | granian image with the optional PyO3 reconciliation engine installed and enabled. |

### Raw image — HTTP only (default)

Simplest option. No proxy in front, plain HTTP. Ideal for local development or behind your own reverse proxy.

```bash
docker pull emersonfelipesp/proxbox-api:latest
docker run -d -p 8000:8000 --name proxbox-api emersonfelipesp/proxbox-api:latest
```

Service URL:

- <http://127.0.0.1:8000>

### Nginx image — HTTPS with mkcert

nginx terminates HTTPS using auto-generated [mkcert](https://github.com/FiloSottile/mkcert) certificates and proxies to uvicorn inside the container.

```bash
docker pull emersonfelipesp/proxbox-api:latest-nginx
docker run -d -p 8443:8000 --name proxbox-api-nginx \
  emersonfelipesp/proxbox-api:latest-nginx
```

Service URL:

- <https://127.0.0.1:8443> (self-signed, trusted on the container host)

#### Connecting netbox-proxbox to the nginx image

The nginx image is HTTPS-only — plain HTTP requests to the TLS port return a
JSON `400` body with `{"error":"plain_http_on_https_port", ...}` (rendered from
nginx's internal `497` code). When configuring the **FastAPI Endpoint** in the
NetBox `netbox-proxbox` plugin, set:

| Field | Value |
|-------|-------|
| **Use HTTPS** | ✓ enabled |
| **Verify SSL** | ✗ disabled (when using the bundled mkcert certificate) |
| **Port** | the host port mapped to container `8000` (typically `8800` or `8443`) |

The `Use HTTPS` and `Verify SSL` toggles are independent in
`netbox-proxbox >= 0.0.16` — see
[issue #352](https://github.com/emersonfelipesp/netbox-proxbox/issues/352) for
context. Earlier plugin releases couple the two flags, which makes the
nginx-image + self-signed-cert combination unreachable.

### Granian image — HTTPS with mkcert (no nginx)

[Granian](https://github.com/emmett-framework/granian) is a Rust-based ASGI server with native TLS and HTTP/2. A single process handles everything — no nginx or supervisord required.

```bash
docker pull emersonfelipesp/proxbox-api:latest-granian
docker run -d -p 8443:8000 --name proxbox-api-granian \
  emersonfelipesp/proxbox-api:latest-granian
```

Service URL:

- <https://127.0.0.1:8443>

### Experimental PyO3/Rust images

Use the experimental images when you want the optional native reconciliation
engine to run by default. The raw alias is the simplest Docker command:

```bash
docker pull emersonfelipesp/proxbox-api:pyo3-rust
docker run -d -p 8000:8000 --name proxbox-api-rust \
  emersonfelipesp/proxbox-api:pyo3-rust
```

HTTPS variants are published as `pyo3-rust-nginx` and `pyo3-rust-granian`.
These images set `PROXBOX_RECONCILIATION_ENGINE=rust` and include the
`proxbox-reconcile-rs` PyO3 extension. Switch back to `latest`, `latest-nginx`,
or `latest-granian` to return to the Python-only implementation.

### Docker runtime environment variables

Common to all images, including the experimental PyO3/Rust variants:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Port the server listens on |
| `PROXBOX_BIND_HOST` | `0.0.0.0` | Address the server binds to. Set to `::` for IPv4 + IPv6 dual-stack. Honored by the `raw` and `granian` images; the `nginx` image listens on both stacks unconditionally. |

mkcert-specific (only for the `nginx` and `granian` images):

| Variable | Default | Description |
|----------|---------|-------------|
| `MKCERT_CERT_DIR` | `/certs` | Directory where certificates are stored |
| `MKCERT_EXTRA_NAMES` | — | Extra SANs (comma or space separated), e.g. `proxbox.lan,10.0.0.5` |
| `CAROOT` | — | Mount a volume here to persist the local CA across restarts |

Example with extra SANs:

```bash
docker run -d -p 8443:8000 --name proxbox-api-tls \
  -e MKCERT_EXTRA_NAMES='myhost.local,192.168.1.10' \
  emersonfelipesp/proxbox-api:latest-nginx
```

### Mounting custom certificates

Both the `nginx` and `granian` images detect pre-existing certificates at startup.
If `cert.pem` **and** `key.pem` are already present inside `$MKCERT_CERT_DIR` (default `/certs`),
mkcert generation is **skipped entirely** — the container uses those files as-is.
This lets you mount your own CA-signed, Let's Encrypt, or corporate certificates without
any special flags.

```bash
docker run -d -p 8443:8000 --name proxbox-api-nginx \
  -v ./certs:/certs:ro \
  emersonfelipesp/proxbox-api:latest-nginx
```

The `./certs` directory must contain at minimum:

| File | Description |
|------|-------------|
| `cert.pem` | PEM-encoded certificate (may be a full chain) |
| `key.pem` | PEM-encoded private key (PKCS#1 or PKCS#8) |

Docker Compose example:

```yaml
services:
  proxbox-api:
    image: emersonfelipesp/proxbox-api:latest-nginx
    container_name: proxbox-api
    restart: unless-stopped
    ports:
      - "8443:8000"
    volumes:
      - ./certs:/certs:ro
```

#### Granian image and PKCS#8 keys

Granian's TLS layer requires the private key in **PKCS#8 format**.
The entrypoint handles this automatically:

1. If `key-pkcs8.pem` is present in the mounted directory → use it directly.
2. If `key-pkcs8.pem` is absent and the directory is **writable** → convert `key.pem`
   in place and write `key-pkcs8.pem` there.
3. If `key-pkcs8.pem` is absent and the directory is **read-only** → convert and write
   to `/tmp/key-pkcs8.pem` (ephemeral; not persisted across container restarts).

To pre-convert your key and avoid the `/tmp` fallback:

```bash
openssl pkcs8 -topk8 -nocrypt -in ./certs/key.pem -out ./certs/key-pkcs8.pem
```

Then mount the directory containing all three files (`cert.pem`, `key.pem`, `key-pkcs8.pem`).

### Binding to IPv6 / dual-stack

To listen on both IPv4 and IPv6, set `PROXBOX_BIND_HOST=::`:

```bash
docker run -d -p 8000:8000 -e PROXBOX_BIND_HOST=:: \
  emersonfelipesp/proxbox-api:latest
```

#### Docker Compose quoting caveat

In Compose `environment:` **list-form**, values are taken verbatim — the quotes are NOT stripped — so `- PROXBOX_BIND_HOST="::"` ends up as the literal 4-character string `"::"` inside the container, which used to crash binding with `[Errno -2] Name does not resolve`. The container now sanitizes surrounding quotes defensively, but the recommended forms are:

```yaml
environment:
  - PROXBOX_BIND_HOST=::          # list-form: NO quotes
```

```yaml
environment:
  PROXBOX_BIND_HOST: "::"         # map-form: YAML strips the quotes
```

### Build from source

```bash
git clone https://github.com/emersonfelipesp/proxbox-api.git
cd proxbox-api

docker build -t proxbox-api:raw .                          # raw (default)
docker build --target nginx -t proxbox-api:nginx .         # nginx
docker build --target granian -t proxbox-api:granian .     # granian
```

## Option 2: PyPI

The package is published to [PyPI](https://pypi.org/project/proxbox-api/) as `proxbox-api`.

```bash
pip install proxbox-api
```

Or with `uv`:

```bash
uv add proxbox-api
```

### Optional Rust reconciliation engine

VM reconciliation uses the Python engine by default. An optional native package,
`proxbox-reconcile-rs`, can be installed for compare-mode validation or explicit Rust-engine
testing after wheels are published.

Once the optional package is published, install with:

```bash
pip install proxbox-api[rust]
```

Until the native package is published, install it from a local checkout:

```bash
uv sync --extra test --group dev
uv pip install -e proxbox-reconcile-rs
```

Enable compare mode first from NetBox's `/plugins/proxbox/settings/` page, or use
the environment override for a one-off process:

```bash
PROXBOX_RECONCILIATION_ENGINE=compare uv run fastapi run proxbox_api.main:app
```

The production default remains Python. To roll back immediately, set
`reconciliation_engine` back to `python` in NetBox, or unset
`PROXBOX_RECONCILIATION_ENGINE` if an env override was used.

Start the server:

```bash
python -m uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000
```

## Option 3: Local development from source

Clone repository:

```bash
git clone https://github.com/emersonfelipesp/proxbox-api.git
cd proxbox-api
```

Install runtime dependencies:

```bash
pip install -e .
```

Or use `uv`:

```bash
uv sync
```

Install the optional Rust reconciliation package for local parity testing:

```bash
uv pip install -e proxbox-reconcile-rs
```

Run API:

```bash
uv run fastapi run proxbox_api.main:app --host 0.0.0.0 --port 8000
```

Alternative with uvicorn:

```bash
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000 --reload
```

`fastapi run` does not expose TLS flags; for HTTPS from the app process, use **uvicorn** with `--ssl-certfile` / `--ssl-keyfile` below, or put **nginx/Caddy** in front (recommended for real certificates).

## TLS without Docker

### Local certificates (mkcert)

For trusted HTTPS on your own machine only:

```bash
mkcert -install
mkcert proxbox.backend.local localhost 127.0.0.1 ::1
uv run uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000 --reload \
  --ssl-keyfile=./proxbox.backend.local+3-key.pem \
  --ssl-certfile=./proxbox.backend.local+3.pem
```

Adjust file names to match what `mkcert` created.

### Publicly trusted or corporate certificates

**Recommended:** terminate TLS in **nginx** or **Caddy**, run the API on **HTTP** on `127.0.0.1:8000`:

```bash
uv run uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000
```

Point the proxy at `http://127.0.0.1:8000` and set `ssl_certificate` / `ssl_certificate_key` to your PEM paths (for Let's Encrypt: `fullchain.pem` and `privkey.pem` under `/etc/letsencrypt/live/<domain>/`). Set `X-Forwarded-Proto` and related headers so the app sees the original scheme. See the repository **README** for a complete nginx `server` example.

**Direct uvicorn TLS** (small deployments): use the **full certificate chain** as `--ssl-certfile` and the private key as `--ssl-keyfile`:

```bash
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8443 \
  --ssl-certfile=/etc/letsencrypt/live/api.example.com/fullchain.pem \
  --ssl-keyfile=/etc/letsencrypt/live/api.example.com/privkey.pem
```

Ensure the process user can read those files, and renew/reload after certificate updates.

## Verify installation

Open:

- Root: <http://127.0.0.1:8000/>
- Swagger: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>
