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
