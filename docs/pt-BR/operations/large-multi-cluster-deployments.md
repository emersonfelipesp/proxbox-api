# Implantações Grandes com Vários Clusters

Ambientes grandes normalmente precisam de limites de transporte maiores e de
um limite de entrada mais alto, não de mais concorrência. O plugin netbox-proxbox
invoca a maior parte das etapas de sincronização com um endpoint Proxmox por
vez, portanto a concorrência por endpoint não precisa crescer com o número de
clusters. Dentro do proxbox-api, a coleta de rotinas de backup e de replicação
ainda dispara uma tarefa Proxmox por sessão fornecida, em paralelo
(`backup_routines.py`, `replications.py`), e leituras agregadas de firewall ou
datacenter percorrem todas as sessões passadas. Dimensione Proxmox e NetBox para
essa orquestração por endpoint mais qualquer fan-out multi-sessão incluído nos
jobs. Aumente a concorrência somente após medir a capacidade de Proxmox, NetBox
e PostgreSQL disponível para um job.

## Ajustes de runtime

Quando listadas, as variáveis de ambiente sobrescrevem os valores da página de
configurações do Proxbox no NetBox. Os valores do plugin ficam em cache por
cinco minutos; alterações no ambiente exigem reinício do processo. Os valores
de transporte do Proxmox não têm sobrescrita pelo ambiente: eles podem ser
sobrescritos em cada endpoint Proxmox, e o valor do endpoint prevalece sobre o
padrão do plugin.

| Variável de ambiente | Configuração do plugin | Padrão | Mínimo | Controle |
|---|---|---:|---:|---|
| — | `proxmox_timeout` | 5 s | 1 | Timeout HTTP padrão do Proxmox; cada endpoint pode sobrescrevê-lo. |
| — | `proxmox_max_retries` | 0 | 0 | Número padrão de tentativas do Proxmox; cada endpoint pode sobrescrevê-lo. |
| — | `proxmox_retry_backoff` | 0.5 s | 0 | Backoff padrão de tentativas do Proxmox; cada endpoint pode sobrescrevê-lo. |
| `PROXBOX_NETBOX_TIMEOUT` | `netbox_timeout` | 120 s | 1 | Timeout total de uma requisição HTTP ao NetBox. |
| `PROXBOX_NETBOX_MAX_RETRIES` | `netbox_max_retries` | 5 | 0 | Tentativas para falhas transitórias de transporte do NetBox. |
| `PROXBOX_NETBOX_RETRY_DELAY` | `netbox_retry_delay` | 2.0 s | 0 | Atraso base do backoff exponencial de tentativas do NetBox. |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `netbox_max_concurrent` | 1 | 1 | Requisições REST concorrentes ao NetBox. Mantenha dentro do orçamento do pool de conexões do banco do NetBox. |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `netbox_write_concurrency` | 8 | 1 | Operações concorrentes por VM com muitas escritas em uma sincronização. |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `proxmox_fetch_concurrency` | 8 | 1 | Leituras concorrentes do Proxmox nas etapas de interfaces, snapshots, backups, histórico de tarefas e etapas relacionadas. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `vm_sync_max_concurrency` | 4 | 1 | Buscas concorrentes de configuração de VM e operações de discos virtuais. |
| `PROXBOX_BULK_BATCH_SIZE` | `bulk_batch_size` | 50 | 1 | Objetos por lote geral de escrita em massa no NetBox. |
| `PROXBOX_BULK_BATCH_DELAY_MS` | `bulk_batch_delay_ms` | 500 ms | 0 | Atraso entre lotes gerais de escrita em massa. |
| `PROXBOX_BACKUP_BATCH_SIZE` | `backup_batch_size` | 5 | 1 | VMs por lote de descoberta e reconciliação de backups. |
| `PROXBOX_BACKUP_BATCH_DELAY_MS` | `backup_batch_delay_ms` | 200 ms | 0 | Atraso entre lotes de backup. |
| `PROXBOX_INTERFACE_BATCH_SIZE` | `interface_batch_size` | 5 | 1 | VMs por lote de sincronização de interfaces. |
| `PROXBOX_INTERFACE_BATCH_DELAY_MS` | `interface_batch_delay_ms` | 100 ms | 0 | Atraso entre lotes de interfaces. |
| `PROXBOX_GUEST_AGENT_TIMEOUT` | — | 15.0 s | 1.0 | Timeout de uma chamada `network-get-interfaces` do guest agent QEMU. Somente variável de ambiente (sem configuração no plugin NetBox); reinício necessário após alteração. |

Consulte [Ajustes de Concorrência em Runtime](../development/async-tunables.md)
para a referência de implementação, concorrência e diagnóstico.

## Perfil inicial para cerca de 30 clusters

Use este perfil como uma base conservadora e ajuste a partir de medições:

```text
PROXBOX_RATE_LIMIT=3000
proxmox_timeout=15
proxmox_max_retries=2
proxmox_retry_backoff=1.0
netbox_timeout=180
netbox_max_concurrent=1
```

Para clusters remotos, defina o timeout específico do endpoint Proxmox entre
20 e 30 segundos. Use `netbox_max_concurrent=2` somente quando o pool do banco
do NetBox tiver capacidade. Inicialmente, mantenha os ajustes de concorrência e
lotes nos valores padrão.

## Limite de requisições e custo de autenticação

`PROXBOX_RATE_LIMIT` é um limite por endereço de origem e por processo, com
padrão de 300 requisições por minuto. O middleware executa antes da
autenticação. Todas as requisições do NetBox normalmente chegam ao proxbox-api
pelo mesmo endereço; portanto, uma sincronização grande e o tráfego normal da
interface compartilham o mesmo limite. O valor `3000` é um ponto de partida
prático para um ambiente grande.

Quando o limite se esgota, a resposta é HTTP 429:

```json
{"detail":"Rate limit exceeded. Please try again later."}
```

Mantenha uma única chave ativa do proxbox-api quando possível. A autenticação
verifica as chaves ativas em ordem, e cada candidata acrescenta uma verificação
bcrypt a cada requisição autenticada. Faça a rotação, confirme que a nova chave
funciona e depois desative a antiga.

## Processos e organização dos jobs

A contagem de workers uvicorn é uma decisão de tradeoff, não um limite rígido de
um único processo:

- **Limite de requisições** — `PROXBOX_RATE_LIMIT` é aplicado por processo
  worker. Cada worker mantém seu próprio orçamento por endereço de origem; vários
  workers multiplicam a capacidade efetiva de entrada, mas também dividem o
  limite global, a menos que você ajuste o valor.
- **Registro de sincronizações ativas** — a visão consultiva em memória das
  sincronizações em execução é por worker. `GET /sync/active` e status
  relacionados refletem apenas os jobs do worker que atende a requisição, salvo
  se você padronizar um worker ou tratar a sonda como aproximada.
- **Política de execução interativa** — `PROXBOX_EXECUTION_MODE` e pins de
  geração relacionados são locais ao processo; workers mistos podem divergir na
  admissão interativa se a configuração não for idêntica em todos.
- **Relay de console no navegador** — tickets standalone e payloads Fernet ficam
  no SQLite compartilhado; criação e consumo podem funcionar entre workers
  quando o banco é compartilhado.
- **Pressão sobre conexões NetBox** — cada worker aplica `netbox_max_concurrent`
  de forma independente. O uso REST concorrente total escala com
  `netbox_max_concurrent × workers`.

Para semântica global coerente de limite e status de sincronização, um worker
mais um `PROXBOX_RATE_LIMIT` mais alto costuma ser o layout mais simples. Vários
workers podem fazer sentido quando você aceita limites e visibilidade
particionados, dimensiona os pools do NetBox para a concorrência multiplicada e
mantém a política interativa alinhada em cada processo. Workers não substituem
aumentar os tunáveis de concorrência das etapas dentro de um único job.

Evite recarregar a página inicial do Proxbox enquanto uma sincronização estiver
iniciando, pois as requisições iniciais da página competem com o pico da
sincronização pelo mesmo limite do endereço de origem. Divida ambientes muito
grandes em subconjuntos de endpoints e execute um subconjunto por job. Isso
limita o escopo de falhas e facilita medir a duração e a carga nos serviços
dependentes.
