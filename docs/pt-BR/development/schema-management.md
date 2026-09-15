# Gerenciamento de Schemas Proxmox

O `proxbox-api` inclui schemas OpenAPI do Proxmox pre-gerados para as versoes
estaveis recentes do PVE. Esses schemas alimentam as rotas proxy geradas em
runtime em `/proxmox/api2/*`. A geracao em runtime e desabilitada por padrao.
Defina `PROXBOX_RUNTIME_CODEGEN_ENABLED=true` somente em desenvolvimento quando
for necessario gerar via HTTP, atualizar rotas ou descobrir schemas de usuario.

## Schemas incluidos

As seguintes versoes sao incluidas com o pacote em `proxbox_api/generated/proxmox/`:

| Tag de versao | Versao Proxmox |
|---------------|----------------|
| `8.1`         | PVE 8.1.x      |
| `8.2`         | PVE 8.2.x      |
| `8.3`         | PVE 8.3.x      |
| `latest`      | Snapshot atual do API Viewer |

As tags incluidas no pacote sao imutaveis. Com a configuracao padrao, o startup
e a renderizacao de schemas usam somente os schemas do pacote e nunca examinam o
diretorio de schemas de usuario. A unica forma suportada de atualizar uma tag
incluida, inclusive `latest`, e instalar outro pacote que contenha o schema
novo. O log confirma quais versoes incluidas foram encontradas:

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

Se a versao conectada nao tiver schema correspondente, a resposta padrao
informa que a geracao em runtime esta desabilitada. Com o opt-in exclusivo para
desenvolvimento, a geracao pode iniciar automaticamente em segundo plano:

```json
{
  "schema_status": {
    "status": "generating",
    "version_tag": "8.4",
    "message": "No bundled schema found for Proxmox 8.4. Background generation started. This may take several minutes."
  }
}
```

As rotas para a nova versao sao registradas quando a geracao termina somente se
`PROXBOX_RUNTIME_CODEGEN_ENABLED=true`.

## CLI: `proxbox-schema`

O comando `proxbox-schema` e a forma recomendada de gerenciar schemas manualmente.

### Listar versoes disponiveis

```bash
proxbox-schema list
```

Saida:

```text
Available Proxmox OpenAPI schema versions (4):
         8.1   6.4 MB   [bundled]   /opt/proxbox_api/generated/proxmox/8.1/openapi.json
         8.2   6.4 MB   [bundled]   /opt/proxbox_api/generated/proxmox/8.2/openapi.json
         8.3   6.4 MB   [bundled]   /opt/proxbox_api/generated/proxmox/8.3/openapi.json
      latest   7.3 MB   [bundled]   /opt/proxbox_api/generated/proxmox/latest/openapi.json
```

`list` e `status` inspecionam somente os schemas incluidos no pacote por padrao
e nao acessam o diretorio gerado pelo usuario. Em um processo de desenvolvimento
com opt-in, adicione `--include-user` a qualquer um dos comandos para incluir
artefatos de usuario com proveniencia verificada. A saida identifica esses
artefatos como `user-generated`. A flag e rejeitada a menos que
`PROXBOX_RUNTIME_CODEGEN_ENABLED=true`.

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

Isso percorre o Proxmox API Viewer oficial, analisa todos os endpoints e escreve
os artefatos no diretorio de schemas gerados pelo usuario. O padrao e
`$XDG_DATA_HOME/proxbox/generated/proxmox` ou
`~/.local/share/proxbox/generated/proxmox` quando `XDG_DATA_HOME` nao esta
definido. O comando imprime o progresso e um resumo de conclusao:

```
Generating Proxmox OpenAPI schema for version '8.4'...
Output directory: /var/lib/proxbox/generated/proxmox/8.4
Source URL: https://pve.proxmox.com/pve-docs/api-viewer/
Workers: 10

This may take several minutes. The pipeline crawls the Proxmox API Viewer,
parses all endpoints, and generates OpenAPI + Pydantic artifacts.

Generation completed for Proxmox 8.4
  Endpoints:  493
  Operations: 1284
  Duration:   187.3s
  Output:     /var/lib/proxbox/generated/proxmox/8.4

Schema is ready for offline inspection.
Start the development app with PROXBOX_RUNTIME_CODEGEN_ENABLED=true to discover it.
```

Em uma instancia de desenvolvimento com opt-in, registre as novas rotas sem reiniciar:

```bash
curl -s -X POST http://localhost:8800/proxmox/viewer/routes/refresh \
  -H "X-Proxbox-API-Key: SUA_CHAVE"
```

Cada diretorio de versao persistido contem `openapi.json`, a renderizacao
offline `pydantic_models.py`, a captura bruta e `provenance.json`. O sidecar de
proveniencia registra `source_url`, `generated_at` e o digest SHA-256 dos bytes
exatos de `openapi.json`. A descoberta em runtime ignora o artefato de usuario
quando o sidecar esta ausente ou o digest diverge. Esse sidecar detecta
corrupcao; ele nao autentica o artefato, pois um processo com o mesmo usuario do
sistema operacional pode substituir o documento e forjar seu digest. Por isso,
a producao mantem a geracao em runtime desabilitada.

#### Regenerar um schema de usuario existente

```bash
proxbox-schema generate 8.4 --force
```

Sem `--force`, o comando encerra cedo quando um schema de usuario ja existe.
Tags incluidas no pacote nao podem ser regeneradas nem sobrepostas, mesmo com
`--force`; escolha uma nova tag.

#### Diretorio de saida personalizado

```bash
proxbox-schema generate 8.4 --output-dir /data/proxmox-schemas
```

Defina `PROXBOX_GENERATED_DIR=/data/proxmox-schemas` e
`PROXBOX_RUNTIME_CODEGEN_ENABLED=true` em um aplicativo de desenvolvimento
quando esse diretorio deva ser a raiz descoberta de schemas de usuario.
Informar apenas `--output-dir` nao altera a raiz configurada do aplicativo.

#### Gerar a partir de uma origem nao padrao

```bash
proxbox-schema generate review-8.4 \
  --source-url https://schemas.example.net/api-viewer/ \
  --output-dir /data/proxmox-schemas
```

Artefatos de qualquer `source_url` nao padrao sao armazenados em
`/data/proxmox-schemas/custom/review-8.4/`. Eles servem somente para inspecao e
nao podem ser descobertos nem registrados como rotas proxy em runtime.

### Colocar artefatos legados em quarentena

```bash
proxbox-schema quarantine-legacy
```

Essa etapa de upgrade idempotente usa um lock entre processos e movimentos sem
seguir links nem sobrescrever destinos para colocar em quarentena todo
`pydantic_models.py`, cache de rotas invalido e sidecar de proveniencia orfao no
diretorio de usuario. O startup do lifespan executa a mesma etapa antes do
registro das rotas.

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

Os endpoints HTTP de geracao e atualizacao sao exclusivos para desenvolvimento.
Eles retornam HTTP 404 porque nao existem na tabela de rotas, a menos que o
processo inicie com `PROXBOX_RUNTIME_CODEGEN_ENABLED=true`.

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

O valor da query `version_tag` deve corresponder a
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` e nao pode ser `.` nem `..`. Valores
invalidos retornam HTTP 422 antes que o crawler seja iniciado ou que qualquer
diretorio seja criado. O pipeline aplica a mesma validacao a chamadas diretas
por Python e pela CLI e resolve cada checkpoint e artefato gerado dentro do
diretorio de saida configurado.

Quando `persist=true`, uma tag existente no pacote retorna HTTP 409 com a
orientacao de escolher outra tag ou usar `persist=false`. Um `source_url` nao
padrao persiste somente em `custom/<version_tag>/`; a resposta identifica o
resultado como exclusivo para inspecao.

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

## Carregamento de modelos em runtime

O registro de rotas em runtime constroi os modelos de request e response
diretamente a partir do documento OpenAPI analisado com
`pydantic.create_model`. Ele nao avalia o arquivo de codigo-fonte gerado
`pydantic_models.py`. Nomes de propriedades JSON e descricoes de campos
permanecem dados fornecidos aos campos do Pydantic, inclusive quando o atributo
Python precisa ser normalizado e o alias JSON original deve ser preservado.

`GET /proxmox/viewer/pydantic` renderiza o codigo-fonte a partir de um documento
OpenAPI incluido, analisado e validado por padrao. Com o opt-in de
desenvolvimento, tambem pode renderizar um documento de usuario admitido pela
proveniencia. A renderizacao ocorre fora do event loop, usa cache pelo digest
verificado do schema, tem limite de 2 MiB e aceita seis requisicoes por minuto
por origem. A rota nunca le codigo-fonte Python persistido. O renderer offline
valida identificadores de classes e campos, rejeita colisoes de nomes
normalizados e nomes reservados do Pydantic e usa representacoes de literais
Python para aliases, descricoes e valores padrao.

Antes da persistencia, restauracao do cache ou construcao dos modelos, o
documento fica limitado a 8 MiB, profundidade de schema 32, 4.096 paths, 16.384
operacoes, 512 propriedades por schema, 8.192 modelos gerados e 4.096 caracteres
para titulos, descricoes e valores enum string. Um cache rejeitado nunca
substitui o conjunto last-known-good ja montado; o startup usa os artefatos
autoritativos. O registro tambem rejeita mais de 8 versoes elegiveis, mais de 32
MiB de OpenAPI agregado, mais de 16.384 modelos agregados ou mais de 32.768
rotas agregadas antes de construir modelos ou o cache.
