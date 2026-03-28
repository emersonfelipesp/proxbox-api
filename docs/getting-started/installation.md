# Installation

This page documents supported ways to run `proxbox-api`.

## Requirements

- Python 3.10+
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
uv run fastapi run --host 0.0.0.0 --port 8000
```

Alternative with uvicorn:

```bash
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000 --reload
```

## TLS with local certificates

If you need HTTPS locally, generate a certificate with `mkcert` and pass it to uvicorn:

```bash
mkcert -install
mkcert proxbox.backend.local localhost 127.0.0.1 ::1
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000 --ssl-keyfile=./localhost+2-key.pem --ssl-certfile=./localhost+2.pem
```

## Verify installation

Open:

- Root: <http://127.0.0.1:8000/>
- Swagger: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>
