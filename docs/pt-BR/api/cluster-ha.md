# Referencia de HA do Cluster Proxmox

Endpoints somente leitura de Alta Disponibilidade do Proxmox, agregados entre todos os clusters configurados. Atendem a aba **HA** na pagina de detalhe da VM e a pagina **HA Status** no nivel de cluster, adicionadas pelo `netbox-proxbox` v0.0.15+ para a [issue #243](https://github.com/emersonfelipesp/netbox-proxbox/issues/243).

- **Disponivel desde:** proxbox-api `v0.0.11`. O floor correspondente do consumidor e `netbox-proxbox >= 0.0.15` — versoes anteriores do plugin nao chamam estes paths.
- **Mutacoes ficam fora do escopo intencionalmente.** Adicionar/remover recurso, migrar/realocar e CRUD de grupos HA nao sao expostos aqui e podem entrar em uma release seguinte.
- **Paths de origem no Proxmox:** `/cluster/ha/status/current`, `/cluster/ha/resources`, `/cluster/ha/groups`. O router agrega resultados de cada `ProxmoxSession` configurada no backend; uma unica requisicao gera uma chamada por cluster.

## Resumo dos endpoints

| Metodo | Path | Retorna | Observacoes |
|--------|------|---------|-------------|
| `GET`  | `/proxmox/cluster/ha/status` | `list[HaStatusItemSchema]` | Linhas por servico CRM/LRM mais entradas de quorum/master, vindas de `/cluster/ha/status/current`. Erros em um cluster geram uma unica linha sintetica com `status="error: ..."` e os outros clusters seguem agregando normalmente. |
| `GET`  | `/proxmox/cluster/ha/resources` | `list[HaResourceSchema]` | Recursos HA configurados, mesclados com o estado atual (node, CRM state, request state). Erros em um cluster sao registrados via `logger.exception` e aquele cluster contribui com zero linhas. |
| `GET`  | `/proxmox/cluster/ha/resources/by-vm/{vmid}` | `HaResourceSchema \| null` | Conveniencia para um VM/CT especifico. Tenta `vm:{vmid}` primeiro e cai para `ct:{vmid}`. Retorna `null` (nao 404) quando o guest nao esta sob HA, para que a aba do NetBox renderize estado vazio. |
| `GET`  | `/proxmox/cluster/ha/groups` | `list[HaGroupSchema]` | Lista de grupos HA com detalhe mesclado (nodes, restricted, nofailback). |
| `GET`  | `/proxmox/cluster/ha/groups/{group}` | `HaGroupSchema \| null` | Detalhe de um grupo unico; retorna `null` quando nenhum cluster possui o grupo. |
| `GET`  | `/proxmox/cluster/ha/summary` | `HaSummarySchema` | Envelope composto `{status, groups, resources}`. Chama os tres handlers em paralelo via `asyncio.gather` para que a pagina HA do cluster gere apenas um round-trip por render. |

Todos os endpoints exigem o header `X-Proxbox-API-Key`, como o restante do `proxbox-api`. Reaproveitam `ProxmoxSessionsDep` e o rate limit global.

## Schemas de resposta

Definidos em `proxbox_api/routes/proxmox/ha.py` e exportados como models Pydantic v2. Todos os campos sao opcionais porque o Proxmox mistura linhas por servico com linhas de cluster (quorum, master, lrm:&lt;node&gt;) e linhas de servico em estados diferentes omitem chaves diferentes.

### `HaStatusItemSchema`

```jsonc
{
  "cluster_name": "lab",      // injetado pelo proxbox-api para desambiguar linhas multi-cluster
  "id": null,                  // chave id bruta do Proxmox, quando presente
  "type": "service",           // "service" | "quorum" | "master" | "lrm" | ...
  "sid": "vm:100",             // service id; null em linhas de quorum/master
  "node": "pve01",
  "state": "started",
  "status": "started",
  "crm_state": "started",
  "request_state": "started",
  "quorate": true,
  "failback": null,
  "max_relocate": 1,
  "max_restart": 1,
  "timestamp": 1730000000
}
```

### `HaResourceSchema`

```jsonc
{
  "cluster_name": "lab",
  "sid": "vm:100",
  "type": "vm",                // "vm" | "ct"
  "state": "started",
  "group": "ha-group-a",
  "max_relocate": 2,
  "max_restart": 1,
  "failback": true,
  "comment": null,
  "digest": "abc",
  // Estado em runtime, mesclado de /cluster/ha/status/current quando disponivel.
  "node": "pve02",
  "crm_state": "started",
  "request_state": "started",
  "status": "started"
}
```

### `HaGroupSchema`

```jsonc
{
  "cluster_name": "lab",
  "group": "ha-group-a",
  "type": "group",
  "nodes": "pve01:1,pve02:2",
  "restricted": true,
  "nofailback": false,
  "comment": null,
  "digest": null
}
```

### `HaSummarySchema`

```jsonc
{
  "status":    [/* HaStatusItemSchema, ... */],
  "groups":    [/* HaGroupSchema, ... */],
  "resources": [/* HaResourceSchema, ... */]
}
```

## Tratamento de erros

- O router usa `logger.exception` (nunca `except` silencioso) quando uma busca por cluster falha, para que dashboards do netbox-proxbox nunca enxerguem "zeros silenciosos".
- Falha em `/cluster/ha/status` registra uma linha sintetica `HaStatusItemSchema` para aquele cluster com `status="error: <mensagem>"`; clusters saudaveis ainda contribuem com suas linhas.
- Falha no fetch de topo de `/cluster/ha/resources` ou `/cluster/ha/groups` loga e ignora aquele cluster.
- Fetches de detalhe internos (resource detail por SID, group detail) logam em nivel `debug` e caem para o payload da listagem — a resposta e best-effort, nunca um 5xx.
- `/proxmox/cluster/ha/resources/by-vm/{vmid}` sempre retorna `null` para VM/CT ids fora do HA; o consumidor deve tratar `null` como "nao gerenciado por HA" e nao como erro.

## Regras de coercao

O Proxmox retorna inteiros `0`/`1`, chaves em kebab-case (`max-restart`, `crm-state`) e booleanos em string em alguns endpoints. O router normaliza via `_coerce_int` e `_coerce_bool`:

- `bool` → o proprio `bool`; `int`/`float` → `bool(value)`; `"1"`/`"true"`/`"yes"`/`"on"` → `True`; `"0"`/`"false"`/`"no"`/`"off"`/`""` → `False`; caso contrario `None`.
- `bool` → `int(value)`; `int` → ele mesmo; `str` numerica → `int(str)`; caso contrario `None`.

Os fallbacks em kebab-case (`row.get("max_restart") or row.get("max-restart")`) sao intencionais — o mesmo codigo de leitura cobre payloads de versoes antigas e da serie 8.x do Proxmox.

## Como o `netbox-proxbox` consome estes endpoints

O `netbox-proxbox` expoe um shim REST fino sob `/api/plugins/proxbox/ha/` que faz proxy para estes endpoints:

| Shim do plugin                                | Chamada de backend                                   |
|-----------------------------------------------|------------------------------------------------------|
| `GET /api/plugins/proxbox/ha/summary/`        | `GET /proxmox/cluster/ha/summary`                    |
| `GET /api/plugins/proxbox/ha/vm/{vmid}/`      | `GET /proxmox/cluster/ha/resources/by-vm/{vmid}`     |

O plugin renderiza essas chamadas como uma pagina de dashboard Django (`HAClusterView`) e uma aba HA por VM (`ProxmoxVMHATabView`, condicionada ao custom field `proxmox_vm_id`). A pagina propria do plugin documenta o lado do consumidor — veja `netbox-proxbox/docs/api/ha.md`.

## Testes

Os testes unitarios do router ficam em `tests/test_proxmox_ha_routes.py`. Eles aplicam patch em `get_ha_status_current`, `get_ha_resources` e `get_ha_groups` de `proxbox_api.services.proxmox_helpers` e cobrem:

- Agregacao em `ha_status` e a linha sintetica de erro quando o helper levanta.
- Mesclagem do estado runtime em `ha_resources` a partir de `/status/current`.
- Retorno `null` em `ha_resource_by_vm` quando nenhum SID esta sob HA, com fallback de `vm:{vmid}` para `ct:{vmid}`.
- `ha_groups` lista + detalhe mesclado e `null` quando o grupo nao existe em nenhum cluster.
- Composicao paralela em `ha_summary`.
- Registro do router sob o prefixo `/proxmox/cluster/ha/*` na app factory real.

Para executar:

```bash
uv run pytest tests/test_proxmox_ha_routes.py -q
```

## Veja Tambem

- [Referencia HTTP — Alta Disponibilidade (somente leitura)](http-reference.md#alta-disponibilidade-somente-leitura) — listagem consolidada das rotas dentro do restante do surface `/proxmox/*`.
- [Referencia HTTP — Verbos Operacionais de VM](http-reference.md#verbos-operacionais-de-vm) — surface de escrita complementar (start/stop/snapshot/migrate) que depende de `ProxmoxEndpoint.allow_writes`.
- `netbox-proxbox/docs/api/ha.md` para o consumidor do lado do plugin.
