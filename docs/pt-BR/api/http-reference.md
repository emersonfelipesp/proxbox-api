# Referencia HTTP da API

Esta pagina resume os principais endpoints HTTP do `proxbox-api`.

Para schemas completos de requisicao/resposta, use OpenAPI em runtime em `/docs`.

## Root e utilitarios

- `GET /` - Metadados do servico e links.
- `GET /cache` - Inspecao do snapshot da cache em memoria.
- `GET /clear-cache` - Limpar cache em memoria.
- `GET /sync-processes` - Listar registros de processos de sincronizacao da API do plugin NetBox.
- `POST /sync-processes` - Criar um registro de processo de sincronizacao na API do plugin NetBox.

## Rotas NetBox (`/netbox`)

- `POST /netbox/endpoint` - Criar endpoint singleton do NetBox.
- `GET /netbox/endpoint` - Listar registros de endpoints NetBox.
- `GET /netbox/endpoint/{netbox_id}` - Obter endpoint por ID.
- `PUT /netbox/endpoint/{netbox_id}` - Atualizar endpoint.
- `DELETE /netbox/endpoint/{netbox_id}` - Excluir endpoint.
- `GET /netbox/status` - Buscar status da API do NetBox.
- `GET /netbox/openapi` - Buscar OpenAPI do NetBox.

### Regra singleton do NetBox

Tentar criar um segundo endpoint retorna HTTP 400 com:

```json
{
  "detail": "Only one NetBox endpoint is allowed"
}
```

## Rotas Proxmox (`/proxmox`)

### CRUD de configuracao de endpoints

- `POST /proxmox/endpoints`
- `GET /proxmox/endpoints`
- `GET /proxmox/endpoints/{endpoint_id}`
- `PUT /proxmox/endpoints/{endpoint_id}`
- `DELETE /proxmox/endpoints/{endpoint_id}`

Regras de validacao:

- Requer `password` ou (`token_name` e `token_value`).
- `token_name` e `token_value` devem ser definidos juntos.
- Nomes de endpoint devem ser unicos.

### Sessao e descoberta

- `GET /proxmox/sessions`
- `GET /proxmox/version`
- `GET /proxmox/`
- `GET /proxmox/storage`
- `GET /proxmox/nodes/{node}/storage/{storage}/content`
- `GET /proxmox/{top_level}`
- `GET /proxmox/{node}/{type}/{vmid}/config`

### Dados de cluster e nodes

- `GET /proxmox/cluster/status`
- `GET /proxmox/cluster/resources`
- `GET /proxmox/nodes/`
- `GET /proxmox/nodes/{node}/network`
- `GET /proxmox/nodes/{node}/qemu`

### Geracao de codigo do viewer

- `POST /proxmox/viewer/generate`
- `GET /proxmox/viewer/openapi`
- `GET /proxmox/viewer/openapi/embedded`
- `GET /proxmox/viewer/integration/contracts`
- `GET /proxmox/viewer/pydantic`
- `POST /proxmox/viewer/routes/refresh`

### Rotas proxy geradas em runtime

`proxbox-api` monta rotas proxy do Proxmox geradas em runtime a partir do contrato OpenAPI embutido sob:

- `/proxmox/api2/{version_tag}/*`
- `/proxmox/api2/*` como alias de compatibilidade para `latest`

Comportamento:

- Rotas sao construidas na inicializacao para cada versao gerada presente em `proxbox_api/generated/proxmox/`.
- O conjunto de rotas montadas e armazenado em cache em `proxbox_api/generated/proxmox/runtime_generated_routes_cache.json`.
- No `uvicorn --reload`, a inicializacao prefere esse manifesto de cache para preservar o conjunto de rotas montado anteriormente em desenvolvimento.
- Rotas sao reconstruidas sob demanda com `POST /proxmox/viewer/routes/refresh`.
- `POST /proxmox/viewer/routes/refresh` sem parametros de consulta reconstrui todas as versoes disponiveis.
- `POST /proxmox/viewer/routes/refresh?version_tag=8.3.0` reconstrui apenas essa versao montada.
- O alias sem versao `/proxmox/api2/*` encaminha para o contrato `latest`.
- Corpos de requisicao e respostas sao validados com modelos Pydantic gerados em runtime.
- Rotas geradas aparecem no `/docs` e `/openapi.json` do FastAPI.
- Rotas `latest` sao montadas antes de tags de versao mais antigas para aparecerem primeiro no Swagger.
- Rotas geradas tem prioridade sobre rotas `proxmox/*` mais antigas, entao colisoes de caminho resolvem para a superficie da API gerada.

Normalizacao de parametros de caminho:

- Quando o viewer Proxmox usa nomes de parametros de caminho que nao sao identificadores validos do FastAPI, a rota FastAPI montada usa um nome de placeholder normalizado.
- Exemplo:
  - Caminho do contrato Proxmox: `/nodes/{node}/hardware/pci/{pci-id-or-mapping}`
  - Caminho FastAPI montado: `/proxmox/api2/latest/nodes/{node}/hardware/pci/{pci_id_or_mapping}`
- A chamada proxmoxer upstream ainda usa o nome original do parametro Proxmox do contrato OpenAPI gerado.

## Rotas DCIM (`/dcim`)

- `GET /dcim/devices`
- `GET /dcim/devices/create` - criar dispositivos NetBox a partir de nodes Proxmox (retorna JSON ao completar).
- `GET /dcim/devices/create/stream` - variante SSE streaming. Emite eventos `step` por dispositivo com progresso granular enquanto dispositivos sao criados.
- `GET /dcim/devices/{node}/interfaces/create`
- `GET /dcim/devices/interfaces/create`

## Rotas de Virtualizacao (`/virtualization`)

- `GET /virtualization/cluster-types/create` (placeholder)
- `GET /virtualization/clusters/create` (placeholder)
- `GET /virtualization/virtual-machines/create` - criar VMs NetBox a partir de recursos Proxmox (retorna JSON ao completar).
- `GET /virtualization/virtual-machines/create/stream` - variante SSE streaming. Emite eventos `step` por VM com progresso granular enquanto VMs sao criadas.
- `GET /virtualization/virtual-machines/`
- `GET /virtualization/virtual-machines/{id}`
- `GET /virtualization/virtual-machines/summary/example`
- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`
- `GET /virtualization/virtual-machines/backups/all/create/stream`
- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`
- `GET /virtualization/virtual-machines/storage/create`
- `GET /virtualization/virtual-machines/storage/create/stream`

## Atualizacao completa (`/full-update`)

- `GET /full-update` - executa sincronizacao de dispositivos depois de VMs, retorna resultado JSON combinado.
- `GET /full-update/stream` - variante SSE streaming. Emite eventos `step` por objeto para dispositivos e VMs durante a sincronizacao completa.

## Formato SSE streaming

Todos os endpoints `/stream` retornam `Content-Type: text/event-stream` e emitem tres tipos de eventos:

| Evento      | Descricao |
|-------------|-----------|
| `step`      | Frame de progresso. Contem `step` (tipo de objeto, ex. `device`, `virtual_machine`), `status` (`started`, `progress`, `completed`, `failed`), `message` (texto legivel), `rowid` (nome/ID do objeto), e `payload` (JSON original estilo websocket). |
| `error`     | Frame de erro. Contem `step`, `status: "failed"`, `error`, e `detail`. |
| `complete`  | Frame final. Contem `ok` (booleano), `message`, e opcionalmente `result` ou `errors`. |

Exemplo de evento `step` para um dispositivo:

```
event: step
data: {"step":"device","status":"progress","message":"Processing device pve01","rowid":"pve01","payload":{"object":"device","type":"create","data":{"rowid":"pve01","completed":false}}}
```

Headers:

- `Cache-Control: no-cache`
- `X-Accel-Buffering: no`

## Rotas Extras (`/extras`)

- `GET /extras/extras/custom-fields/create`

Este endpoint cria campos customizados esperados usados pela sincronizacao de metadados de VM.

## Rotas de configuracao do plugin Proxbox

Estes handlers de rota existem em `proxbox_api/routes/proxbox/__init__.py` mas nao estao atualmente montados em `main.py`:

- `GET /netbox/plugins-config`
- `GET /netbox/default-settings`
- `GET /settings`

Para monta-los, inclua o router na inicializacao do app se desejado.
