# Tunáveis de Concorrência em Runtime

## Ordem de Resolução das Configurações

Todo tunable relacionado a async no proxbox-api resolve através de uma cadeia
de prioridade de três níveis com cache de 5 minutos:

```
variável de env  >  ProxboxPluginSettings (página do plugin NetBox)  >  padrão embutido
```

A resolução é feita por `proxbox_api.runtime_settings.get_int` (e `get_float`,
`get_bool`, `get_str`):

```python
def get_int(
    settings_key: str,
    env: str,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    # 1. verificar os.environ
    if env in os.environ:
        return clamp(int(os.environ[env]), minimum, maximum)
    # 2. verificar ProxboxPluginSettings (cache de 5 min)
    settings = _get_cached_plugin_settings()
    if settings and hasattr(settings, settings_key):
        return clamp(getattr(settings, settings_key) or default, minimum, maximum)
    # 3. padrão embutido
    return default
```

Isso significa que você pode alterar a concorrência sem reiniciar o serviço —
atualize a página de configurações do plugin no NetBox e o novo valor terá
efeito em até 5 minutos (ou imediatamente ao reiniciar).

## Referência de Tunáveis de Concorrência

| Variável de Env | Chave de Configurações do Plugin | Padrão | Mín | Descrição |
|---|---|---|---|---|
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `vm_sync_max_concurrency` | 4 | 1 | Máx de fetches concorrentes de configuração de VM Proxmox nas fases de sync de VMs e discos |
| `PROXBOX_VM_CONFIG_FETCH_TIMEOUT_SECONDS` | — | 30 | 1 | Segundos maximos para um fetch de configuracao de VM no sync completo antes de isolar essa VM como falha |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `netbox_write_concurrency` | 8 | 1 | Máx de tarefas concorrentes de sync por VM com escrita intensa no NetBox (VMs e discos) |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `proxmox_fetch_concurrency` | 8 | 1 | Máx de leituras concorrentes da API Proxmox para interfaces |
| `PROXBOX_SESSION_ACQUIRE_CONCURRENCY` | `session_acquire_concurrency` | 8 | 1 | Máx de aberturas de sessão concorrentes na aquisição parcial multi-endpoint |
| `PROXBOX_INTERFACE_BATCH_SIZE` | `interface_batch_size` | 5 | 1 | VMs por lote de sincronização de interfaces (evita sobrecarga do NetBox) |
| `PROXBOX_INTERFACE_BATCH_DELAY_MS` | `interface_batch_delay_ms` | 100 | 0 | Milissegundos entre lotes de sincronização de interfaces |
| `PROXBOX_GUEST_AGENT_TIMEOUT` | — | 15.0 | 1.0 | Segundos para chamada `network-get-interfaces` do agente guest (somente variável de ambiente; não existe na página de configurações do plugin NetBox) |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `netbox_max_concurrent` | 1 | 1 | Máx de requisições GET concorrentes ao NetBox (manter baixo para evitar esgotamento do pool PostgreSQL) |
| `PROXBOX_NETBOX_TIMEOUT` | `netbox_timeout` | 120 | 1 | Timeout total da sessão HTTP do NetBox em segundos |

Para dimensionamento do ambiente, limites de entrada, topologia de workers e
um perfil inicial para cerca de 30 clusters, consulte [Implantações Grandes com
Vários Clusters](../operations/large-multi-cluster-deployments.md).

`ProxmoxSessionsDep` permanece estrita: a falha de um endpoint interrompe a
dependência e fecha as sessões já abertas para uma requisição comum de
inventário. `ProxmoxSessionsPartialDep` é uma opção explícita para leituras
agregadas multi-endpoint. Ela retorna dados dos endpoints saudáveis e informa
falhas sem segredos no header de resposta JSON limitado
`X-Proxbox-Endpoint-Errors`. O resumo tem o formato `{"failed": <quantidade>,
"ids": [<até 20 IDs de endpoint>], "truncated": <booleano>}`, tem limite de
1024 bytes e não inclui nomes de endpoint nem mensagens de erro. Se todos os
endpoints selecionados falharem, a requisição ainda retorna um erro.

Durante a sincronização, o plugin netbox-proxbox solicita os dados de firewall
e datacenter para um endpoint por vez. Assim, uma falha é informada como falha
daquele endpoint. Outros clientes multi-endpoint podem consultar o header de
resumo limitado para identificar os IDs com falha e preservar os dados
saudáveis.

## A Otimização de `netbox_version` Único (F3)

Antes dessa otimização, `ensure_vm_type` chamava `detect_netbox_version(nb)` a
cada invocação — uma rodada extra ao NetBox por tipo de VM por passo de
sincronização. Em um cluster com 50 tipos de VM únicos e 500 VMs, isso poderia
significar 50+ verificações de versão redundantes por sincronização.

A correção chama `detect_netbox_version` **uma vez** no início de
`create_virtual_machines` e encaminha o resultado para cada chamada de
`ensure_vm_type`:

```python
# Chamado uma vez no início do passo de sincronização
netbox_version = await asyncio.to_thread(detect_netbox_version, nb)

# Encaminhado para todas as chamadas ensure_vm_type
for vm_type in unique_vm_types:
    await ensure_vm_type(nb, vm_type=vm_type, tag_refs=tag_refs,
                         netbox_version=netbox_version)   # <-- pré-resolvido
```

`detect_netbox_version` é uma função bloqueante (chama a API de status do
NetBox de forma síncrona), então também é envolvida em `asyncio.to_thread` por
segurança do event loop.

## Diagnosticando Gargalos de Concorrência

### Identificar o Gargalo pelos Logs de Temporização

A função `_run_full_update_vm_batch` registra durações das fases:

```
VM full-update phase timing: fetch_ms=8234.12 process_ms=432.10 fetched_ok=480 fetch_failed=2
```

| Condição | Causa provável | Ação de ajuste |
|---|---|---|
| `fetch_ms` muito alto, `fetched_ok` baixo | API Proxmox está lenta ou `PROXBOX_VM_SYNC_MAX_CONCURRENCY` muito baixo | Aumentar concorrência (verificar rate limits Proxmox primeiro) |
| `fetch_ms` alto + muitos `fetch_failed` | API Proxmox está sobrecarregada | Reduzir concorrência ou adicionar rate limiting |
| `process_ms` alto | Preparação lenta; inclui resolução de dependências e trabalho local de modelos | Separar esperas upstream de perfis de CPU antes de ajustar; perfilar `build_netbox_virtual_machine_payload` somente quando a evidência de CPU apontar para ele |
| Timeouts de escrita NetBox no despacho | Esgotamento do pool PostgreSQL | Reduzir `PROXBOX_NETBOX_WRITE_CONCURRENCY` |

O log detalhado do lote acrescenta `process_cpu_ms`, `snapshot_ms`,
`hydration_ms`, `name_resolution_ms`, `canonicalization_ms`, `dispatch_ms` e
`persistence_ms`. Compare `total_ms` com o tempo de CPU do processo antes de
classificar uma fase como CPU-bound. `process_cpu_ms` mede o processo inteiro e
só pode ser atribuído ao sync durante uma execução isolada. O mesmo log registra
a quantidade de requests de configuração, latência total/máxima, limites
efetivos de workers/fila e pico de RSS do processo. A fila pendente dos workers de configuração
tem capacidade igual ao dobro de `PROXBOX_VM_SYNC_MAX_CONCURRENCY`; aumentar a
configuração eleva tanto as requisições upstream ativas quanto o trabalho
pendente limitado. Ajuste somente depois de medir contagens de requisições,
esperas do pool, timeouts e pressão upstream.

### Verificar Travamentos do Agente Guest

Se `PROXBOX_GUEST_AGENT_TIMEOUT` for muito baixo para VMs com muitas interfaces,
os dados do agente guest são silenciosamente descartados. Procure por:

```
WARNING proxbox_api: Timeout agente guest (tentativa 1): vmid=101 node=pve01 timeout=15.0s
```

Aumente `PROXBOX_GUEST_AGENT_TIMEOUT` para `30` ou `60` para VMs de roteador
VRRP.

### Usar o Log de Debug de Cache

Defina `PROXBOX_DEBUG_CACHE=true` para emitir eventos de acerto/erro/evicção de
cache por requisição do cache GET do NetBox. Altas taxas de erro aumentam a
latência efetiva do NetBox e podem ser abordadas ajustando
`PROXBOX_NETBOX_GET_CACHE_TTL` ou `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES`.

## Exemplo: Ajustando para um Cluster Grande

Para um cluster com 2000 VMs em um host de baixa latência:

```bash
# .env ou seção de environment do docker-compose
PROXBOX_VM_SYNC_MAX_CONCURRENCY=8      # de 4 — cluster aguenta
PROXBOX_NETBOX_WRITE_CONCURRENCY=12    # de 8 — pool DB suporta
PROXBOX_PROXMOX_FETCH_CONCURRENCY=12  # de 8 — leituras de interface são rápidas
PROXBOX_GUEST_AGENT_TIMEOUT=30        # de 15 — algumas VMs são lentas
PROXBOX_NETBOX_GET_CACHE_TTL=120      # de 60 — reduz pressão GET
```

Monitore os logs de temporização após cada mudança. Não aumente a concorrência
além do que o serviço downstream consegue suportar — esgotamento do pool de
conexões PostgreSQL é mais difícil de diagnosticar do que throughput lento.
