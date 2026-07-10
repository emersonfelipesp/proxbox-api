# Fluxos de Sincronizacao

Esta pagina explica os principais fluxos de sincronizacao entre Proxmox e NetBox.

## Fluxo de Full Update

Endpoint HTTP:

- `GET /full-update`

Ordem atual de execucao:

1. Sincroniza os nodes Proxmox para devices do NetBox.
2. Sincroniza storages Proxmox para registros de storage do plugin NetBox.
3. Sincroniza as VMs Proxmox para VMs do NetBox.
4. Sincroniza task history.
5. Sincroniza discos virtuais das VMs descobertas.
6. Sincroniza backups das VMs.
7. Sincroniza snapshots das VMs.
8. Sincroniza interfaces de node e enderecos IP.
9. Sincroniza interfaces das VMs.
10. Sincroniza IPs das VMs e a primary IP.
11. Sincroniza jobs de replicacao entre clusters Proxmox.
12. Sincroniza backup routines (configuracoes de backups agendados).

A variacao em `GET /full-update/stream` emite os mesmos estagios via Server-Sent Events.

## Fluxo de Sync de VM

Endpoint principal:

- `GET /virtualization/virtual-machines/create`

Comportamento principal:

- Le os cluster resources das sessoes Proxmox.
- Resolve configs por VM (`qemu` e `lxc`).
- Monta payloads normalizados para o NetBox.
- Cria dependencias como cluster, device e role quando necessario.
- Cria interfaces e IPs da VM quando possivel.
- Escreve journal entries para auditoria.
- No modo full-update, a criacao de VM nao faz writes de rede, porque as etapas dedicadas de interface e IP cuidam disso.
- Nomes duplicados de VM dentro de um mesmo cluster NetBox sao resolvidos de forma deterministica antes da fila de operacoes. Veja [Resolvedor de Colisoes de Nome de VM](./name-collision-resolver.md).

### Modelo Assincrono com Ordem de Dependencias

O sync de VM e assincrono de ponta a ponta, mas nem todas as etapas podem rodar em paralelo. O fluxo aplica uma cadeia estrita de dependencias antes de abrir fan-out por VM.

Preflight sequencial de dependencias:

1. Garante objetos pai globais no NetBox:
	- Manufacturer
	- Device type (depende de manufacturer)
	- Role de node Proxmox
2. Para cada cluster, garante objetos pai do escopo do cluster:
	- Cluster type
	- Cluster
	- Site
3. Para cada node do cluster, garante o device:
	- Device (depende de cluster + device type + role + site)
4. Garante objetos de role de VM por tipo (`qemu` e `lxc`).

Depois desse preflight, as operacoes por VM rodam concorrentemente com limite por semaforo.

Ordem obrigatoria por VM:

1. Buscar dados da VM no Proxmox (resource/config).
2. Reconciliar VM no NetBox (create/patch).
3. Reconciliar interfaces e IPs da VM (quando habilitado).
4. Reconciliar discos da VM.
5. Reconciliar task history da VM.

Assim, o async e usado para throughput quando os objetos sao independentes, mas dependencias pai-filho sempre sao aguardadas em sequencia.

### Regras de Paralelismo

Permitido em paralelo:

- VMs diferentes no mesmo cluster ou em clusters diferentes, depois do preflight.
- Operacoes de interface de uma VM quando o objeto VM ja existe.
- Operacoes de disco de uma VM quando o objeto VM ja existe.

Nao permitido em paralelo:

- Criar objetos filho antes dos objetos pai necessarios existirem.
- Reconciliar estado da VM no NetBox antes de buscar os dados da VM no Proxmox.
- Criar device antes de manufacturer/device type/site/cluster estarem prontos.

### Busca em duas fases no full-update

No modo full-update o lote de VMs roda em duas fases distintas para que o
semaforo de concorrencia nunca segure uma resposta HTTP do Proxmox enquanto
trabalho de CPU ou de NetBox nao relacionado executa:

1. **Fase de busca** — a config de cada VM no Proxmox e buscada primeiro em um
   lote assincrono enxuto. O semaforo (`PROXBOX_VM_SYNC_MAX_CONCURRENCY`)
   protege *apenas* a chamada `get_vm_config`, entao as respostas HTTP pendentes
   sao drenadas rapidamente.
2. **Fase de processamento** — as configs buscadas viram o estado desejado no
   NetBox. O trabalho sincrono e ligado a CPU (Pydantic `model_validate`,
   construcao do payload NetBox) e descarregado com `asyncio.to_thread` e roda a
   partir de dados em memoria.

Antes dessa separacao, um unico slot do semaforo cobria busca + validacao +
chamadas ao NetBox + construcao do payload; enquanto os slots estavam ocupados
com CPU ou NetBox, o event loop nao conseguia drenar as respostas do Proxmox em
voo, entao o timeout de requisicao a nivel de sessao disparava falsamente e
produzia falhas espurias de `ProxmoxTimeoutError` em clusters com muitas VMs.
Falhas por VM permanecem isoladas nas duas fases (uma busca ou preparacao que
falha incrementa o contador de falhas e o restante do lote prossegue), e uma
linha de log de tempo reporta `fetch_ms`, `process_ms` e a contagem de falhas de
busca.

### Modos de sync (VM e template de VM)

O plugin encaminha os parametros de query `sync_mode_vm` e
`sync_mode_vm_template` (`always` / `bootstrap_only` / `disabled`, padrao
`always`) em cada requisicao de stage de VM, e o backend aplica a filtragem por
registro: um recurso Proxmox com o campo `template` verdadeiro e regido por
`sync_mode_vm_template`, e qualquer outro recurso QEMU/LXC por `sync_mode_vm`.
Um modo `disabled` pula os recursos correspondentes na passagem sem conta-los
como falha; um valor desconhecido cai para `always` com um aviso, para que um
parametro malformado nunca bloqueie um sync silenciosamente.

A filtragem e aplicada **na origem**, antes da descoberta e do precompute de
dependencias, entao um modo `disabled` nao cria nem atualiza objetos
dependentes no NetBox (manufacturer, device type, cluster, site, devices de
node, roles de VM) para VMs que nunca serao sincronizadas.

### Reflexao das chaves do cloud-init

Para VMs QEMU que bootam com cloud-init, o sync de VM reflete as chaves SSH
configuradas, o usuario e o bag de IP/Gateway/DNS para a metadata Proxbox da
VM no NetBox para que operadores auditem o estado do cloud-init sem abrir a
UI do Proxmox. O mapeamento fica em `proxbox_api/proxmox_to_netbox/` e e
coberto por `tests/test_vm_cloudinit_mapping.py`; a aba correspondente no
plugin NetBox renderiza o mesmo payload. Rastreado em
[netbox-proxbox#363](https://github.com/emersonfelipesp/netbox-proxbox/issues/363).

### Parsing de `netbox-metadata` a partir das descricoes do Proxmox

Operadores podem embutir um bloco JSON com cerca (`netbox-metadata`) dentro
da descricao da VM no Proxmox. O sync extrai o bloco, valida-o por um schema
Pydantic permissivo e usa o resultado para semear campos do NetBox geridos
por usuario (description, tags, custom fields) antes do payload Proxmox-derivado
normal mesclar. A logica de parsing fica centralizada em
`proxbox_api/proxmox_to_netbox/description_metadata.py` e e travada por
`tests/test_description_metadata.py`. JSON invalido ou violacoes de schema
sao logadas mas nao falham o sync — o sync cai para a string bruta da descricao.

## Fluxo de Backup

Endpoints:

- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`
- `GET /virtualization/virtual-machines/backups/all/create/stream`

Comportamento principal:

- Descobre conteudo de backup no storage do Proxmox.
- Mapeia backups para VMs do NetBox.
- Cria objetos de backup no modelo do plugin NetBox.
- Trata duplicidade.
- Pode remover backups que nao existem mais na origem Proxmox.

## Fluxo de Snapshot

Endpoints:

- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`

Comportamento principal:

- Descobre snapshots para VMs do NetBox mapeadas para VM IDs do Proxmox.
- Reconcilia objetos de snapshot no modelo do plugin NetBox.
- Resolve registros de storage relacionados quando possivel.

## Fluxo de Storage

Endpoints:

- `GET /virtualization/virtual-machines/storage/create`
- `GET /virtualization/virtual-machines/storage/create/stream`

Comportamento principal:

- Descobre definicoes de storage do Proxmox.
- Reconcilia registros de storage do plugin NetBox usados pelos fluxos de backup e snapshot.

## Modo SSE

Cada fluxo de sync possui um endpoint `/stream` correspondente que emite Server-Sent Events em tempo real:

- `GET /full-update/stream`
- `GET /dcim/devices/create/stream`
- `GET /virtualization/virtual-machines/create/stream`

Como funciona:

1. O endpoint de stream cria uma instancia de `WebSocketSSEBridge`.
2. O servico de sync e chamado com `use_websocket=True` e o bridge como argumento `websocket`.
3. Enquanto o servico processa cada objeto, ele chama `await websocket.send_json(...)` com o progresso por objeto.
4. O bridge converte cada payload de websocket em um evento SSE `step` com campos normalizados.
5. O endpoint de stream itera `bridge.iter_sse()` e envia cada frame SSE ao cliente HTTP.
6. Ao concluir, o bridge e fechado e um evento final `complete` e emitido.

Isso fornece progresso granular como:

- `Processing device pve01`
- `Synced device pve01`
- `Processing virtual_machine vm101`
- `Synced virtual_machine vm101`

## Modo WebSocket

O endpoint WebSocket `/ws` fornece sync interativo com o mesmo progresso por objeto, mas via canal bidirecional.
O comando `Full Update Sync` dispara a mesma logica de sync, mas envia mensagens JSON diretamente ao cliente WebSocket.

## Rastreamento e observabilidade

- Os sync-process records sao criados em objetos do plugin NetBox.
- Journal entries sao escritos com resumo e erros.
- Fluxos WebSocket e SSE fornecem status em tempo real.

## Tratamento de falhas

O tratamento de erros usa decorators e utilitarios de validacao:

### Validacao de erros

- Respostas NetBox sao validadas para garantir que contem os campos obrigatorios.
- Respostas Proxmox sao validadas com modelos Pydantic quando ha helpers tipados.
- Respostas invalidas levantam excecoes tipadas como `NetBoxAPIError` ou `ProxmoxAPIError`.

### Hierarquia de erros de sync

Tipos de excecao customizados fornecem contexto detalhado:

- `VMSyncError`: falhas no sync de VM
- `DeviceSyncError`: falhas no sync de node/device
- `StorageSyncError`: falhas na definicao de storage
- `NetworkSyncError`: falhas em interface de rede e VLAN
- Base: `SyncError` para falhas genericas de sync

### Retry e resiliencia

- Os helpers de retry aplicam exponential backoff para falhas transientes.
- O comportamento e configuravel por `PROXBOX_NETBOX_MAX_RETRIES` e `PROXBOX_NETBOX_RETRY_DELAY`.
- Tentativas falhas sao logadas com contexto antes de tentar novamente.
- A falha final sobe com contexto completo.

### Guests com muitas interfaces

O sync de interfaces de VM le as interfaces do guest pelo guest agent do QEMU
(`network-get-interfaces`). Guests com muitas interfaces (roteadores VRRP,
enderecos alias) exigem cuidado extra:

- **Modelo duplo de interface de VM** — o padrao
  `vm_interface_sync_strategy=guest_os_model` mantem a interface core do NetBox
  `virtualization.VMInterface` nomeada pela config do Proxmox (`net0`, `net1`,
  ...). Quando ha dados do guest-agent, o proxbox-api tambem faz upsert das
  linhas de plugin `GuestVMInterface` do netbox-proxbox com nomes do sistema
  operacional guest (`ens18`, `eth0`, ...) e liga suas linhas de endereco aos
  mesmos IDs core de `ipam.IPAddress` ja reconciliados na VMInterface core. Ele
  nunca cria registros IPAM duplicados para o lado guest. Releases antigos do
  netbox-proxbox sem esses endpoints retornam 404; essas escritas de plugin sao
  logadas e ignoradas sem falhar o sync core de interface/IP.
- **Rename legado depreciado** — `vm_interface_sync_strategy=legacy_rename`
  preserva o comportamento anterior em que `use_guest_agent_interface_name=true`
  renomeia a VMInterface core de `net0` para o nome do sistema operacional
  guest. O backend registra um aviso de depreciacao para esse modo.
- **Timeout dedicado com um retry** — a chamada ao guest-agent usa
  `PROXBOX_GUEST_AGENT_TIMEOUT` (campo de plugin `guest_agent_timeout`, padrao
  15s) em vez do timeout curto de sessao, e tenta novamente uma vez em caso de
  timeout, ja que uma unica enumeracao lenta costuma ser transiente. O
  proxmox-sdk nao tem timeout por chamada, entao o backend amplia
  temporariamente o timeout do backend HTTPS durante a chamada e o restaura
  depois.
- **Agregacao por MAC de alias** — entradas alias do guest-agent nomeadas
  `"<pai>:<N>"` (ex.: `ens20:1`) compartilham o MAC da NIC pai e carregam
  enderecos extras. Elas sao mescladas na interface pai (enderecos deduplicados
  por `(ip_address, prefix)`) em vez de deixar a ultima entrada por MAC vencer,
  o que antes resolvia nomes de interface errados e descartava os enderecos do
  pai. Interfaces realmente distintas que compartilham um MAC mas nao tem nome
  de alias (interfaces VRRP reais) sao preservadas intactas.
- **Falhas do bulk-reconcile aparecem** — quando a reconciliacao em lote das
  interfaces de VM falha, ou termina com registros com falha (falha parcial), o
  stage agora levanta excecao (e emite um frame de falha no stream) em vez de
  retornar um sucesso vazio/parcial, para que interfaces nunca fiquem
  silenciosamente ausentes no NetBox.

O dispatch por VM tambem e isolado: a falha de criacao/atualizacao de uma VM e
logada e contada no total de falhas da execucao, em vez de abortar a fila
inteira, entao uma VM ruim nao derruba mais todas as VMs enfileiradas depois
dela.

### Structured logging

Todas as operacoes de sync usam structured logging:

- Phase logging: cada fase distinta emite logs com contexto de operacao e fase.
- Resource logging: eventos por objeto sao logados com ID, tipo e status.
- Completion logging: os resultados incluem contagem de sucessos e falhas e tempo decorrido.
- Error logging: falhas incluem detalhes da excecao, stack trace e contexto completo.

### Response handling

- Erros de dominio sao levantados via `ProxboxException` e retornados como JSON estruturado pelos handlers da app.
- Excecoes nao tratadas sao capturadas pelo handler global e retornadas como JSON estruturado com status 500.
- Os handlers tentam continuar em alguns loops batch quando faz sentido.
- No modo SSE, erros sao emitidos como frames `error` seguidos de um `complete` final com `ok: false`.

Para detalhes de implementacao, veja `proxbox_api/utils/sync_error_handling.py` e `proxbox_api/utils/structured_logging.py`.
