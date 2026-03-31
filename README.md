# Installing proxbox-api (Plugin backend made using FastAPI)

## Tooling: uv + Ruff

This repo uses [uv](https://docs.astral.sh/uv/) to install Python and dependencies, and [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

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

The image is built from this repository with **uv** and **`uv.lock`** (multi-stage Dockerfile): dependencies install in a builder stage, then only `.venv` and `proxbox_api/` are copied into the runtime image.

**Reverse proxy:** both the default and mkcert images run **[nginx](https://nginx.org/)** in front of the app. **nginx** listens on **`PORT`** (default **8000**); **uvicorn** listens only on **`127.0.0.1:8001`** inside the container. **[supervisord](http://supervisord.org/)** keeps both processes running. Map your host port to **`PORT`** (for example `-p 8800:8000`). The bundled nginx config turns **`proxy_buffering`** off (and disables **gzip** for that location) so **chunked and streaming responses** (including SSE) are forwarded without being fully buffered first.

### Build from source (this repo)

```bash
docker build -t proxbox-api:local .
docker run -d -p 8000:8000 --name proxbox-api proxbox-api:local
```

### Pull the docker image

```
docker pull emersonfelipesp/proxbox-api:latest
```

### Run the container
```
docker run -d -p 8000:8000 --name proxbox-api emersonfelipesp/proxbox-api:latest
```

### HTTPS image (mkcert)

There is a second image variant where **nginx terminates HTTPS** using certificates from [mkcert](https://github.com/FiloSottile/mkcert) and still proxies to **uvicorn** on `127.0.0.1:8001`. It is published next to the default image:

- `emersonfelipesp/proxbox-api:<version>-mkcert` (for example `0.0.4-mkcert`)
- `emersonfelipesp/proxbox-api:latest-mkcert`

**Defaults:** the certificate always includes **`localhost`** and **`127.0.0.1`**. You can add more names or IPs with **`MKCERT_EXTRA_NAMES`** (commas and/or spaces), for example `proxbox.lan,10.0.0.5`.

**Optional:** set **`CAROOT`** to a mounted directory so the same local CA is reused across container restarts (then install that root CA on your workstation if you want the browser to trust the cert).

```bash
docker pull emersonfelipesp/proxbox-api:latest-mkcert

docker run -d -p 8443:8000 --name proxbox-api-tls \
  -e MKCERT_EXTRA_NAMES='myhost.local,192.168.1.10' \
  emersonfelipesp/proxbox-api:latest-mkcert
```

Build the mkcert target from this repository:

```bash
docker build --target mkcert -t proxbox-api:local-mkcert .
docker run -d -p 8443:8000 proxbox-api:local-mkcert
```

To run a shell or tests instead of starting nginx+uvicorn, pass a command (the entrypoint delegates to it), for example: `docker run --rm … emersonfelipesp/proxbox-api:latest-mkcert sh -c "mkcert -help"`.

## Using git repository

### Clone the repository

```
git clone https://github.com/netdevopsbr/netbox-proxbox.git
```

### Change to 'proxbox_api' project root folder

```
cd proxbox_api 
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
