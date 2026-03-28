# Testing

## Test stack

- `pytest`
- `httpx`
- FastAPI `TestClient`

Dependencies are listed in `requirements-test.txt`.

## Run tests

```bash
pytest
```

## Targeted endpoint tests

The file `proxbox_api/test_endpoint_crud.py` includes dedicated coverage for:

- Proxmox endpoint CRUD lifecycle.
- Proxmox endpoint auth validation rules.
- NetBox endpoint singleton enforcement.

Run only this test file:

```bash
pytest proxbox_api/test_endpoint_crud.py
```

## Compile check

```bash
python -m compileall proxbox_api
```

## Recommended pre-PR checks

- `pytest`
- `python -m compileall proxbox_api`
- `mkdocs build --strict` (when docs changed)
