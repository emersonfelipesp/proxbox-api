# Visao Geral da Arquitetura

`proxbox-api` e organizado em camadas de rotas FastAPI, dependencias de sessao, servicos de sync e camadas de schema.

## Camadas de alto nivel

- Camada de API: `proxbox_api/main.py`, `proxbox_api/app/*` e `proxbox_api/routes/*`
- Camada de sessao: `proxbox_api/session/*`
- Camada de servicos: `proxbox_api/services/*`
- Camada de schemas e enums: `proxbox_api/schemas/*`, `proxbox_api/enum/*`
- Camada de persistencia: `proxbox_api/database.py`
- Camada utilitaria: streaming, logging, cache, retry e excecoes

## Componentes de runtime

- App FastAPI monta os grupos de rotas atuais:
  - `/`
  - `/cache`
  - `/clear-cache`
  - `/full-update`
  - `/ws`
  - `/ws/virtual-machines`
  - `/admin`
  - `/admin/encryption` — superficie de inspecao e rotacao da chave de criptografia.
  - `/auth` — bootstrap e gerenciamento de chaves de API.
  - `/netbox`
  - `/proxmox`
  - `/proxmox/cluster/ha/*` — leitura agregada de High-Availability entre clusters; ver [API de HA do cluster](../api/cluster-ha.md).
  - `/proxmox/{qemu,lxc}/{vmid}/{start,stop,snapshot,migrate}` — verbos operacionais de escrita (mais DELETE-para-cancelar e GET-stream para migrate). Gate em `ProxmoxEndpoint.allow_writes`. Ver [Referencia HTTP — Verbos Operacionais de VM](../api/http-reference.md#verbos-operacionais-de-vm).
  - `/dcim`
  - `/virtualization`
  - `/extras`
  - `/sync/individual`
  - `/sync/active` — probe local-ao-processo para um `/full-update` em andamento.
- Configuracao de endpoints persistida em SQLite.
- Acesso ao NetBox via clientes `netbox-sdk` sync e async.
- Acesso ao Proxmox via sessoes do SDK sync `proxmox-sdk` e wrappers tipados.
- Rotas Proxmox geradas em runtime sao montadas durante o startup da aplicacao.

## Fronteira de confianca e rastreabilidade do preflight de imagens

O preflight Packer e separado da execucao por construcao. A rota resolve
exatamente um endpoint/sessao persistido e habilitado, enquanto
`services/packer_preflight.py` recebe essa sessao e chama somente metodos GET do
Proxmox. `allow_writes` e informado, mas nao bloqueia a leitura. O servico nao
possui escrita de banco, mutacao Proxmox, SSH ou espera de task. Falhas de
criacao de sessao e checks upstream viram diagnosticos tipados fixos; excecoes,
schemas de endpoint, credenciais e output de processo nao cruzam a fronteira
HTTP ou de logs.

Read-only nao significa advisory: quando recebe o `recipe_digest` renderizado
pelo servidor, um preflight pronto emite um plano assinado de cinco minutos. O
binding da receita e um HMAC com dominio separado, nao um hash do script sujeito
a dicionario, e a configuracao do endpoint usa outra chave contextual. O plano
tambem vincula target, storages e VMID sem alterar o banco. A execucao autentica
o plano, repete os GETs, atualiza e revalida autoritativamente endpoint/API/SSH
imediatamente antes da escrita e consome o UUID uma unica vez ao adquirir o
bloqueador exclusivo `endpoint_id:vmid`.

A execucao possui uma fronteira de autoridade persistida separada. O
`ProxmoxEndpoint` local selecionado deve estar habilitado e conter um binding
completo de node/SSH. A rota deriva host, usuario, porta, caminho da identidade
e fingerprint fixado da linha persistida; campos SSH legados do request sao
apenas assercoes e nao podem redirecionar a execucao. A chave do servidor
escaneada precisa corresponder ao fingerprint persistido antes de ser entregue
ao OpenSSH com verificacao estrita. Binarios SSH absolutos, `-F none` e opcoes
que desabilitam proxy e canonicalizacao isolam a conexao da configuracao
OpenSSH ambiente. A identidade e aberta uma vez com `O_NOFOLLOW`, validada via
`fstat` como arquivo regular nao-symlink pertencente a root/servico sem
permissoes de grupo/mundo, e herdada por `/proc/self/fd`, fechando a troca
concorrente do pathname.

`CloudImageBuildOperation` persiste somente digests, target, lease, timestamps,
contadores e transicoes; nunca scripts, URLs, credenciais, cloud-init ou output
bruto. O SSH roda assincronamente numa unidade `systemd-run` unica. Timeout e
cancelamento interrompem essa unidade. Cleanup, journal e fechamento de sessao
terminam mesmo sob cancelamento repetido; cancelamento/completion usam
compare-and-swap. Exit code zero nao e sucesso: a API Proxmox precisa verificar
o artefato final. Recovery, cancelamento, estado parcial/desconhecido ou lease
expirado preservam `lease_key` como bloqueador `recovery_required`, sem delecao
ou release automatico e sem recovery destrutivo neste escopo.

`CloudImageBuildTarget` e o plano canonico de provider/storage para preflight e
renderizacao. O requisito de snippets deriva do provider; todos os providers
usam staging privado aleatorio em `/var/tmp`, e destinos ISO/snippet sao
resolvidos pelo volume ID exato com `pvesm path`, e mappings customizados nao
suportados falham na validacao. O modulo neutro
`schemas/cloud_image_security.py` possui a normalizacao SSH e elimina o ciclo
de imports entre schema e rota. A fronteira de validacao de request usa uma
resposta fixa que nunca serializa input ou contexto do Pydantic.

A mudanca deste escopo organiza evidencia local candidata de rastreabilidade
entre capitulos (SWE-052), arquitetura/design (SWE-057/SWE-058),
implementacao/testes unitarios (SWE-060/SWE-062), verificacao contra requisitos
(SWE-066) e avaliacao/status dos resultados de teste (SWE-068). Isto nao
estabelece, isoladamente, conformidade NPR 7150.2D, aprovacao ou verificacao
independente.

| Requisito | Evidencia de implementacao | Evidencia de verificacao |
|---|---|---|
| `PF-01` endpoint exato, uma sessao habilitada, sem fallback para a primeira | `routes/cloud/template_images.py::_resolve_preflight_target` | cenarios multi-sessao, ausente, desabilitado, ambiguo e erro de sessao em `tests/test_packer_preflight.py` |
| `PF-02` readiness somente leitura em endpoint sem escritas | `services/packer_preflight.py::run_packer_preflight` | fake rejeita qualquer metodo nao-GET; testes `allow_writes=false` e staging privado de release |
| `PF-03` findings estaveis de node/storage/VMID/unsupported | schemas v1, requisitos de storage por provider e `cluster/nextid?vmid=` autoritativo | shape tipado, papeis ISO/release, VMID oculto, payload malformado, colisao, unsupported e fixture |
| `PF-04` respostas de build/erro sem segredos | resposta v2, writes fixos codificados, recipes source tipados, validacao generica, resumo de execucao, diagnosticos fixos e validador de preview | canarios de delimitador/comando/422/URL/cloud-init/stdout/stderr/sessao/SDK/cleanup e rejeicao de preview durante execucao |
| `PF-05` estabilidade do produtor/OpenAPI | versoes explicitas, `vm_storage` canonico, alias legado `storage` e janela ate `0.0.21.x` | assercoes OpenAPI, testes de alias/conflito, fixture do produtor e fixture consumer-shaped explicitamente pertencente ao produtor |
| `PF-06` autoridade persistida e cleanup exato | binding endpoint/node atualizado, host key fixada, identidade fixada por descritor e cleanup resistente a cancelamento | edit concorrente, symlink/modo/owner/troca de path, argv SSH, fingerprint, cancelamento repetido e close exato |
| `PF-07` plano aprovado e owner unico | HMACs separados de endpoint/receita, claims assinados e bloqueador `CloudImageBuildOperation.lease_key` retido | oracle de credencial/receita, tamper, drift, expiracao, replay, concorrencia e recovery fail-closed |
| `PF-08` execucao duravel verificada | drain async, unidade fixa, cleanup sob cancelamento repetido, journal CAS e verificacao API final | contadores, cancelamento duplo/triplo, corrida cancel/completion, completion verificado e recovery forcado |

O storage de imagem exige `iso` apenas para `proxmox_iso`; providers
release/source usam staging privado e nao declaram capacidade de storage de
imagem. O storage de VM sempre exige `images`, e snippets so sao verificados
quando o plano normalizado derivado do provider precisa deles. O literal
`import` existe apenas como enum de request na operacao mutante
`download-url` e, intencionalmente, nao faz parte do readiness.

Status de ciclo de vida do escopo no Capitulo 4 (isto nao afirma que todos os
requisitos NPR 7150.2D se aplicam ou estao completos):

| Fase / requisito | Status | Evidencia atual | Pendente ou gap |
|---|---|---|---|
| Requisitos — SWE-053, SWE-055 | Parcial | requisitos rastreados da feature e mapa `PF-01`–`PF-06` acima | validacao pelo projeto consumidor Packer pendente |
| Arquitetura — SWE-057 | Parcial / evidencia local | fronteiras read/write e de autoridade SSH persistida documentadas | revisao independente de arquitetura e validacao aprovada de deploy pendentes |
| Design — SWE-058 | Parcial / evidencia local | contratos versionados, target normalizado, resolver exato, servico read-only e preview explicito | conformidade do consumidor e revisao independente pendentes |
| Implementacao — SWE-060, SWE-061, SWE-062 | Parcial / evidencia local | codigo da branch, Ruff/format/compile e testes focados | revisao adversarial independente e CI remoto pendentes |
| Testes — SWE-065, SWE-066, SWE-068, SWE-071 | Parcial / evidencia local | ASGI real para auth/sucesso/desabilitado/malformado/cleanup, cancelamento de sessao, fixture JSON e resultados focados | suite completa, suite downstream e CI Gitea pendentes |
| Qualificacao de modelo/simulacao — SWE-070 | N/A | nao usa software de voo nem modelo de qualificacao | nao aplicavel |
| Validacao na plataforma alvo — SWE-073 | Gap | nenhuma mutacao Proxmox/NetBox live foi autorizada | pacote/container e staging aprovado pendentes |
| Operacoes/entrega — SWE-075, SWE-077 | Parcial | docs de API/arquitetura/agentes e janela de compatibilidade | release notes, artefato publicado, deploy e pos-release pendentes |

A fixture consumer-shaped nao conta como verificacao downstream: ela pertence
ao proxbox-api e testa apenas a intencao de compatibilidade do produtor com um
modelo de teste declarado de forma independente. O parser, a fixture e a suite
reais do netbox-packer continuam em HOLD de integracao. Por isso, o rollout
falha fechado: staging e producao devem manter
`PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION` ausente/falso ate existir essa evidencia
do consumidor. Planejamento e preflight somente leitura nao sao afetados.

## Modelos de dados principais

### `NetBoxEndpoint`

- Campos: `name`, `ip_address`, `domain`, `port`, `token_version`, `token_key`, `token`, `verify_ssl`
- Suporta token NetBox v1 e v2.
- Inclui propriedade computada `url` para criar sessao NetBox.
- O comportamento singleton e aplicado na logica do endpoint de criacao.

### `ProxmoxEndpoint`

- Campos principais/API: `name`, `ip_address`, `domain`, `port`, `username`,
  `password`, `verify_ssl`, `token_name`, `token_value`, `enabled`,
  `allow_writes` e `access_methods`.
- Binding opcional para execucao Cloud Image: `ssh_target_node`, `ssh_host`,
  `ssh_username`, `ssh_port`, `ssh_identity_file` e
  `ssh_known_host_fingerprint`; builds executaveis exigem o conjunto completo.
- `domain` e opcional e `name` e unico.
- Suporta autenticacao por senha ou por par de token.

## Fluxo de startup

1. `create_app()` inicializa o banco e o bootstrap do NetBox.
2. A app monta static assets, CORS, handlers de excecao, rotas de cache, full-update e WebSocket.
3. Os routers sao incluidos para NetBox, Proxmox, DCIM, virtualization, extras e sync individual.
4. As rotas Proxmox geradas em runtime sao montadas no startup da lifespan e podem falhar em modo open, a menos que `PROXBOX_STRICT_STARTUP` esteja habilitado.
5. O OpenAPI customizado embute o contrato Proxmox gerado quando ele existe.

## Extensao de OpenAPI

`proxbox_api/openapi_custom.py` substitui a geracao de OpenAPI do FastAPI e embute metadados do OpenAPI Proxmox gerado quando disponivel:

- Arquivo-fonte: `proxbox_api/generated/proxmox/latest/openapi.json`
- Campos de extensao:
  - `info.x-proxmox-generated-openapi`
  - `x-proxmox-generated-openapi`

## Ciclo de sync

- Endpoints de sync orquestram descoberta no Proxmox e criacao de objetos no NetBox.
- Journal entries fornecem rastreabilidade.
- Endpoints WebSocket e SSE fornecem progresso em tempo real por objeto.
