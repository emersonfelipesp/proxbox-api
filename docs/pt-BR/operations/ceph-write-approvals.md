# Aprovacao e Recuperacao de Escritas Ceph v2

As escritas Ceph v2 operam em modo fail-closed. O cliente deve criar um plano
duravel para um endpoint Proxmox explicito, obter aprovacao de outro ator e
consumir o token de uso unico antes de `proxbox-api` despachar qualquer mutacao.
Planejamento e reconciliacao permanecem somente leitura.

A execucao tambem fica desabilitada por padrao no limite do servico. Tanto
`PROXBOX_ENABLE_CEPH_V2_WRITES=true` quanto
`PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY=true` sao obrigatorios para capability,
approval ou apply concederem autoridade de mutacao. O segundo flag e uma
atestacao do operador de que um gateway autenticado substitui
`X-Proxbox-Actor`; nao substitui a implantacao e validacao desse gateway.

Este guia descreve o contrato de seguranca da issue #258. As rotas de inventario
v1 em `/ceph/*` nao foram alteradas.

## Limite de seguranca

O fluxo protege contra:

- escolha da primeira sessao Proxmox disponivel em vez do endpoint informado;
- capacidade de escrita anunciada para endpoint ausente, desabilitado, ambiguo
  ou com `allow_writes=false`;
- revogacao da permissao depois da aprovacao e antes de uma mutacao posterior;
- troca de operacoes, branch, provider, endpoint, solicitante ou payload
  canonico durante o apply;
- confirmacoes previsiveis, vazamento do token pelo banco, expiracao, replay e
  entrega concorrente duplicada;
- autoaprovacao; e
- perda da resposta HTTP seguida de repeticao cega da mutacao.

A chave de API do servico continua sendo a autenticacao externa.
`X-Proxbox-Actor` e somente uma assercao delegada; esta mudanca nao autentica a
pessoa indicada. Um gateway NetBox confiavel e autenticado deve deriva-lo do
principal autenticado e substituir qualquer valor enviado por cliente nao
confiavel. `proxbox-api` exige o header em plan, approval e apply, rejeita
valores vazios e rejeita `actor` no corpo que divergir do header.

**O rollout de producao esta bloqueado ate essa assercao do gateway ser
implantada e validada.** Mantenha os dois flags de execucao Ceph falsos ate
entao. Um cliente direto que escolhe a chave do servico e os dois headers de
ator nao satisfaz aprovacao independente por duas pessoas.

Os limites de validacao e erro de provider do Ceph v2 devolvem diagnosticos
fixos. Valores rejeitados, nomes arbitrarios de campos extras e texto bruto de
excecoes SDK/HTTP nao sao refletidos em API, SSE, auditoria ou logs. Payloads
estruturados sao sanitizados recursivamente para variantes normalizadas em
snake/camel/kebab/com espacos de aliases de segredos, incluindo `access_key`,
`rgw_access_key`, `accessKey`, `apiKey`, `api_token`, `access_token`,
`client_secret` e `privateKey`; `credential_ref` so e mantido quando for um
identificador opaco valido. Excecoes e valores nao-JSON do provider passam pela
mesma fronteira de redacao; o fallback textual bruto nunca e persistido. O logger aplica a mesma regra
a argumentos de formatacao tardia, extras estruturados, credenciais em
userinfo/query de URL, excecoes aninhadas e traceback antes do handler real
renderiza-los.

## Registros duraveis

| Tabela | Finalidade |
|---|---|
| `ceph_plan` | Payload canonico, digest SHA-256, endpoint, revisao estavel da configuracao calculada com chave do servidor, solicitante, branch e validade de 15 minutos. O apply recarrega e valida todos os campos de identidade e o digest. |
| `ceph_approval` | Uma autoridade de aprovacao por plano. Guarda apenas o hash SHA-256 do token, digest, endpoint e revisao, solicitante, aprovador distinto, expiracao, consumo e operation-run. |
| `ceph_operation_run` | Resultado duravel, revisao do endpoint, referencias de tarefas do provider, lease da execucao em curso e nonce aleatorio nao exposto do dono do lease. Os campos de vinculo do plano/aprovacao sao fixados na criacao; atualizacoes de ciclo alteram apenas lease, status e resultado. |
| `ceph_operation_event` | Checkpoints ordenados e append-only: consumo, intencao antes do SDK, submissao do UPID e transicao terminal ou `outcome_unknown`. `(run_id, sequence)` e unico. |
| `ceph_provider_task_claim` | Posse permanente e global ao provider de cada `(provider, provider_task_ref)`; `endpoint_id` permanece apenas como contexto de auditoria. A constraint unica e o primeiro evento de submissao fazem commit atomico, impedindo reutilizacao sequencial ou concorrente inclusive entre endpoints. A migracao reconstrui tabelas legadas sem colisao em uma transacao. Se o mesmo ref legado aparecer em endpoints distintos, o startup recusa com `ceph_provider_task_claim_cross_endpoint_collision` e preserva toda a evidencia para investigacao. |

O token bruto aparece somente na resposta de criacao da aprovacao. Ele nunca e
persistido, incluido no resumo da operacao, registrado em log ou devolvido no
replay. IDs de plano/aprovacao nao sao credenciais. Booleanos legados,
`confirm_destructive`, `confirmation_token` e strings derivadas do ID do plano
nao autorizam escrita.

Os tempos de seguranca sao limitados e voltam aos padroes quando o valor e
invalido ou nao finito: `PROXBOX_CEPH_TASK_TIMEOUT` usa 300 segundos (1–3600),
`PROXBOX_CEPH_TASK_POLL_INTERVAL` usa 1 segundo (0,1–60) e
`PROXBOX_CEPH_RUN_LEASE_SECONDS` usa 360 segundos (1–3600). Mantenha o lease
maior que uma requisicao individual de status. O worker de polling renova o
lease enquanto a tarefa esta ativa, mas nao pode recupera-lo depois de expirar.

## Fluxo obrigatorio

1. Consulte
   `GET /ceph/v2/capabilities?provider=proxmox&endpoint_id=<id>`. `apply=true`
   exige os dois flags de execucao, SDK de escrita, exatamente um endpoint local
   existente/habilitado, exatamente uma sessao privada criada do schema completo
   desse endpoint e `allow_writes=true`. Sem endpoint, a capacidade e somente
   leitura/plan (`apply=false`).
2. Crie o plano com `POST /ceph/v2/plans` ou `/ceph/v2/plan`, header
   `X-Proxbox-Actor`, `provider="proxmox"` e `endpoint_id` explicito. A resposta
   persistida inclui `id`, `digest`, `endpoint_id`,
   `endpoint_config_revision`, `requester`, `created_at` e `expires_at` e e a
   unica autoridade do apply. A revisao e um HMAC opaco da configuracao completa
   relevante a mutacao; nenhum segredo do endpoint e persistido nela.

   `netbox-ceph` resolve o endpoint do cluster no plugin para o ID canonico do
   endpoint SQLite do proxbox-api por
   `netbox_proxbox.views.backend_sync.resolve_backend_endpoint_id`; a PK do
   plugin NetBox nao pode ser usada como substituta. O request canonico envia
   `provider="proxmox"` e `endpoint_id` no nivel superior e exatamente um
   objeto desejado com `node` imutavel acima do `payload` tipado. Mapeamento
   ausente ou nao positivo falha antes do planejamento, sem fallback.

   Cada operacao Proxmox nao-noop do plano canonico deve vincular um node exato
   no campo superior `node`. Um payload desejado legado pode fornecer `node`,
   mas o planejamento o extrai dos argumentos do SDK e o persiste como binding
   imutavel da operacao. Node ausente, fora do endpoint selecionado, conflitante
   entre estado live/desejado ou ambiguo entre varios nodes bloqueia o plano. O
   adapter nunca escolhe o primeiro node e nunca inventa `localhost`. Deletes
   preservam o binding do node mesmo quando `after_summary` fica vazio.

   O planejamento valida `after_summary` com schema Pydantic estrito para o par
   exato `(kind, action)`, e o sink valida novamente. Campos desconhecidos e
   obrigatorios ausentes bloqueiam o plano em vez de serem removidos no
   dispatch. Por exemplo, create de OSD exige `dev`, e update de OSD exige o
   booleano `in`. Callers, inclusive `netbox-ceph`, devem enviar o node exato e
   somente os argumentos SDK documentados para o par pretendido.
3. Outro ator envia `POST /ceph/v2/plans/{plan_id}/approvals`, o mesmo
   `endpoint_id` no corpo e seu `X-Proxbox-Actor`. O plano deve estar valido,
   atual, sem bloqueios e ainda autorizado. Uma restricao unica permite apenas
   uma aprovacao por plano. O token opaco e exibido uma vez e expira em ate dez
   minutos, nunca depois do plano.

   Um segundo POST de approval retorna 409 `approval_already_issued` com
   metadados de recuperacao validados: ID da approval, ID/digest exatos do plano,
   endpoint/revisao, solicitante/aprovador e `operation_run_id` quando consumida.
   O token e seu hash nunca sao repetidos.
4. O solicitante original envia `POST /ceph/v2/plans/{plan_id}/apply` (ou o
   alias `/ceph/v2/apply`) com `plan_id`, o mesmo `endpoint_id`,
   `approval_token` e seu header de ator. Apply inline esta fechado.

O consumo usa um update condicional e cria o audit run `running` na mesma
transacao. Duas requisicoes concorrentes nao vencem juntas. Imediatamente antes
de cada chamada SDK que realmente muta o provider, o adapter recarrega o gate
do endpoint. Revogar `allow_writes`, desabilitar/remover o endpoint ou perder a
sessao unica interrompe a proxima mutacao. O adapter compara a revisao estavel
persistida e tambem, em tempo constante, um HMAC privado com chave aleatoria por
requisicao dos campos de conexao, autenticacao, TLS, timeout e retry do endpoint
e da sessao real. Qualquer mudanca interrompe a proxima mutacao. `noop` nao chama
o provider.

Antes de cada chamada SDK, o motor persiste a intencao `dispatching` enquanto o
run ainda possui um lease vivo. Um heartbeat em background renova esse lease
durante todo o await do SDK. A consulta de freshness do endpoint e o heartbeat
serializam em um lock compartilhado e nunca usam a mesma sessao SQLModel em
concorrencia. Cada checkpoint posterior usa compare-and-swap
exigindo o mesmo nonce nao exposto de dono e lease nao expirado; worker tardio
nao pode acrescentar evento, terminalizar ou readquirir um run que nao possui.
Um crash deixa um run `dispatching` nao terminal e auditavel ate a expiracao do
lease, quando somente a recuperacao por status/SSE o muda para
`outcome_unknown`. UPID significa apenas `submitted`. O mesmo node e a mesma
sessao sao consultados ate `stopped/OK` (`completed`) ou erro terminal
(`failed`). Falha de transporte, timeout ou cancelamento permanece
`outcome_unknown`.

Cada mutacao Proxmox baseada em tarefa deve retornar exatamente uma estrutura UPID
completa (node, tres campos hexadecimais limitados de processo/tempo, tipo,
identificador da tarefa, usuario autenticado e comentario opcional). O node do
resultado e o node embutido no UPID devem ser iguais ao node imutavel do plano,
e o UPID completo nunca pode ter sido reivindicado por outro run do provider,
independentemente do endpoint. Referencia ausente,
parcial, multipla, reutilizada ou inconsistente com o node acrescenta
`provider_task_binding_invalid` e torna o run `outcome_unknown`; uma string que
apenas comeca com `UPID:` nao e evidencia de execucao. Ausencia de task ID nunca
significa sucesso. Somente os pares comprovados pelo proxmox-sdk
`flag:create`, `flag:update`, `flag:delete` e `osd:update` aceitam uma resposta
`None` bem-sucedida como conclusao sincrona tipada. Todos os demais pares
continuam baseados em tarefa; `None` vira `outcome_unknown`. A transacao atomica
de claim/submissao e o checkpoint de conclusao sincrona usam um shield repetido:
cada novo `cancel()` e lembrado, a tarefa duravel interna termina primeiro e so
depois o cancelamento e propagado. Se o cancelamento chega apos o aceite do
provider, o motor termina os checkpoints de evidencia e de cancelamento
conservador `outcome_unknown` antes de propaga-lo.

## Falha e recuperacao

Erros usam `detail.reason` estavel:

| HTTP | Reason | Acao |
|---|---|---|
| 503 | `ceph_write_execution_disabled` | Mantenha approval/apply parados; implante e valide o gateway confiavel antes de habilitar deliberadamente os dois flags. |
| 400 | `actor_required` | Envie um `X-Proxbox-Actor` confiavel e nao vazio. |
| 403 | `endpoint_disabled`, `endpoint_writes_disabled` | Mantenha a escrita parada; corrija a politica deliberadamente. |
| 403 | `approval_requester_mismatch` | Somente o solicitante persistido pode aplicar. |
| 404 | `endpoint_missing` | O seletor duravel nao resolve mais. |
| 409 | `endpoint_configuration_changed`, `endpoint_session_ambiguous`, `endpoint_session_binding_mismatch`, `endpoint_session_binding_changed` | O mesmo ID foi redirecionado ou o schema exato do endpoint/sessao mudou. Preserve o registro e crie plano novo apos correcao deliberada; nunca use sessao generica ou colidente. |
| 409 | `plan_integrity_failed` | Preserve para investigacao e crie um plano novo. |
| 409 | `two_person_approval_required` | O aprovador deve ser outro ator. |
| 409 | `approval_already_issued` | Use os metadados validados e a rota de status; plano novo e necessario apenas para uma nova tentativa deliberada. |
| 409 | `approval_invalid`, `approval_plan_mismatch`, `approval_endpoint_mismatch` | A credencial e ausente, desconhecida ou vinculada a outra autoridade. |
| 409 | `approval_replayed` | Consulte o run original pelos IDs de recuperacao; nao repita a mutacao. |
| 409 | `canonical_plan_required`, `persisted_plan_required` | Apply inline ou com substituicao de payload esta fechado. |
| 410 | `plan_expired`, `approval_expired` | Crie e aprove independentemente um plano novo. |

Se a resposta do apply for perdida, repetir a mesma requisicao retorna 409
`approval_replayed` com `approval_id`, `plan_id` e `operation_run_id`. Consulte
`GET /ceph/v2/operations/{operation_run_id}` e seus eventos ordenados, ou o SSE
`GET /ceph/v2/operations/{id}/events`. `GET /ceph/v2/approvals/{approval_id}`
retorna somente metadados seguros e nunca o token ou seu hash. Para run
`running`/`dispatching` cujo lease expira, a proxima leitura de status/SSE muda
atomicamente o estado para `outcome_unknown`, acrescenta `run_lease_expired` e
preserva as referencias de tarefa com uma acao explicita de recuperacao.
Recuperacao limpa o dono do lease. Uma resposta tardia do SDK ou polling nao
pode readquirir o lease expirado, acrescentar evento terminal nem substituir a
recuperacao por `completed`. Enquanto o lease ainda esta vivo, perder sua posse
nao terminaliza o run prematuramente: o worker autorizado ou a recuperacao apos
expiracao controla essa transicao. `completed` so aparece depois de sucesso
sincrono declarado ou `stopped/OK`;
`failed` e falha terminal conhecida. Em `outcome_unknown`, nao repita a escrita:
use reconcile somente leitura e a referencia da tarefa para descobrir o estado
real. Uma correcao sempre exige plano e aprovacao novos.

## Reconciliacao somente leitura

`POST /ceph/v2/reconcile` apenas le estado do provider e registra o resumo. Nao
chama o writer nem consome aprovacao. Todo novo tipo de mutacao deve passar pelo
gate comum por mutacao e pelo motor de aprovacao duravel. Dashboard e external
anunciam `apply=false`, `destructive_operations=false` e capabilities de
mutacao falsas; seus sinks tambem rejeitam mutacoes. Permanecem fechados ate
existirem seletor duravel, revisao de configuracao, autoridade de credencial e
gate fail-closed equivalente. Leitura, plan, metrics e reconcile nao implicam
autoridade de mutacao.

## Upgrade e rollback

A mudanca de schema e aditiva: o startup cria `ceph_plan`, `ceph_approval`,
`ceph_operation_event` e `ceph_provider_task_claim`; adiciona a revisao de configuracao a planos, approvals e
runs; e adiciona o lease e o `lease_owner` nullable a tabelas antigas de
`ceph_operation_run`. Planos ou
approvals Proxmox legados sem revisao nao sao autoridade valida e falham
fechados. Eventos legados que carregam task ID sao convertidos de forma
idempotente em claims permanentes antes de novo trafego apply. Nenhum
`allow_writes` e alterado e o historico permanece.

Ordem de rollout:

1. Faca backup do SQLite e mantenha `allow_writes=false` para Ceph.
2. Implante o backend e valide leitura, capability por endpoint, plan/get e
   reconcile somente leitura em staging.
3. Implante o cliente/UI com create → approval independente → apply e
   recuperacao por operation-run. Atualize o caller para enviar um node exato e
   payload estrito por kind/action para toda mutacao Proxmox. Prossiga somente
   depois que o gateway autenticado
   substituir `X-Proxbox-Actor` pelo principal confiavel.
4. Defina `PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY=true` somente apos essa validacao;
   depois defina `PROXBOX_ENABLE_CEPH_V2_WRITES=true` em staging. Habilite um
   endpoint, aplique plano nao destrutivo e prove que o replay nao chama o SDK
   novamente.
5. Complete a matriz de tipos destrutivos e revogacao antes de habilitar
   producao deliberadamente.

Para rollback, primeiro bloqueie trafego de approval/apply e desabilite a
autoridade de escrita enquanto a versao protegida ainda roda. Preserve tabelas
e runs. Uma versao anterior nao pode receber trafego Ceph v2 apply; isole ou
desmonte a rota antes de reverter o codigo. Tabelas e colunas aditivas podem
permanecer para o proximo roll-forward.

## Evidencia NPR 7150.2D Capitulo 4

A issue #258 e este guia produzem **somente evidencia de lifecycle no escopo da
feature**. Eles nao estabelecem conformidade do projeto, certificacao ou
acreditacao NASA, classificacao do software, nem atendimento de qualquer
requisito SWE por si so. Os identificadores abaixo sao links candidatos para a
autoridade do projeto avaliar contra a matriz oficial de aplicabilidade e os
planos aprovados.

### Evidencia produzida por esta mudanca

| Fase | Links SWE candidatos | Artefato limitado |
|---|---|---|
| Requisitos e risco | SWE-050, SWE-051, SWE-053, SWE-054, SWE-055, SWE-184 | Issue/change record, limite de ameacas, criterios de aceite e testes de substituicao de endpoint, replay, segredo e cancelamento. Nao e baseline completo de requisitos nem matriz bidirecional de rastreabilidade do projeto. |
| Arquitetura e design | SWE-057, SWE-058 | Fluxo documentado plano → approval → consumo atomico → revisao/sessao do endpoint → eventos/lease. Nao se alega revisao formal de arquitetura nem baseline de design aprovado. |
| Implementacao | SWE-060, SWE-061, SWE-062, SWE-135, SWE-136, SWE-186 | Implementacao tipada Pydantic/SQLModel, migracao aditiva, comandos Ruff/compile/test, lock de dependencias e testes de concorrencia. Acreditacao de ferramentas e registros de analise/codificacao do projeto inteiro ficam fora desta mudanca. |
| Testes | SWE-065, SWE-066, SWE-068, SWE-071, SWE-187, SWE-189, SWE-190, SWE-191, SWE-192, SWE-193, SWE-211 | Testes locais focados de unidade, HTTP, concorrencia, migracao e canarios de seguranca, incluindo binding exato de node, comparacao canonica de noop sem node, rejeicao estrita de payload, CAS/nao-ressurreicao do lease, unicidade duravel de claims sequenciais/concorrentes, checkpoints seguros contra cancelamento, serializacao heartbeat/sessao, conclusao sincrona explicita e redacao recursiva em API/SSE/persistencia/log por handler real. Nao e qualificacao da plataforma alvo, matriz de hazards do projeto, disposicao completa de cobertura nem aceite de release. |
| Operacoes e manutencao | SWE-075, SWE-077, SWE-194, SWE-195, SWE-196 | Rollout, rollback, recuperacao fail-closed, campos de auditoria e gates do operador estao documentados. Aprovacao de entrega, custodia/acesso de arquivo, execucao de retencao e registros de retirada nao sao estabelecidos aqui. |

### Disposicoes explicitamente abertas

- **Aplicabilidade/classificacao:** uma autoridade responsavel do projeto deve
  atribuir a classificacao e determinar os requisitos SWE aplicaveis. Esta
  branch nao marca SWE-143 nem qualquer outro requisito como N/A.
- **Registros independentes de lifecycle:** esta branch nao fornece plano de
  gerenciamento de software aprovado, baseline de requisitos, atas de revisao
  de arquitetura/design, relatorio de status da configuracao, qualificacao ou
  acreditacao de ferramentas, nem matriz requisitos-testes do projeto.
- **Verificacao:** a corrida `AsyncSession` em Python 3.12, CI completo do repo,
  relatorio/disposicao de cobertura de branches, nova revisao adversarial
  independente e validacao do gateway/staging permanecem gates pre-merge ou
  pre-entrega.
- **Operacao no alvo:** nenhuma mutacao Ceph real nem qualificacao da plataforma
  alvo foi executada. A execucao de producao permanece desabilitada.
- **Release/entrega/arquivo:** descricao de release SWE-063, aprovacao de
  entrega, fechamento de defeitos, custodia/acesso de arquivo, execucao da
  manutencao e evidencia de retirada pertencem ao processo controlado do projeto
  e permanecem abertos.

A evidencia de release deve registrar comandos/resultados, diff revisado,
backup, endpoint de staging, IDs de approval/run com segredos redigidos e a
decisao do operador que habilitou escrita em producao.
