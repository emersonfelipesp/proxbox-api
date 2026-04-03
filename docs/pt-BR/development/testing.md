# Testes

## Stack de testes

- `pytest`
- `httpx`
- FastAPI `TestClient`

As dependencias de teste sao definidas em `pyproject.toml` em
`[project.optional-dependencies] -> test`.

Instale com:

```bash
uv sync --extra test --group dev
```

## Executar testes

```bash
pytest
```

Para o caminho especifico de geracao de schema e helpers tipados:

```bash
pytest tests/test_pydantic_generator_models.py tests/test_session_and_helpers.py
```

Para a superficie atual de rotas e contratos de docs:

```bash
pytest tests/test_generated_proxmox_routes.py tests/test_proxmox_codegen_docs.py tests/test_api_routes.py tests/test_stub_routes.py tests/test_admin_logs.py
```

## Testes direcionados de endpoints

O arquivo `proxbox_api/test_endpoint_crud.py` cobre:

- Ciclo completo de CRUD de endpoint Proxmox.
- Regras de validacao de autenticacao de endpoint Proxmox.
- Regra singleton para endpoint NetBox.

Executar apenas este arquivo:

```bash
pytest proxbox_api/test_endpoint_crud.py
```

## Verificacao de compilacao

```bash
python -m compileall proxbox_api
```

## Checks recomendados antes de PR

- `pytest`
- `python -m compileall proxbox_api`
- `mkdocs build --strict` (quando docs mudarem)

## Checks de contrato Proxmox gerado

Ao alterar `proxbox_api/proxmox_codegen/` ou a camada de servico Proxmox voltada para sync:

- Regenere `proxbox_api/generated/proxmox/*/pydantic_models.py` a partir dos artefatos `openapi.json` versionados.
- Confirme que respostas array-de-objetos continuam emitindo schemas concretos `...ResponseItem`.
- Confirme que rotas com helpers continuam retornando payloads compativeis com o codigo de sync existente.
