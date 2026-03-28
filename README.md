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

### Pull the docker image

```
docker pull emersonfelipesp/proxbox-api:latest
```

### Run the container
```
docker run -d -p 8000:8000 --name proxbox-api emersonfelipesp/proxbox-api:latest
```

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

## Using mkcert

Install the local CA in the system trust store.

```
mkcert -install
mkcert proxbox.backend.local localhost 127.0.0.1 ::1
```

With the keyfile and certfile generated, pass it on uvicorn command to start up FastAPI

### NGINX

```
sudo cp nginx.conf /etc/nginx/sites-available/proxbox
sudo ln -s -f /etc/nginx/sites-available/proxbox /etc/nginx/sites-enabled/proxbox
sudo systemctl restart nginx
```

```
/opt/netbox/venv/bin/uvicorn netbox-proxbox.proxbox_api.proxbox_api.main:app --host 0.0.0.0 --port 8000 --app-dir /opt/netbox/netbox --ssl-keyfile=localhost+2-key.pem --ssl-certfile=localhost+2.pem
```

Or 

```
cd /opt/netbox/netbox/netbox-proxbox/proxbox_api
uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000 --reload --ssl-keyfile=./proxbox_api/proxbox.backend.local+3-key.pem --ssl-certfile=./proxbox_api/proxbox.backend.local+3.pem
```
