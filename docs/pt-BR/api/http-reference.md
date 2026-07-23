# Referencia HTTP da API

Esta pagina resume os endpoints HTTP expostos por `proxbox-api`.

Para schemas completos de request e response, use o OpenAPI em tempo de execucao em `/docs`.

## Root e utilitarios

- `GET /` - Metadados e links do servico.
- `GET /version` - Versao do backend para invalidacao externa de cache.
- `GET /cache` - Inspeciona o cache em memoria.
- `GET /clear-cache` - Limpa caches em memoria, incluindo o cache GET do NetBox e o cache de reconcile de custom fields.

## Autenticacao (`/auth`)

Todas as requisicoes, exceto os endpoints de bootstrap, requerem o header `X-Proxbox-API-Key`. Consulte [Autenticacao](../getting-started/authentication.md) para o fluxo completo de bootstrap e gerenciamento de chaves.

- `GET /auth/bootstrap-status` - Verifica se o registro inicial de chave ainda e necessario. Isento de autenticacao.
- `POST /auth/register-key` - Registra a primeira chave de API. Isento de autenticacao; falha se ja existir uma chave.
- `POST /auth/keys` - Cria uma nova chave de API. Retorna o valor da chave uma unica vez; armazene com seguranca.
- `GET /auth/keys` - Lista todas as chaves de API. Os valores sao ocultados (apenas metadados sao retornados).
- `DELETE /auth/keys/{key_id}` - Remove uma chave de API pelo ID.
- `POST /auth/keys/{key_id}/activate` - Reativa uma chave previamente desativada.
- `POST /auth/keys/{key_id}/deactivate` - Desativa uma chave ativa sem remove-la.

## Admin

- `GET /admin/` - Dashboard HTML do admin para os registros configurados do NetBox. Esta rota fica fora do OpenAPI.
- `GET /admin/logs` - Buffer de logs em memoria com filtros opcionais para `level`, `limit`, `offset`, `since` e `operation_id`.
- `GET /admin/logs/stream` - Stream SSE de logs em tempo real. Suporta os parametros `level`, `errors_only`, `operation_id` e `newer_than_id`.

## Pipeline de imagens Cloud (`/cloud/templates/images`)

### Preflight somente leitura

`POST /cloud/templates/images/preflight` aceita os campos do contrato v1
`contract_version` (`"1.0"`), `endpoint_id`, `target_node`, `vmid`,
`image_storage`, `vm_storage` e os campos aditivos `provider`,
`snippets_required`, `snippets_storage` e `recipe_digest`. O provider e a unica autoridade para
o requisito de snippets: providers release/source exigem snippets e
`proxmox_iso` nao. `snippets_required` permanece somente como assercao de
compatibilidade v1 obsoleta; valores divergentes sao rejeitados. Clientes v1
antigos que omitem os campos aditivos preservam o readiness de
`provider="release_image"`, mas somente um request com o `recipe_digest`
renderizado pelo servidor recebe um plano executavel assinado.

A rota resolve o endpoint persistido habilitado exato e exige exatamente uma
sessao Proxmox originada do banco com o mesmo id; nunca usa a primeira sessao
como fallback. Em seguida faz somente leituras de status do node, storage do
node, `GET /cluster/nextid?vmid=...` autoritativo e recursos VM suplementares.
Assim, o preflight funciona com
`allow_writes=false`; `writes_enabled` e apenas informativo e nenhuma mutacao
Proxmox ou de banco ocorre.

A resposta v1 contem `ready`, `capabilities`, `findings` e o `recipe_digest`
recebido. Quando o readiness passa com digest, a resposta inclui `plan_id`,
`plan_digest`, `plan_token` e `expires_at`. O token assinado expira em cinco
minutos e vincula configuracao do endpoint, node, provider, storages, VMID e a
receita exata. Emitir o plano nao altera o banco; a execucao o consome uma unica
vez no journal duravel. O status de cada
capacidade e `passed`, `failed` ou `unsupported`. Cada finding contem somente
`code`, `severity`, `target` e `message`; excecoes upstream e credenciais do
endpoint nunca sao retornadas. Payloads de colecao malformados falham de forma
fechada como `unsupported`, e o storage so passa quando `enabled` e `active`
estao explicitamente saudaveis. As verificacoes cobrem sessao exata, node
online, `images` no storage de VM, `iso` no storage de imagem para
`provider="proxmox_iso"`; builds release/source usam staging privado em
`/var/tmp` e nao declaram capacidade de storage de imagem. `snippets` e
verificado quando o provider exige, junto com a disponibilidade do VMID pelo probe
autoritativo. A enumeracao de recursos pode confirmar uma colisao visivel, mas
nunca transforma negacao ou payload malformado de `nextid` em sucesso.

`import` nao e um tipo de conteudo configurado do storage Proxmox. O pipeline
envia `content=import` apenas no POST separado de `download-url`; o preflight
valida a capacidade configurada real `images` e nao exige uma entrada
`import`.

### Privacidade da resposta de build

O fluxo executavel possui tres etapas: renderizar com `execute=false` para
obter `recipe_digest`, um HMAC opaco com dominio separado e nunca um hash bruto
do script; executar o preflight com esse binding para obter o `plan_token`; e
enviar `execute=true` com `preflight_plan_token`. A ultima etapa revalida
assinatura, expiracao, binding de configuracao com chave separada e receita,
repete o preflight, atualiza autoritativamente endpoint/SSH imediatamente antes
da escrita e adquire atomicamente o bloqueador exclusivo `endpoint_id:vmid`.
Replay, drift e expiracao retornam codigos fixos; recovery, cancelamento, estado
desconhecido ou lease de execucao expirado retornam
`build_target_recovery_required` e continuam bloqueados sem release automatico.

`POST /cloud/templates/images` retorna o contrato v2. Respostas padrao e de
execucao omitem URLs de imagem/source, cloud-init gerado, scripts de first boot
e build, lista de comandos, stdout e stderr. A execucao expoe somente
diagnosticos tipados fixos, exit code e contadores limitados de bytes/linhas.
Falhas inesperadas de execucao ou do SDK Proxmox direto sao normalizadas sem
retornar ou registrar o texto bruto da excecao. Falhas diretas do SDK retornam
um detalhe `502` fixo com codigo `proxmox_build_failed`; falhas de cleanup nao
substituem uma resposta valida.

Material renderizado bruto so pode ser retornado em preview nao executavel com
`execute=false` e `include_sensitive_preview=true`. Ele fica em
`sensitive_preview`, e marcado como sensivel e pode conter credenciais, URLs
assinadas, chaves ou segredos de cloud-init; nao registre nem persista esse
objeto. A validacao rejeita preview sensivel com `execute=true` ou `execute`
omitido, tornando-o impossivel durante a execucao normal.

Requests executaveis derivam a autoridade SSH exclusivamente do
`ProxmoxEndpoint` persistido. Ele precisa estar habilitado, permitir escrita,
usar `access_methods="api_ssh"` e conter `ssh_target_node`, `ssh_host`,
`ssh_username`, `ssh_port`, `ssh_identity_file` e
`ssh_known_host_fingerprint`. Campos SSH legados do request sao apenas
assercoes; qualquer divergencia retorna `409` antes de subprocesso. A chave do
servidor e escaneada, comparada ao fingerprint SHA-256 persistido e entregue ao
OpenSSH com verificacao estrita.
O arquivo de identidade precisa ser regular, nao symlink, pertencer a root/conta
do servico e nao ter permissoes de grupo/mundo. Ele e aberto uma vez com
`O_NOFOLLOW`, verificado com `fstat` e herdado como `/proc/self/fd/<fd>`, de modo
que troca concorrente do pathname nao substitui a chave.
O processo SSH usa binarios absolutos, `-F none`, `ProxyCommand=none`,
`ProxyJump=none` e `CanonicalizeHostname=no`, impedindo que configuracao
ambiente redirecione a conexao.

A execucao usa `asyncio` e uma unidade `systemd-run` unica gerada pelo servidor.
Stdout/stderr sao drenados continuamente apenas para contadores, sem acumular,
registrar ou persistir conteudo. Timeout, cancelamento do request ou `POST
/cloud/templates/images/operations/{id}/cancel` interrompem a unidade exata;
`GET /cloud/templates/images/operations/{id}` expoe o journal sem segredos. Um
cleanup local/remoto, transicao do journal e fechamento da sessao terminam mesmo
sob cancelamento repetido. Cancelamento e completion usam compare-and-swap para
que um request stale nao sobrescreva o vencedor. Um
exit code zero permanece `verification_pending` ate a API Proxmox confirmar o
artefato. Estado parcial ou desconhecido vira `recovery_required` e nunca e
apagado automaticamente. Recovery, cancelamento, estado desconhecido e lease
expirado retêm o bloqueador ate um futuro fluxo explicito de reconciliacao; este
escopo nao expoe recovery destrutivo.

### Bloqueio de rollout do consumidor

A fixture `tests/fixtures/netbox_packer_preflight_v1.json` pertence ao
proxbox-api. Ela valida um parser com shape de consumidor mantido neste repo
produtor e, portanto, registra apenas a intencao de compatibilidade; nao prova
que o netbox-packer implementou ou validou o contrato. Ate existir contrato e
teste reais no consumidor, staging e producao devem manter
`PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION` ausente ou falso. Planejamento e
preflight somente leitura permanecem disponiveis enquanto a execucao falha de
forma fechada.

User-data, first-boot, metadata e authorized keys sao codificados em base64 em
comandos de escrita fixos, portanto conteudo do caller nao pode encerrar um
delimitador shell. Caminhos de snippets e ISO sao resolvidos pelo volume ID
exato com `pvesm path`; mappings customizados de `snippets_dir` sao rejeitados.
`source_build_command` aceita somente as assercoes tipadas
`pfsense_memstickserial` e `opnsense_dvd`. O catalogo controla wrappers fixos em
`/usr/local/libexec/proxbox`, roots canonicos em
`/opt/proxbox/image-sources` e artefatos fixos dentro desses roots. Caminhos do
caller sao apenas assercoes. O script valida `realpath`, owner root e ausencia
de escrita por grupo/outros. Todos os providers usam staging aleatorio privado
via `mktemp`.

`vm_storage` e a unica autoridade para o storage de disco. O nome legado
`storage` continua aceito como alias durante `0.0.21.x`, e normalizado para
`vm_storage`; enviar os dois com valores diferentes e rejeitado. O alias nao e
emitido no OpenAPI nem nas respostas. Quando ambos sao omitidos, o pipeline
preserva o destino legado `local-lvm`; valores explicitos nunca sao trocados.

Respostas de validacao de request sao genericas e nunca incluem `input` ou
contexto do Pydantic, campos do request, URLs assinadas, chaves ou user-data. O
caller recebe um `422` estavel com `request_validation_error` e deve validar
detalhes localmente contra o OpenAPI.

Janela de compatibilidade: preflight v1 e resposta segura v2 permanecem
suportados ate `0.0.21.x`. Uma substituicao breaking nao pode chegar antes de
`0.0.22.0` e deve ser documentada primeiro. Campos brutos legados de
build/output foram removidos imediatamente das respostas padrao como correcao
de seguranca e nao fazem parte dessa janela.

## Rotas NetBox (`/netbox`)

- `POST /netbox/endpoint` - Cria o endpoint NetBox singleton.
- `GET /netbox/endpoint` - Lista os registros de endpoint NetBox.
- `GET /netbox/endpoint/{netbox_id}` - Busca um endpoint pelo ID.
- `PUT /netbox/endpoint/{netbox_id}` - Atualiza o endpoint.
- `DELETE /netbox/endpoint/{netbox_id}` - Remove o endpoint.
- `GET /netbox/status` - Busca o status da API NetBox.
- `GET /netbox/openapi` - Busca o OpenAPI do NetBox.

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

- Informe `password`, ou ambos `token_name` e `token_value`.
- `token_name` e `token_value` devem ser informados juntos.
- Os nomes dos endpoints devem ser unicos.

### Sessao e descoberta

- `GET /proxmox/sessions`
- `GET /proxmox/version`
- `GET /proxmox/`
- `GET /proxmox/storage`
- `GET /proxmox/nodes/{node}/storage/{storage}/content`
- `GET /proxmox/{top_level}` onde `top_level` e um de `access`, `cluster`, `nodes`, `storage` ou `version`
- `GET /proxmox/{node}/{type}/{vmid}/config`

### Dados de cluster, node e replication

- `GET /proxmox/cluster/status`
- `GET /proxmox/cluster/resources`
- `GET /proxmox/nodes/`
- `GET /proxmox/nodes/{node}/network`
- `GET /proxmox/nodes/{node}/qemu`
- `GET /proxmox/replication`

### Alta Disponibilidade (somente leitura)

Endpoints agregados entre todos os clusters Proxmox configurados. Atendem a aba HA na pagina de detalhe da VM e a pagina de HA do cluster adicionadas pelo `netbox-proxbox` para a [issue #243](https://github.com/emersonfelipesp/netbox-proxbox/issues/243). Mutacoes (incluir/remover recurso, migrar/realocar, CRUD de grupos) ficam fora deste escopo e podem entrar em uma release seguinte.

- `GET /proxmox/cluster/ha/status` - Linhas por servico do CRM/LRM vindas de `/cluster/ha/status/current`, mais entradas de quorum/master.
- `GET /proxmox/cluster/ha/resources` - Recursos HA configurados, mesclados com o estado atual (node, CRM state, request state).
- `GET /proxmox/cluster/ha/resources/by-vm/{vmid}` - Conveniencia para uma VM/CT; tenta `vm:{vmid}` e cai para `ct:{vmid}`. Retorna `null` (nao 404) quando o guest nao esta sob HA, para que a aba do NetBox renderize estado vazio.
- `GET /proxmox/cluster/ha/groups` - Lista de grupos HA com detalhe mesclado (nodes, restricted, nofailback).
- `GET /proxmox/cluster/ha/groups/{group}` - Detalhe de um grupo unico; retorna `null` quando nenhum cluster possui o grupo.
- `GET /proxmox/cluster/ha/summary` - Envelope unico (`{status, groups, resources}`) composto em paralelo via `asyncio.gather`. Usado pela pagina HA do cluster para que cada render gere apenas um round-trip.

### Verbos Operacionais de VM

Verbos POST que atuam sobre uma unica VM QEMU ou container LXC. Implementados em `proxbox_api/routes/proxmox_actions.py`. Cada handler e protegido por [`ProxmoxEndpoint.allow_writes`](../getting-started/configuration.md#endpoints-proxmox); o gate roda antes de qualquer chamada ao NetBox ou ao Proxmox, entao um endpoint com escritas desabilitadas retorna 403 mesmo que os servicos a jusante estejam fora do ar.

Todos os verbos aceitam:

- `endpoint_id` (query parameter, obrigatorio) — seleciona o cluster Proxmox alvo entre varios.
- `Idempotency-Key` (header, opcional) — janela de cache de 60 segundos por `(endpoint_id, verb, vmid)`. Um segundo POST com a mesma chave retorna o corpo em cache sem re-despachar.
- `X-Proxbox-Actor` (header, opcional) — rotulo do ator gravado no journal entry do NetBox. Default: `proxbox-api`.

Toda invocacao (sucesso, falha ou no-op) escreve exatamente um journal entry no `VirtualMachine` correspondente do NetBox, resolvido pelo custom field `proxmox_vm_id`. Se a escrita do journal falhar, a rota falha fechada em vez de retornar sucesso sem registro de auditoria.

| Method | Path | Proposito |
|---|---|---|
| `POST` | `/proxmox/qemu/{vmid}/start` | Inicia uma VM QEMU. No-op por estado quando ja `running` (`result: "already_running"`). |
| `POST` | `/proxmox/lxc/{vmid}/start` | Inicia um container LXC. Mesma regra de no-op. |
| `POST` | `/proxmox/qemu/{vmid}/stop` | Para uma VM QEMU. No-op por estado quando ja `stopped` (`result: "already_stopped"`). |
| `POST` | `/proxmox/lxc/{vmid}/stop` | Para um container LXC. Mesma regra de no-op. |
| `POST` | `/proxmox/qemu/{vmid}/snapshot` | Cria snapshot QEMU. Corpo JSON opcional `{snapname, description}`; quando `snapname` e omitido a rota gera `proxbox-{idempotency_key[:8]}` ou `proxbox-{utc_stamp}`. Sempre despachado (sem no-op por estado). |
| `POST` | `/proxmox/lxc/{vmid}/snapshot` | Cria snapshot LXC. Mesmas regras de corpo e default. |
| `POST` | `/proxmox/qemu/{vmid}/migrate` | Migra uma VM QEMU. Corpo obrigatorio `{target, online}`. Executa um preflight contra `/nodes/{node}/qemu/{vmid}/migrate` e rejeita quando o target nao e permitido ou `online=true` esbarra em discos/recursos locais. Retorna **202 Accepted** com `proxmox_task_upid` e `sse_url` (endpoints de cancel/stream abaixo). |
| `POST` | `/proxmox/lxc/{vmid}/migrate` | Migra um container LXC. Mesmo corpo e shape 202. |
| `DELETE` | `/proxmox/qemu/{vmid}/migrate/{task_upid}` | Cancel best-effort de uma migracao em andamento. Audita a intencao de cancel mesmo que o Proxmox recuse. |
| `DELETE` | `/proxmox/lxc/{vmid}/migrate/{task_upid}` | Cancel best-effort para migrate de LXC. |
| `GET` | `/proxmox/qemu/{vmid}/migrate/{task_upid}/stream` | Stream SSE emitindo `migrate_dispatched`, varios `migrate_progress`, depois `migrate_succeeded` xor `migrate_failed`. |
| `GET` | `/proxmox/lxc/{vmid}/migrate/{task_upid}/stream` | Stream SSE para progresso de migrate de LXC. |

#### Gate `allow_writes` (formato do 403)

Quando `endpoint_id` falta, o endpoint nao existe ou `ProxmoxEndpoint.allow_writes` esta `false`, o handler retorna HTTP 403 com um dos tres codigos em `reason`:

```json
{
  "reason": "endpoint_writes_disabled",
  "detail": "Operational verbs are disabled on this endpoint. Enable ProxmoxEndpoint.allow_writes on the NetBox side after granting core.run_proxmox_action to the operator group.",
  "endpoint_id": 7
}
```

Outros formatos de 403 usam `reason: "endpoint_id_required"` ou `reason: "endpoint_not_found"` e omitem `endpoint_id`. O gate e a fronteira de confianca documentada em `docs/design/operational-verbs.md` §2.3 layer 3 — deve permanecer como o primeiro check de cada handler.

#### Shape da resposta (sucesso / no-op)

```json
{
  "verb": "start",
  "vmid": 100,
  "vm_type": "qemu",
  "endpoint_id": 7,
  "result": "ok",
  "dispatched_at": "2026-05-13T14:22:08Z",
  "proxmox_task_upid": "UPID:pve1:00012E34:...",
  "journal_entry_url": "/api/extras/journal-entries/42/"
}
```

`result` e um de `ok`, `already_running`, `already_stopped`, `accepted` (dispatch de migrate), `cancel_requested`, `cancel_failed`, `rejected` ou `failed`. Caminhos de erro adicionam `reason` e `detail`. A resposta 202 do migrate carrega tambem `sse_url`, `target`, `online` e `source_node`.

### Helpers do viewer e do contrato gerado

- `POST /proxmox/viewer/generate`
- `GET /proxmox/viewer/openapi`
- `GET /proxmox/viewer/openapi/embedded`
- `GET /proxmox/viewer/integration/contracts`
- `POST /proxmox/viewer/routes/refresh`
- `GET /proxmox/viewer/pydantic`

### Rotas live geradas em runtime

`proxbox-api` monta rotas Proxmox geradas em runtime a partir do OpenAPI embutido sob:

- `/proxmox/api2/{version_tag}/*`
- `/proxmox/api2/*` como alias de compatibilidade para `latest`

Comportamento:

- As rotas sao montadas no startup para cada versao gerada disponivel em `proxbox_api/generated/proxmox/`.
- O conjunto montado e armazenado em cache em `proxbox_api/generated/proxmox/runtime_generated_routes_cache.json`.
- Em `uvicorn --reload`, o startup prefere esse manifest de cache para preservar o conjunto montado durante o desenvolvimento.
- As rotas sao reconstruidas sob demanda com `POST /proxmox/viewer/routes/refresh`.
- `POST /proxmox/viewer/routes/refresh` sem query params reconstrui todas as versoes disponiveis.
- `POST /proxmox/viewer/routes/refresh?version_tag=8.3.0` reconstrui apenas essa versao.
- O alias sem versao `/proxmox/api2/*` encaminha para o contrato `latest`.
- Request bodies e responses sao validados com modelos Pydantic gerados em runtime.
- Os modelos gerados cobrem schemas de resposta object, array, scalar e `null`.
- Para respostas em array cujos itens sao objetos, a geracao emite `{Operation}ResponseItem` junto com `RootModel[list[{Operation}ResponseItem]]`.
- As rotas geradas aparecem no `/docs` e no `/openapi.json` do FastAPI.
- As rotas `latest` sao montadas antes de tags mais antigas para aparecerem primeiro no Swagger.
- As rotas geradas tem prioridade sobre rotas manuais `/proxmox/*` quando existe colisao de path.

Normalizacao de path parameters:

- Quando o viewer do Proxmox usa nomes de path parameters que nao sao identificadores validos do FastAPI, a rota montada usa um nome normalizado.
- Exemplo:
  - Caminho do contrato Proxmox: `/nodes/{node}/hardware/pci/{pci-id-or-mapping}`
  - Caminho montado no FastAPI: `/proxmox/api2/latest/nodes/{node}/hardware/pci/{pci_id_or_mapping}`
- A chamada via SDK proxmox-sdk continua usando o nome original do parameter do contrato gerado.

Descoberta de versao:

- Uma versao so pode ser montada quando `proxbox_api/generated/proxmox/<version-tag>/openapi.json` existe.
- Entradas como `__pycache__` e arquivos na raiz de `generated/proxmox/` sao ignorados.

Selecao de target:

- Se existir apenas um endpoint Proxmox, as rotas geradas usam ele automaticamente.
- Se existirem varios endpoints, informe um de:
  - `target_name`
  - `target_domain`
  - `target_ip_address`
- `source` define se os endpoints vem do banco local ou dos registros do plugin NetBox.

Integracao tipada do sync:

- As rotas de sync ainda chamam o Proxmox diretamente, mas passam por `proxbox_api/services/proxmox_helpers.py` com backend proxmox-sdk.
- Essa camada valida os payloads com os modelos gerados em `proxbox_api/generated/proxmox/latest/pydantic_models.py` antes de retornar para os handlers.
- Isso evita round-trips HTTP internos e mantem VM config, cluster status, cluster resources, storage listing e node storage content alinhados ao contrato usado por `/proxmox/api2/*`.

Exemplos de rotas geradas:

- `GET /proxmox/api2/latest/cluster/resources`
- `GET /proxmox/api2/8.3.0/nodes/{node}/qemu/{vmid}/config`
- `POST /proxmox/api2/latest/access/acl`
- `GET /proxmox/api2/cluster/resources` como alias de compatibilidade para `latest`

Formato da resposta de refresh:

- Resposta top-level: resumo da registracao retornado por `register_generated_proxmox_routes()` mais o campo `message`.
- `state`: snapshot aninhado retornado por `generated_proxmox_route_state()`.
- `state.mounted_versions`: versoes atualmente montadas no FastAPI.
- `state.alias_version_tag`: versao usada por `/proxmox/api2/*`.
- `state.cache_path`: caminho do manifest persistido usado para preservar as rotas entre reloads.
- `state.cache_enabled`: indica se a persistencia de cache esta habilitada para as rotas geradas.
- `state.loaded_from_cache`: indica se a ultima registracao veio do cache persistido.
- `state.route_count`: total de rotas FastAPI atualmente montadas.
- `state.versions.<tag>.route_count`: total de rotas FastAPI montadas para a versao.
- `state.versions.<tag>.path_count`: total de paths OpenAPI montados para a versao.
- `state.versions.<tag>.method_count`: total de operacoes HTTP montadas para a versao.
- `state.versions.<tag>.schema_version`: valor de `info.version` do OpenAPI gerado.

Cobertura de testes:

- `tests/test_generated_proxmox_routes.py` executa um suite exaustivo de rotas mockadas para cada operacao gerada em todas as versoes disponiveis, mais o alias `latest`.
- `tests/test_pydantic_generator_models.py` verifica os modelos gerados para payloads array, scalar, `null` e object aliasados.
- `tests/test_session_and_helpers.py` valida a camada de helpers tipados do Proxmox e confirma que os handlers de sync continuam retornando payloads validados.

## Rotas DCIM (`/dcim`)

- `GET /dcim/devices`
- `GET /dcim/devices/create` - Cria devices NetBox a partir de nodes Proxmox.
- `GET /dcim/devices/create/stream` - Variacao SSE.
- `GET /dcim/devices/{node}/interfaces/create`
- `GET /dcim/devices/interfaces/create` - Sincroniza todas as interfaces de node em todos os clusters.
- `GET /dcim/devices/interfaces/create/stream` - Variacao SSE para sync de interfaces.

## Rotas de Virtualizacao (`/virtualization`)

- `GET /virtualization/cluster-types/create` - Stub que retorna HTTP 501.
- `GET /virtualization/clusters/create` - Stub que retorna HTTP 501.
- `GET /virtualization/virtual-machines/create` - Cria VMs NetBox a partir dos recursos Proxmox.
- `GET /virtualization/virtual-machines/create/stream` - Variacao SSE.
- `GET /virtualization/virtual-machines/{netbox_vm_id}/create` - Cria uma VM unica pelo ID do NetBox.
- `GET /virtualization/virtual-machines/{netbox_vm_id}/create/stream` - Variacao SSE para sync de uma VM.
- `GET /virtualization/virtual-machines/`
- `GET /virtualization/virtual-machines/{id}`
- `GET /virtualization/virtual-machines/{id}/summary` - Stub que retorna HTTP 501.
- `GET /virtualization/virtual-machines/summary/example`
- `GET /virtualization/virtual-machines/interfaces/create`
- `GET /virtualization/virtual-machines/interfaces/create/stream`
- `GET /virtualization/virtual-machines/interfaces/ip-address/create`
- `GET /virtualization/virtual-machines/interfaces/ip-address/create/stream`
- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`
- `GET /virtualization/virtual-machines/backups/all/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/backups/create/stream`
- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/snapshots/create/stream`
- `GET /virtualization/virtual-machines/virtual-disks/create`
- `GET /virtualization/virtual-machines/virtual-disks/create/stream`
- `GET /virtualization/virtual-machines/{netbox_vm_id}/virtual-disks/create/stream`
- `GET /virtualization/virtual-machines/storage/create`
- `GET /virtualization/virtual-machines/storage/create/stream`

## Full Update

- `GET /full-update` - Executa sync de devices, storages, VMs, task history, discos, backups, snapshots, interfaces de node, interfaces de VM, IPs de VM, replications e backup routines.
- `GET /full-update/stream` - Variacao SSE.

Os tres endpoints de `virtual-disks` tambem aceitam o parametro opcional
`fetch_max_concurrency` para sobrescrever a largura do fetch de config de VM
Proxmox naquela requisicao.

## WebSocket

- `GET /` - WebSocket basico de contagem para testes de conectividade.
- `GET /ws/virtual-machines` - WebSocket para sincronizacao de VMs.
- `GET /ws` - WebSocket orientado por comandos para orquestracao de sync.

## Formato SSE

Todos os endpoints `/stream` retornam `Content-Type: text/event-stream` e emitem tres tipos de evento:

| Evento | Descricao |
|--------|-----------|
| `step` | Frame de progresso com `step`, `status`, `message`, `rowid` e `payload`. |
| `error` | Frame de erro com `step`, `status: "failed"`, `error` e `detail`. |
| `complete` | Frame final com `ok`, `message` e, opcionalmente, `result` ou `errors`. |

Headers:

- `Cache-Control: no-cache`
- `X-Accel-Buffering: no`

## Rotas Individual Sync (`/sync/individual`)

- `GET /sync/individual/node`
- `GET /sync/individual/vm`
- `GET /sync/individual/vm/{cluster_name}/{node}/{type}/{vmid}`
- `GET /sync/individual/cluster`
- `GET /sync/individual/interface`
- `GET /sync/individual/ip`
- `GET /sync/individual/disk`
- `GET /sync/individual/storage`
- `GET /sync/individual/snapshot`
- `GET /sync/individual/task-history`
- `GET /sync/individual/backup`
- `GET /sync/individual/replication`
- `GET /sync/individual/backup-routines`

## Rotas Extras (`/extras`)

- `POST /extras/custom-fields/reconcile`
- `GET /extras/bootstrap-status`
- `GET /extras/extras/custom-fields/create`

`POST /extras/custom-fields/reconcile` e a rota suportada de recuperacao para
custom fields ausentes ou com drift no NetBox. Ela ignora o cache local de
custom fields, le o NetBox ao vivo e reconcilia o inventario canonico do
Proxbox. `GET /extras/bootstrap-status` retorna o ultimo status de bootstrap
do NetBox e seus warnings. A rota GET legada com prefixo duplo continua
disponivel por compatibilidade.

## Rotas de configuracao do plugin Proxbox

Estes handlers existem em `proxbox_api/routes/proxbox/__init__.py`, mas nao estao montados atualmente em `main.py`:

- `GET /netbox/plugins-config`
- `GET /netbox/default-settings`
- `GET /settings`

Para montar essas rotas, e preciso incluir esse router no startup da aplicacao.
