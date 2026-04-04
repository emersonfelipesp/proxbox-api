# proxmox-mock-api

Standalone schema-driven FastAPI mock service for the generated Proxmox API.

## Run

```bash
uv run proxmox-mock-api
```

Or with uvicorn:

```bash
uv run uvicorn proxmox_mock.main:app --host 0.0.0.0 --port 8000 --reload
```

## Test

```bash
uv run pytest tests
```
