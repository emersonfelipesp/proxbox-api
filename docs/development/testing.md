# Testing

## Test stack

- `pytest`
- `httpx`
- FastAPI `TestClient`

Test dependencies are defined in `pyproject.toml` under
`[project.optional-dependencies] -> test`.

Install them with:

```bash
uv sync --extra test --group dev
```

## Run tests

```bash
pytest
```

For the schema-generation and typed helper path specifically:

```bash
pytest tests/test_pydantic_generator_models.py tests/test_session_and_helpers.py
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

## Generated Proxmox contract checks

When changing `proxbox_api/proxmox_codegen/` or the sync-facing Proxmox service layer:

- Regenerate `proxbox_api/generated/proxmox/*/pydantic_models.py` from the checked-in `openapi.json` artifacts.
- Confirm array-of-object responses still emit concrete `...ResponseItem` schemas.
- Confirm helper-backed routes still return payloads compatible with existing sync code.
