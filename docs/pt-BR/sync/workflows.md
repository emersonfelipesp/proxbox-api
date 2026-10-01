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

A reconciliacao de Devices de node usa o template efetivo documentado em
[Configuracao](../getting-started/configuration.md#template-de-nome-do-device-de-node).
Os mapas usam o cluster Proxmox e o nome curto do node como chave, enquanto as
escritas e buscas no NetBox usam o nome renderizado. O estado tipado de sync
sempre registra os nomes curtos originais do node e do cluster, mantendo VMs e
interfaces ligadas ao Device correto quando clusters reutilizam um nome ou um
Device gerenciado e renomeado.

A flag de comportamento `sync_node_interfaces` (`GET /full-update?sync_node_interfaces=true`
e o mesmo parametro de query em `GET /full-update/stream`) e repassada a etapa de
interfaces de node nas execucoes com e sem streaming. Quando ativa, a etapa
reconcilia a topologia completa de `/nodes/{node}/network`, incluindo a opcao
`hwaddress` fixada em uma bridge como endereco MAC primario, exatamente como
`GET /dcim/devices/interfaces/create?sync_node_interfaces=true`. Sem ela, a
etapa mantem o comportamento legado por interface, que nao define MAC.

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
- No modo full-update, a criacao de VM nao faz writes de rede nem task history,
  porque as etapas dedicadas de interface, IP e task history sao as unicas donas
  desses trabalhos.
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
5. Depois de conhecer todas as VMs bem-sucedidas, reconciliar o task history em
   um unico agregado por nodes (a menos que `sync_task_history=false`).

Assim, o async e usado para throughput quando os objetos sao independentes, mas dependencias pai-filho sempre sao aguardadas em sequencia.

### Propriedade do task history

As rotas de criacao gerais e direcionadas usam `sync_task_history=true` por
padrao, preservando o comportamento standalone. Elas enviam somente os IDs
NetBox das VMs reconciliadas com sucesso para uma unica chamada agregada. O
full-update define a flag da etapa de VM como `false` e executa depois uma unica
etapa dedicada para todas as VMs. O coletor pagina uma vez o arquivo de cada
node selecionado, em vez de consultar todos os nodes para cada VM; cobertura
parcial retorna `degraded=true`. O REST standalone converte esse agregado
degradado em HTTP 502 depois de preservar as linhas reconciliadas, enquanto o
SSE publica o resumo degradado da etapa. Consultas de IDs selecionados no NetBox
usam lotes limitados de valores repetidos e falham de forma fechada se qualquer
lote nao puder ser lido. Veja
[Sincronizacao de Task History](./task-history.md).

### Selecao em execucoes por etapa: propriedade estrita versus tolerante

As etapas por VM (`virtual-machines`, `virtual-disks`, `backups`, `snapshots`,
`vm-interfaces` e `vm-ip-addresses`) resolvem o dono Proxmox de cada VM a partir
do sidecar tipado de estado de sync (endpoint, cluster, VMID e tipo de VM) e,
nas rotas com lista selecionada, contra os recursos Proxmox ativos. Uma VM cujo
dono nao pode ser resolvido e tratada por um modo de selecao explicito escolhido
pela rota:

| Modo | Usado por | Comportamento |
|---|---|---|
| Estrito | Rotas que enderecam uma VM pelo caminho: `/{netbox_vm_id}/create`, `/{netbox_vm_id}/backups/create/stream`, `/{netbox_vm_id}/snapshots/create/stream`, `/{netbox_vm_id}/virtual-disks/create/stream` | Falha de forma fechada na primeira VM com propriedade inutilizavel (HTTP 502 ou um `complete` SSE com `ok=false`). Nao ha outra VM para avancar e um dono errado nunca deve ser adivinhado. |
| Tolerante | Execucoes por etapa e de todo o ambiente: rotas com lista `netbox_vm_ids`, `/all/create`, as etapas `interfaces/create` e `interfaces/ip-address/create` de todo o ambiente, o cache de propriedade do backup completo e as duas variantes de full-update | Descarta a VM com um `WARNING` que nomeia o ID da VM no NetBox e o motivo, processa normalmente as demais VMs e reporta o descarte. |

Rotas que nao dizem escolhem pelo enderecamento: rotas de lista e de todo o
ambiente sao tolerantes e rotas de VM unica por caminho sao estritas, de modo que
um plugin orquestrador antigo, que nao envia nenhum parametro novo, continua
funcionando sem mudanca.

No modo tolerante uma VM e descartada quando o sidecar esta incompleto (sem ID de
endpoint, cluster, VMID positivo ou tipo de VM), quando ha mais de um sidecar,
quando uma VM explicitamente selecionada nao tem sidecar, quando o cluster nao
tem fonte Proxmox disponivel ou e ambiguo entre endpoints, quando o endpoint do
sidecar discorda do dono do cluster, quando nenhum recurso Proxmox ativo
corresponde (por exemplo um guest apagado no Proxmox mas ainda no NetBox) ou
varios correspondem, e quando duas VMs selecionadas reivindicam o mesmo
endpoint/cluster/VMID/tipo. Todos os que reivindicam um dono compartilhado sao
descartados, pois nao e possivel saber qual esta certo. Em uma varredura de todo
o ambiente, uma VM sem nenhum sidecar e nao gerenciada e e ignorada em silencio,
sem aviso.

Permanecem fatais nos dois modos por nao serem problemas de propriedade por VM:
uma varredura de sidecar ilegivel ou indisponivel, um ID de VM invalido e uma
selecao que o NetBox nao devolve por completo.

Uma VM descartada nunca e tocada pela etapa. Ela fica fora do cache de
propriedade; portanto nao e reconciliada nem coberta pela limpeza de backups ou
snapshots obsoletos, e suas linhas existentes no NetBox permanecem como estao.

As VMs descartadas sao reportadas como avisos estruturados,
`[{"netbox_vm_id": <int>, "reason": "<texto>"}]`, e o resultado da etapa fica
`degraded=true`; a execucao nao falha e, se todas as VMs selecionadas forem
descartadas, a etapa devolve um resultado vazio com os avisos em vez de lancar
erro:

- Resultados em dict (`snapshots`, `virtual-disks`) carregam as chaves `degraded`
  e `warnings`. Os eventos SSE `complete` e `step` da etapa carregam o mesmo
  resultado.
- Resultados em lista (`backups`, `vm-interfaces`, `vm-ip-addresses`) carregam os
  avisos no resultado. O resultado SSE ganha `warnings` e `degraded` ao lado de
  `count`. A resposta REST continua sendo uma lista simples quando limpa e vira
  `{"<etapa>": [...], "count": n, "warnings": [...], "degraded": true}` quando
  degradada.
- O stream de `virtual-machines` reporta os mesmos `warnings` e `degraded` no
  resultado de `complete`. A rota REST `/create` continua devolvendo uma lista
  simples quando limpa e, quando uma VM selecionada foi descartada, devolve
  `{"virtual_machines": [...], "count": n, "warnings": [...], "degraded": true}`.
  As rotas por ID `/{netbox_vm_id}/create` permanecem estritas e nunca degradam.
- O full-update (REST e SSE) agrega os avisos de todas as etapas, cada um com sua
  `phase`, em `warnings` no nivel superior e define `degraded=true`.

O task-history mantem seu proprio contrato e nao faz parte deste modo: uma VM
explicitamente selecionada sem identidade continua fatal ali, e seu agregado
degradado continua gerando HTTP 502 no REST standalone. Veja
[Sincronizacao de Task History](./task-history.md).

Um sidecar fica incompleto quando o sync da VM grava sua identidade somente com
`overwrite_vm_custom_fields` habilitado e descarta um ID de endpoint ausente. O
gravador registra um aviso que nomeia a VM sempre que o endpoint, o cluster, o
VMID ou o tipo de VM ao vivo estiver ausente ou o tipo for `unknown`, tornando a
causa visivel onde ocorre; o que e persistido nao muda.

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

## Tratamento de VMs orfas

A configuracao `delete_orphans` e a variavel `PROXBOX_DELETE_ORPHANS`
controlam a varredura de orfas no fim da execucao. Quando desabilitada, a
varredura nao consulta nem altera o NetBox. Quando habilitada, uma VM QEMU ou um
container LXC descoberto pelo Proxbox mas nao tocado pela execucao atual nunca e
removido: o backend define `status=decommissioning` e adiciona a tag
`proxbox-soft-deleted`, preservando as tags existentes. Um dry-run apenas
relata os candidatos, sem enviar PATCH. Se o guest reaparecer no Proxmox, a
reconciliacao normal remove o marcador e preserva as demais tags.

### Executando a varredura em um sync por etapas

As rotas de full-update executam a varredura sozinhas. Um chamador que dispara
cada etapa separadamente, como o plugin NetBox, deve chamar a varredura
independente depois das suas etapas:

- `GET /virtualization/virtual-machines/orphans/sweep`
- `GET /virtualization/virtual-machines/orphans/sweep/stream`

`run_id` e obrigatorio e deve ser o mesmo valor enviado a etapa de VMs, pois e o
identificador gravado no sidecar de estado de cada VM reconciliada; uma VM com
outro `run_id` e candidata a orfa. `dry_run=true` apenas simula.
`endpoint_ids` ou `proxmox_endpoint_ids` (separados por virgula; o alias tem
precedencia) limitam a varredura as VMs dos endpoints Proxmox informados, e
`vm_stage_failed=true` pula a varredura. Uma varredura real (sem `dry_run`)
precisa informar o escopo de endpoints: sem ele a rota responde HTTP 422, porque
`run_id` e `vm_stage_failed` nao sao verificados e uma varredura real sem escopo
poderia marcar todas as VMs gerenciadas. Um dry-run pode ficar sem escopo. A rota le `delete_orphans` como o
full-update: com a configuracao desabilitada retorna `enabled=false` sem
consultar nem alterar nada.

Todo resultado inclui `skipped_reason` (`null` quando a varredura foi
executada):

| `skipped_reason` | Significado |
|---|---|
| `disabled` | `delete_orphans` esta desabilitado e a chamada nao era dry-run. |
| `vm_stage_failed` | A etapa de VMs relatou falhas. Uma VM ativa que falhou ao reconciliar nao recebe o `run_id` e pareceria orfa. |
| `sidecar_unavailable` | A API de sidecar de estado nao existe (plugin antigo); nao ha como verificar. |
| `sidecar_read_failed` | A leitura do sidecar falhou de forma transitoria; nao ha como verificar. |
| `run_not_found` | Nenhum sidecar no escopo possui o `run_id` informado, entao a execucao nao esta comprovada. Verificado antes de qualquer PATCH ou criacao de tag, na rota independente e no full-update. |
| `live_inventory_unavailable` | A varredura real independente nao conseguiu obter o inventario vivo de convidados do Proxmox para todos os endpoints no escopo (uma sessao falhou, um endpoint do escopo ficou sem sessao ou a consulta deu erro); a ausencia no Proxmox nao pode ser confirmada. |

Como `vm_stage_failed` e `run_id` sao informados pelo chamador, a varredura
tambem verifica no proprio Proxmox: uma candidata so recebe soft-delete quando o
convidado (nome do cluster, vmid e tipo) esta confirmado como ausente dos
recursos vivos do cluster nas sessoes do escopo. A varredura real independente
busca esse inventario nas sessoes selecionadas por `endpoint_ids` e falha de forma
fechada com `live_inventory_unavailable` quando falta algum; o full-update busca um
inventario novo na fronteira da varredura (nao o instantaneo do inicio da chamada, de
modo que um convidado criado durante a execucao e visto) e falha de forma fechada do
mesmo jeito. Uma linha de recurso de convidado cujo tipo ou vmid nao pode ser
determinado (derivado de ids como `qemu/123` quando faltam campos) tambem torna o
inventario indisponivel. Uma candidata ainda presente e ignorada e
registrada (`still_present_in_proxmox`), assim como uma cujo sidecar nao tem nome
do cluster, vmid ou tipo (`identity_incomplete`). Dry-runs nao buscam o inventario.

Logo antes de cada PATCH a varredura le a VM novamente e monta a lista de tags a partir
das tags atuais mais o marcador, preservando tags adicionadas depois da descoberta. Ela
ignora a VM com `vm_unreadable` quando nao pode le-la e com `already_swept` quando o
marcador ja existe. O NetBox nao oferece adicao atomica de tag, entao resta uma janela
muito pequena entre essa leitura e o PATCH.

Uma varredura pulada nao envia PATCH e nao cria a tag marcadora. Imediatamente
antes de cada PATCH a varredura le novamente o sidecar da VM e a pula (contada
como ignorada, reportada como `restamped`) quando o sidecar ja traz o `run_id`
desta execucao ou mudou desde a descoberta; o NetBox nao oferece
compare-and-set, entao isso reduz a janela de corrida sem eliminá-la. Uma VM que
reaparece e readotada pela etapa de VMs em lote, que remove a marca e restaura o
status em um unico PATCH. Quando ha
escopo, um sidecar sem `proxmox_endpoint_raw_id` valido nunca e candidato. Por
isso a varredura real independente exige os mesmos IDs de endpoint usados na
execucao do chamador (apenas um full-update sem restricao varre sem escopo). Um full-update
restrito por `endpoint_ids`, `proxmox_endpoint_ids`, `name`, `domain` ou
`ip_address` deriva o escopo das sessoes Proxmox realmente usadas.

Os IDs de endpoint sao os mesmos gravados pela etapa de VMs em cada sidecar (o
ID do endpoint da sessao Proxmox); use os valores das requisicoes das etapas. O
resultado `complete` SSE da etapa de VMs traz apenas `count`; um chamador por
etapas le a contagem de falhas no campo `failed` do resumo de fase
`virtual-machines`.

### VMs desativadas nas etapas seguintes

As etapas de discos virtuais, snapshots, interfaces de VM e IPs de VM ignoram
VMs com status `decommissioning` ou com a tag `proxbox-soft-deleted`, registrando
uma linha INFO com a quantidade ignorada. A etapa de VMs continua processando
essas VMs, de modo que um guest que reaparece seja readotado. Quando a etapa de
discos nao encontra o guest no Proxmox, registra um aviso (nao um erro) e conta
a VM como ignorada.

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
- `GET /virtualization/virtual-machines/{netbox_vm_id}/backups/create/stream`

Comportamento principal:

- Descobre conteudo de backup no storage do Proxmox.
- Mapeia backups para VMs do NetBox.
- Cria objetos de backup no modelo do plugin NetBox.
- Trata duplicidade.
- Pode remover backups que nao existem mais na origem Proxmox quando
  `delete_nonexistent_backup=true`.

As rotas direcionadas e as selecoes por `netbox_vm_ids` resolvem cada VM do
NetBox para seu dono exato `(ID do endpoint Proxmox, nome normalizado do cluster,
VMID Proxmox)`. A descoberta consulta somente esse endpoint e cluster; ela nunca
amplia o escopo selecionado para outro endpoint que reutilize o mesmo VMID.
Propriedade ausente, sessao do dono indisponivel ou varias VMs selecionadas
reivindicando a mesma identidade falham de forma fechada, sem adivinhacao. A
reconciliacao identifica cada backup pela VM dona no NetBox mais o `volume_id`,
portanto volume IDs iguais pertencentes a VMs diferentes continuam independentes.

A remocao de registros obsoletos e limitada as VMs cuja descoberta no
endpoint/cluster dono terminou com sucesso. Qualquer falha de descoberta em
node/storage torna a execucao parcial e suprime a etapa de remocao de backups.
Por outro lado, uma descoberta totalmente bem-sucedida que encontra zero
backups e autoritativa e pode remover registros obsoletos, mas somente para as
VMs com cobertura comprovada dentro do escopo solicitado.

## Fluxo de Snapshot

Endpoints:

- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/snapshots/create/stream`

Comportamento principal:

- Descobre snapshots para VMs do NetBox mapeadas para VM IDs do Proxmox.
- Reconcilia objetos de snapshot no modelo do plugin NetBox.
- Resolve registros de storage relacionados quando possivel.

As rotas direcionadas e as selecoes por `netbox_vm_ids` preservam o escopo de
propriedade exato da VM do NetBox, endpoint Proxmox, cluster e VMID. Somente a
sessao do endpoint correspondente pode ser consultada. Uma sessao dona ausente
ou ambigua, ou um node nao resolvido, falha de forma fechada para aquela VM, sem
fallback para outro endpoint com o mesmo VMID. A reconciliacao de snapshots
tambem inclui a VM dona no NetBox na identidade de lookup, evitando patches
entre donos quando nomes e VMIDs colidem.

Com `delete_nonexistent_snapshot=true`, a limpeza de registros obsoletos tem
escopo por dono e so e habilitada para uma VM depois que sua descoberta de
snapshots termina com sucesso. Uma falha parcial de endpoint, node ou fetch
suprime a limpeza destrutiva para aquele dono. Uma descoberta vazia e totalmente
bem-sucedida pode remover snapshots obsoletos daquela VM exata no NetBox;
snapshots de VMs fora do escopo com cobertura comprovada nao sao alterados.

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
  `PROXBOX_GUEST_AGENT_TIMEOUT` somente (padrao 15 s, intervalo 1-600; nao e
  configuracao do plugin NetBox) em vez do timeout curto de sessao, e tenta novamente uma vez em caso de
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
