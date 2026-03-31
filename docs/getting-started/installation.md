# Installation

This page documents supported ways to run `proxbox-api`.

## Requirements

- Python 3.11+
- `uv` (recommended) or `pip`
- Network access to NetBox and Proxmox targets

## Option 1: Docker (recommended for quick start)

Pull image:

```bash
docker pull emersonfelipesp/proxbox-api:latest
```

Run container:

```bash
docker run -d -p 8000:8000 --name proxbox-api emersonfelipesp/proxbox-api:latest
```

Service URL:

- <http://127.0.0.1:8000>

## Option 2: Local development from source

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

Point the proxy at `http://127.0.0.1:8000` and set `ssl_certificate` / `ssl_certificate_key` to your PEM paths (for Let’s Encrypt: `fullchain.pem` and `privkey.pem` under `/etc/letsencrypt/live/<domain>/`). Set `X-Forwarded-Proto` and related headers so the app sees the original scheme. See the repository **README** for a complete nginx `server` example.

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
