# Gerenciamento de Schemas Proxmox

O `proxbox-api` inclui schemas OpenAPI do Proxmox pré-gerados para as três ultimas versoes estaveis do PVE. Esses schemas alimentam as rotas proxy geradas em tempo de execucao em `/proxmox/api2/*`. Esta pagina explica como listar, verificar e gerar schemas pela linha de comando ou pela API HTTP.

## Schemas incluidos

As seguintes versoes sao incluidas com o pacote em `proxbox_api/generated/proxmox/`:

| Tag de versao | Versao Proxmox |
|---------------|----------------|
| `8.1`         | PVE 8.1.x      |
| `8.2`         | PVE 8.2.x      |
| `8.3`         | PVE 8.3.x      |
| `latest`      | Snapshot atual do API Viewer |

Na inicializacao, o app carrega todos os diretorios de versao disponiveis e registra rotas para cada um. O log de inicializacao confirma quais versoes foram encontradas:

```
[INFO] Bundled Proxmox OpenAPI schema versions available: 8.1, 8.2, 8.3, latest
```

## Deteccao automatica de versao

Ao chamar `GET /proxmox/sessions`, o app verifica a versao do cluster Proxmox conectado em relacao aos schemas incluidos. Cada entrada de sessao na resposta inclui um campo `schema_status`:

```json
{
  "name": "pve-cluster",
  "proxmox_version": {"version": "8.3.2", "release": "8.3", "repoid": "abc123"},
  "schema_release": "8.3",
  "schema_status": {"status": "available", "version_tag": "8.3"}
}
```

Se a versao conectada nao tiver schema correspondente (por exemplo, um cluster futuro PVE 8.4), a geracao inicia automaticamente em segundo plano:

```json
{
  "schema_status": {
    "status": "generating",
    "version_tag": "8.4",
    "message": "No bundled schema found for Proxmox 8.4. Background generation started. This may take several minutes."
  }
}
```

As rotas para a nova versao sao registradas em tempo de execucao assim que a geracao termina — sem necessidade de reiniciar.

## CLI: `proxbox-schema`

O comando `proxbox-schema` e a forma recomendada de gerenciar schemas manualmente.

### Listar versoes disponiveis

```bash
proxbox-schema list
```

Saida:

```text
Available Proxmox OpenAPI schema versions (4):
         8.1   6.4 MB   /opt/proxbox_api/generated/proxmox/8.1/openapi.json
         8.2   6.4 MB   /opt/proxbox_api/generated/proxmox/8.2/openapi.json
         8.3   6.4 MB   /opt/proxbox_api/generated/proxmox/8.3/openapi.json
      latest   7.3 MB   /opt/proxbox_api/generated/proxmox/latest/openapi.json
```

### Verificar status

```bash
proxbox-schema status
```

Mostra versoes disponiveis e quaisquer tarefas de geracao ativas ou concluidas recentemente:

```
Bundled versions: 8.1, 8.2, 8.3, latest
No active or recent generation tasks.
```

### Gerar um schema

```bash
proxbox-schema generate 8.4
```

Isso percorre o Proxmox API Viewer, analisa todos os endpoints e escreve os artefatos gerados em `proxbox_api/generated/proxmox/8.4/`. O comando imprime o progresso e um resumo de conclusao:

```
Generating Proxmox OpenAPI schema for version '8.4'...
Output directory: proxbox_api/generated/proxmox/8.4
Source URL: https://pve.proxmox.com/pve-docs/api-viewer/
Workers: 10

This may take several minutes. The pipeline crawls the Proxmox API Viewer,
parses all endpoints, and generates OpenAPI + Pydantic artifacts.

Generation completed for Proxmox 8.4
  Endpoints:  493
  Operations: 1284
  Duration:   187.3s
  Output:     proxbox_api/generated/proxmox/8.4

Schema is ready. Restart the app or call POST /proxmox/viewer/routes/refresh
to register the new routes at runtime.
```

Apos a geracao, registre as novas rotas sem reiniciar:

```bash
curl -s -X POST http://localhost:8800/proxmox/viewer/routes/refresh \
  -H "X-Proxbox-API-Key: SUA_CHAVE"
```

#### Regenerar um schema existente

```bash
proxbox-schema generate 8.3 --force
```

Sem `--force`, o comando encerra cedo quando um schema ja existe.

#### Diretorio de saida personalizado

```bash
proxbox-schema generate 8.4 --output-dir /data/proxmox-schemas
```

O app carrega schemas apenas de `proxbox_api/generated/proxmox/` por padrao.

#### Ajustar desempenho do rastreamento

```bash
proxbox-schema generate 8.4 --workers 5 --retry-count 3 --retry-backoff 0.5
```

| Flag | Padrao | Descricao |
|------|--------|-----------|
| `--workers` | `10` | Numero de workers do Playwright |
| `--retry-count` | `2` | Tentativas por endpoint em falhas transitorias |
| `--retry-backoff` | `0.35` | Backoff exponencial base em segundos |
| `--checkpoint-every` | `50` | Escrever checkpoint a cada N endpoints |

## API HTTP

### Verificar status do schema

```http
GET /proxmox/viewer/schema-status
```

Resposta:

```json
{
  "available_versions": ["8.1", "8.2", "8.3", "latest"],
  "generation_tasks": {}
}
```

Verificar uma versao especifica:

```http
GET /proxmox/viewer/schema-status?version_tag=8.4
```

Resposta enquanto a geracao esta em andamento:

```json
{
  "version_tag": "8.4",
  "schema_available": false,
  "generation": {"status": "running", "error": null}
}
```

Valores possiveis de `status`: `pending`, `running`, `completed`, `failed`.

### Acionar geracao

```http
POST /proxmox/viewer/generate?version_tag=8.4
```

Esta e uma requisicao sincrona de longa duracao. Para geracao em segundo plano, prefira `proxbox-schema generate` ou deixe a deteccao automatica acionar via `GET /proxmox/sessions`.

### Atualizar rotas em tempo de execucao

Apos gerar um novo schema, registre suas rotas sem reiniciar:

```http
POST /proxmox/viewer/routes/refresh
```

Ou para uma versao especifica:

```http
POST /proxmox/viewer/routes/refresh?version_tag=8.4
```

## Requisitos

A geracao de schemas usa o [Playwright](https://playwright.dev/python/) para percorrer o Proxmox API Viewer. Instale o extra:

```bash
pip install proxbox_api[playwright]
playwright install chromium
```

Sem o Playwright, o pipeline usa o parser `apidoc.js` como fallback.

## Convencao de nomenclatura de versao

As tags de versao usam o formato `major.minor` do campo `release` do Proxmox (por exemplo, `"8.3"` de `{"release": "8.3", "version": "8.3.2"}`). A tag `latest` e um alias especial para o snapshot mais recente do API Viewer oficial.
