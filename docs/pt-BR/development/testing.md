# Testes

## Stack de testes

- `pytest`
- `httpx`
- FastAPI `TestClient`
- `proxmox-sdk` (testes com mock)

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

## Testes com Mock Proxmox

Todos os testes usam recursos mock do `proxmox-sdk` para validar a integracao com a API Proxmox. A suite de testes suporta tres modos mock:

### Tres Modos Mock

| Modo | Fixture | Porta | Velocidade | Caso de Uso |
|------|---------|-------|------------|-------------|
| **MockBackend** | `proxmox_mock_backend` | N/A | Mais rapido | Iteracao de desenvolvimento |
| **HTTP Published** | `proxmox_mock_http_published` | 8006 | Rapido | Validacao de experiencia do usuario |
| **HTTP Local** | `proxmox_mock_http_local` | 8007 | Rapido | Validacao pre-lancamento |

### 1. MockBackend In-process (`@pytest.mark.mock_backend`)

O modo mais rapido usando `proxmox_sdk.sdk.backends.mock.MockBackend`. Nao requer servidor HTTP.

```python
@pytest.mark.mock_backend
async def test_vm_sync(proxmox_mock_backend):
    """Teste rapido usando MockBackend in-process."""
    vms = await proxmox_mock_backend.request("GET", "/api2/json/nodes/pve01/qemu")
```

### 2. Container HTTP Published (`@pytest.mark.mock_http`)

Usa a imagem Docker publicada `emersonfelipesp/proxmox-sdk:latest` na porta 8006.

```python
@pytest.mark.mock_http
async def test_vm_sync_http(proxmox_mock_http_published):
    """Teste realista usando container HTTP (imagem publicada)."""
    vms = await proxmox_mock_http_published.nodes.get()
```

### 3. Container HTTP Local (`@pytest.mark.mock_http`)

Usa uma imagem Docker construida localmente de `./proxmox-sdk` na porta 8007.

```python
@pytest.mark.mock_http
async def test_vm_sync_http_local(proxmox_mock_http_local):
    """Teste realista usando container HTTP (build local)."""
    vms = await proxmox_mock_http_local.nodes.get()
```

### Executando Testes com Containers Docker

Inicie ambos os containers mock:

```bash
# Usando docker compose (v2)
docker compose up -d

# Ou usando docker-compose (v1)
docker-compose up -d
```

Execute a suite completa de testes com o script de orquestracao:

```bash
./scripts/test-with-mock.sh
```

Execute modos de teste especificos:

```bash
# Apenas MockBackend (rapido, sem HTTP)
PROXMOX_API_MODE=mock uv run pytest tests --ignore=tests/e2e -m "mock_backend"

# Apenas HTTP published
PROXMOX_API_MODE=mock PROXMOX_MOCK_PUBLISHED_URL=http://localhost:8006 \
  uv run pytest tests --ignore=tests/e2e -m "mock_http"

# Apenas HTTP local
PROXMOX_API_MODE=mock PROXMOX_MOCK_LOCAL_URL=http://localhost:8007 \
  uv run pytest tests --ignore=tests/e2e -m "mock_http"
```

### Teste em Modo Dual

Testes podem executar contra MockBackend E containers HTTP:

```python
@pytest.mark.mock_backend
@pytest.mark.mock_http
async def test_vm_sync_all_modes(request, proxmox_mock_backend,
                                   proxmox_mock_http_published,
                                   proxmox_mock_http_local):
    """Teste executa 3 vezes: backend, HTTP published, e HTTP local."""
    ...
```

### Servicos Docker Compose

O `docker-compose.yml` define dois servicos:

```yaml
services:
  proxmox-mock-published:
    image: emersonfelipesp/proxmox-sdk:latest
    ports:
      - "8006:8000"
    environment:
      - PROXMOX_API_MODE=mock
      - PROXMOX_MOCK_SCHEMA_VERSION=latest

  proxmox-mock-local:
    build:
      context: ./proxmox-sdk
      dockerfile: Dockerfile
    ports:
      - "8007:8000"
    environment:
      - PROXMOX_API_MODE=mock
      - PROXMOX_MOCK_SCHEMA_VERSION=latest
```

Ambos os servicos:
- Usam `PROXMOX_API_MODE=mock`
- Definem `PROXMOX_MOCK_SCHEMA_VERSION=latest`
- Tem health checks habilitados
- Nao tem volumes persistentes (estado fresco a cada reinicio)

### Variaveis de Ambiente

| Variavel | Padrao | Descricao |
|----------|--------|-----------|
| `PROXMOX_API_MODE` | `real` | Defina como `mock` para modo teste |
| `PROXMOX_MOCK_PUBLISHED_URL` | `http://localhost:8006` | URL do container published |
| `PROXMOX_MOCK_LOCAL_URL` | `http://localhost:8007` | URL do container local |
| `PYTEST_CURRENT_TEST` | auto-definido | Auto-detecta ambiente pytest |

### Marcadores pytest

| Marcador | Descricao |
|----------|-----------|
| `mock_backend` | Testes usando MockBackend in-process (rapido) |
| `mock_http` | Testes usando container HTTP mock |

## Testes direcionados de endpoints

O arquivo `tests/test_endpoint_crud.py` cobre:

- Ciclo completo de CRUD de endpoint Proxmox.
- Regras de validacao de autenticacao de endpoint Proxmox.
- Regra singleton para endpoint NetBox.

Executar apenas este arquivo:

```bash
pytest tests/test_endpoint_crud.py
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

## Solucao de Problemas

### Container Nao Inicia

```bash
# Ver logs do container
docker compose logs proxmox-mock-published
docker compose logs proxmox-mock-local

# Verificar status de health
docker compose ps

# Reiniciar containers
docker compose restart
```

### Testes Timeout

```bash
# Aumente o timeout de health check em docker-compose.yml
# Ou aguarde mais antes de executar testes:
sleep 30
```

### Erros de Import

```bash
# Garanta que dependencias estao instaladas
uv sync --frozen --extra test --group dev

# Verificar imports
uv run python -c "from proxbox_api.testing import MockProxmoxContext"
```
