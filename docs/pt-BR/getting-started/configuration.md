# Configuracao

`proxbox-api` usa SQLite para configuracao local de bootstrap e dependencias em runtime.

## Localizacao do banco

- Arquivo SQLite padrao: `$XDG_DATA_HOME/proxbox/database.db` ou
  `~/.local/share/proxbox/database.db` fora de containers; imagens publicadas
  usam um fallback interno `/data/database.db` sem definir variavel do operador.
- `PROXBOX_DATABASE_PATH` seleciona outro caminho absoluto.
- `DATABASE_URL` mantem compatibilidade com URLs absolutas `sqlite`,
  `sqlite+pysqlite` e `sqlite+aiosqlite`. URLs relativas/em memoria e todo
  delimitador de query `?` literal sao recusados.
- Se ambas as variaveis estiverem presentes, devem resolver para o mesmo
  arquivo. Nao ha regra de precedencia nem fallback para o diretorio corrente.
- ORM: SQLModel.
- Um `.startup.lock` persistente e especifico do destino serializa probe WAL,
  criacao de tabelas, validacao completa dos schemas de buckets, reservas,
  metricas e bind de chave do auth-lockout e todas as migrations entre workers.
  O SQLite instala o busy timeout de cinco segundos antes de inspecionar ou
  negociar o modo WAL. Uma falha e fatal.
- Todo destino e comparado aos locais legados implicitos para impedir que um
  banco vazio reabra o bootstrap de chave. O override exato de um startup esta
  documentado no guia de operacoes.

Veja [Operacoes do banco de dados](../operations/database.md) para configuracao
em container e systemd, migracao segura, backup e troubleshooting de startup.
- Bloqueios isolados por credencial usam `auth_lockout_buckets` versionada junto
  com reservas independentes por token em `auth_lockout_reservations`. A
  expiracao da reserva altera capacidade sem apagar evidencia de crash, e as
  linhas duraveis de falha usam particoes limitadas separadas de credencial e
  origem. A tabela legada `authlockout` permanece intacta para rollback, mas suas
  linhas ambiguas por IP nao sao importadas.

## Endpoint NetBox

A configuracao do endpoint NetBox e gerenciada por:

- `POST /netbox/endpoint`
- `GET /netbox/endpoint`
- `PUT /netbox/endpoint/{netbox_id}`
- `DELETE /netbox/endpoint/{netbox_id}`

Apenas um registro NetBox e permitido.

O modelo armazenado agora inclui:

- `token_version`: `v1` ou `v2`
- `token_key`: necessario para token v2, ignorado no token v1
- `token`: o segredo do token
- `verify_ssl`: controla a verificacao do certificado TLS em todas as chamadas HTTPS ao NetBox, incluindo buscas de runtime settings em `ProxboxPluginSettings`. Use `false` apenas em labs ou ambientes privados com certificados self-signed.

### Exemplo de token NetBox v1

```json
{
  "name": "netbox-primary",
  "ip_address": "10.0.0.20",
  "domain": "netbox.local",
  "port": 443,
  "token_version": "v1",
  "token": "<NETBOX_API_TOKEN>",
  "verify_ssl": true
}
```

### Exemplo de token NetBox v2

```json
{
  "name": "netbox-secondary",
  "ip_address": "10.0.0.21",
  "domain": "netbox.local",
  "port": 443,
  "token_version": "v2",
  "token_key": "token-name",
  "token": "<NETBOX_API_TOKEN_SECRET>",
  "verify_ssl": true
}
```

## Endpoints Proxmox

Os registros de endpoint Proxmox sao gerenciados por:

- `POST /proxmox/endpoints`
- `GET /proxmox/endpoints`
- `GET /proxmox/endpoints/{endpoint_id}`
- `PUT /proxmox/endpoints/{endpoint_id}`
- `DELETE /proxmox/endpoints/{endpoint_id}`

Regras de autenticacao para create/update:

- Informe `password`, ou ambos `token_name` e `token_value`.
- `token_name` e `token_value` devem ser enviados juntos.
- Os nomes dos endpoints devem ser unicos.

Os campos opcionais do endpoint no banco `timeout`, `max_retries` e
`retry_backoff` herdam os valores efetivos de `ProxboxPluginSettings` quando
estao nulos. Valores explicitos no endpoint prevalecem, inclusive zero retries
ou zero retry backoff. Uma carga do banco sem endpoints, ou com valores
concretos em todos eles, nao faz requisicao de configuracoes ao plugin. Quando
a heranca e necessaria, uma unica busca compartilhada usa um limite total de
tempo e retorna aos valores padrao documentados sem armazenar esse fallback
temporario no cache. Credenciais criptografadas no banco usam a mesma busca
limitada para obter a chave do plugin; se nenhuma chave de ambiente, plugin ou
arquivo local puder descriptografa-las, a carga falha com `503` e nunca envia o
ciphertext como credencial do Proxmox.

### Campo `allow_writes`

`ProxmoxEndpoint.allow_writes` (boolean, padrao `false`) atua como um gate de confianca para os [Verbos Operacionais de VM](../api/http-reference.md#verbos-operacionais-de-vm). Quando `false`, qualquer `POST` para `/proxmox/{qemu|lxc}/{vmid}/{start,stop,snapshot,migrate}` retorna `403` com `reason="writes_disabled_for_endpoint"`, mesmo que a chave de API e o `X-Proxbox-Actor` sejam validos. O campo so pode ser alterado por administradores e e auditado via journal entry. Adicionado na migracao `0037_proxmoxendpoint_allow_writes`.

### Binding SSH para Cloud Image

Builds executaveis do Cloud Image Pipeline exigem
`access_methods="api_ssh"` e um binding persistido e completo de endpoint/node:
`ssh_target_node`, `ssh_host`, `ssh_username`, `ssh_port`,
`ssh_identity_file` e `ssh_known_host_fingerprint`. Configure todos os campos
do binding juntos ou deixe todos os campos opcionais ausentes; bindings
parciais sao rejeitados. O caminho da identidade deve resolver dentro de
`PROXBOX_SSH_KEY_DIR`, e o fingerprint deve usar a forma canonica OpenSSH
`SHA256:<43 caracteres base64>`.
`ssh_port` pode ser omitido no update, mas JSON `null` explicito e rejeitado
antes que uma falha `NOT NULL` possa chegar ao banco.

O `target_node` do request deve corresponder a `ssh_target_node`. Campos SSH
legados no request sao apenas assertions opcionais: eles nao podem redirecionar
a execucao, e divergencias sao rejeitadas antes de `ssh-keyscan` ou `ssh`. A
chave do servidor obtida pelo scan deve corresponder exatamente ao fingerprint
persistido. A execucao usa binarios OpenSSH absolutos com `-F none` e desabilita
ProxyCommand, ProxyJump e canonicalizacao de hostname; configuracao SSH do
operador nao pode redirecionar a conexao.

Planos executaveis de preflight usam HMAC com uma chave derivada de
`PROXBOX_ENCRYPTION_KEY`. Configure a mesma chave em todos os workers de
producao para validar o plano de cinco minutos entre workers e restarts. Sem a
chave, o modo de desenvolvimento usa uma seed local ao processo e invalida
planos pendentes quando o processo muda.

### Exemplo com senha

```json
{
  "name": "pve-lab-1",
  "ip_address": "10.0.0.10",
  "domain": "pve-lab-1.local",
  "port": 8006,
  "username": "root@pam",
  "password": "<PASSWORD>",
  "verify_ssl": false
}
```

### Exemplo com token

```json
{
  "name": "pve-lab-token",
  "ip_address": "10.0.0.11",
  "domain": "pve-lab-token.local",
  "port": 8006,
  "username": "root@pam",
  "token_name": "api-token",
  "token_value": "<TOKEN_VALUE>",
  "verify_ssl": true
}
```

### Privilegios minimos do papel Proxmox

O usuario/token usado pelo `proxbox-api` precisa de leitura em cluster,
datastore e VMs, alem do endpoint de leitura do guest-agent QEMU para que os
IPs das VMs sejam sincronizados com o NetBox.

Privilegios minimos:

| Privilegio             | Motivo                                                         |
|------------------------|----------------------------------------------------------------|
| `Datastore.Audit`      | Listar storages e ler status.                                  |
| `Sys.Audit`            | Ler status do cluster e dos nos.                               |
| `VM.Audit`             | Ler config, snapshots, backups e replicacao das VMs.           |
| `VM.Monitor`           | Necessario para `agent network-get-interfaces` no PVE 8.       |
| `VM.GuestAgent.Audit`  | Necessario para `agent network-get-interfaces` no PVE >= 9.    |

Criar ou atualizar um papel somente-leitura a partir de qualquer no:

```bash
pveum role add NetBoxReadOnly --privs \
  "Datastore.Audit,Sys.Audit,VM.Audit,VM.Monitor,VM.GuestAgent.Audit"

pveum role modify NetBoxReadOnly --privs \
  "Datastore.Audit,Sys.Audit,VM.Audit,VM.Monitor,VM.GuestAgent.Audit"
```

Vincular o papel ao usuario/token na raiz, com propagacao:

```bash
pveum acl modify / --users netbox@pam --roles NetBoxReadOnly --propagate 1
```

!!! warning "PVE 9 separou `VM.GuestAgent.*`"

    O Proxmox VE 9 introduziu privilegios separados `VM.GuestAgent.Audit`,
    `VM.GuestAgent.FileRead`, `VM.GuestAgent.FileWrite`,
    `VM.GuestAgent.FileSystemMgmt` e `VM.GuestAgent.Unrestricted`. Um papel
    criado no PVE 8 (ou copiado de `PVEAuditor`) **nao** inclui
    `VM.GuestAgent.Audit`, e `agent network-get-interfaces` retorna HTTP 403.
    Sintoma: as VMs sincronizam, mas os IPs delas nao aparecem no NetBox. A
    correcao e adicionar `VM.GuestAgent.Audit` ao papel.

## Comportamento de sessoes em runtime

- A sessao NetBox e derivada do endpoint NetBox armazenado.
- O valor `verify_ssl` do endpoint NetBox tambem e usado nas buscas de plugin settings, entao certificados self-signed funcionam de forma consistente quando a verificacao esta desabilitada.
- As sessoes Proxmox usam por padrao registros de endpoint do banco local.
- O modo legado (`source=netbox`) continua suportado na dependencia de sessoes Proxmox.

## Resolucao de tunaveis em runtime

A maioria dos tunaveis em runtime resolvem agora na ordem **variavel de ambiente > `ProxboxPluginSettings` (pagina de configuracoes do plugin no NetBox) > padrao embutido**, via `proxbox_api/runtime_settings.py`. O TTL do cache de configuracoes e de 5 minutos, entao mudancas feitas na pagina de configuracoes do plugin entram em efeito no proximo run de sync sem precisar reiniciar o backend. Definir uma variavel de ambiente continua funcionando como override; deixa-la em branco torna a pagina de configuracoes do plugin a fonte autoritativa.

Algumas variaveis permanecem somente em nivel de processo porque sao lidas antes da conexao com o NetBox existir ou sao infraestrutura exclusiva do operador: `PROXBOX_BIND_HOST`, `UVICORN_WORKERS`, `PROXBOX_DATABASE_PATH`, o `DATABASE_URL` SQLite, `PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY`, `PROXBOX_RATE_LIMIT`, `PROXBOX_AUTH_LOCKOUT_THRESHOLD`, `PROXBOX_AUTH_LOCKOUT_SOURCE_THRESHOLD`, `PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS`, `PROXBOX_AUTH_LOCKOUT_MAX_BUCKETS`, `PROXBOX_AUTH_LOCKOUT_MAX_IN_FLIGHT`, `PROXBOX_AUTH_LOCKOUT_MAX_GLOBAL_IN_FLIGHT`, `PROXBOX_AUTH_MAX_ACTIVE_KEYS`, `PROXBOX_AUTH_LOCKOUT_HMAC_KEY` / `PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE`, `PROXBOX_TRUSTED_PROXIES`, `PROXBOX_ENCRYPTION_KEY` / `PROXBOX_ENCRYPTION_KEY_FILE`, `PROXBOX_STRICT_STARTUP`, `PROXBOX_SKIP_NETBOX_BOOTSTRAP`, `PROXBOX_GENERATED_DIR` e `PROXBOX_CORS_EXTRA_ORIGINS`. As demais mapeiam 1:1 para campos de `ProxboxPluginSettings` e podem ser editadas pela pagina de configuracoes do plugin no NetBox.

## Variaveis de ambiente

| Variavel | Padrao | Descricao |
|----------|--------|-----------|
| `PROXBOX_DATABASE_PATH` | nao definido | Caminho absoluto opcional do SQLite operacional. Caminhos relativos sao recusados e o servico nunca usa o diretorio corrente como fallback. |
| `DATABASE_URL` | nao definido | Entrada compativel para URL SQLite local absoluta, como `sqlite:////var/lib/proxbox-api/database.db`. Se usada com `PROXBOX_DATABASE_PATH`, ambas devem selecionar o mesmo arquivo. Queries e `?` literal sao recusados. |
| `UVICORN_WORKERS` | padrao da imagem `1`; producao pode alterar | Contagem usada pelo entrypoint raw. Deve ser explicitamente `1` no recovery isolado de banco novo. |
| `PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY` | nao definido | Override sensivel com valor exato `1` para um startup auditado de control plane novo enquanto outro banco legado existe. Pare os workers, defina `UVICORN_WORKERS=1`, isole trafego, registre a primeira chave, remova o override e restaure workers. Um marcador duravel impede reuso. |
| `PROXBOX_NETBOX_TIMEOUT` | `120` | Timeout da API NetBox em segundos. Aplicado ao `netbox-sdk` e as requisicoes internas. |
| `PROXBOX_NETBOX_MAX_RETRIES` | `5` | Numero de tentativas para falhas transientes do NetBox. |
| `PROXBOX_NETBOX_RETRY_DELAY` | `2.0` | Delay inicial, em segundos, para retries do NetBox. |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `1` | Maximo de requisicoes simultaneas ao NetBox. Mantenha baixo (1-2) para evitar agotar o pool de conexoes PostgreSQL do NetBox. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `4` | Maximo de fetches concorrentes de configuracao de VM Proxmox durante o sync de VMs e discos. |
| `PROXBOX_GUEST_AGENT_TIMEOUT` | `15` | Timeout por chamada (segundos, intervalo 1-600) para a requisicao `network-get-interfaces` do guest-agent QEMU. Guests com muitas interfaces (VRRP/alias) podem demorar a enumerar; aumente este valor se as buscas de interface via guest-agent expirarem. Mapeia para o campo `ProxboxPluginSettings.guest_agent_timeout`. |
| `PROXBOX_RECONCILIATION_ENGINE` | `python` | Override opcional para `ProxboxPluginSettings.reconciliation_engine`. Valores validos: `python`, `compare` e `rust`. |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `8` (sync de VM, discos) / `4` (snapshots) | Maximo de operacoes concorrentes de escrita no NetBox. O padrao varia por servico de sync. A reconciliacao de task history usa requisicoes bulk limitadas em vez de dispatch de escrita por VM. |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `8` (maioria dos fluxos) / `4` (task-history) | Maximo de operacoes concorrentes de leitura no Proxmox. O padrao varia por servico de sync. |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `8` | Override legado de concorrencia usado por alguns entrypoints de sync. |
| `PROXBOX_RATE_LIMIT` | `300` | Maximo de requisicoes por minuto por endereco IP. |
| `PROXBOX_AUTH_LOCKOUT_THRESHOLD` | `5` | Falhas permitidas por bucket composto de origem/credencial. Intervalo 1-100; valor invalido interrompe o startup. |
| `PROXBOX_AUTH_LOCKOUT_SOURCE_THRESHOLD` | `50` | Falhas permitidas entre todas as credenciais apresentadas por uma origem normalizada. Intervalo 1-100000. |
| `PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS` | `300` | Janela fixa de falhas/bloqueio em segundos. Intervalo 1-86400; valor invalido interrompe o startup. |
| `PROXBOX_AUTH_LOCKOUT_MAX_BUCKETS` | `10000` | Maximo total de linhas duraveis de falha, dividido em particoes independentes de credencial/origem. Intervalo 2-1000000. Falhas sem chave alocam somente linhas de origem. A admissao sempre usa o pool normal por origem/global e nao depende de linha de falha livre. Na saturacao, a linha expirada segura mais antiga e removida primeiro; uma rejeicao que nao pode ser persistida falha fechada e avanca a contabilidade agregada limitada. |
| `PROXBOX_AUTH_LOCKOUT_MAX_IN_FLIGHT` | `32` | Concorrência bcrypt separada por bucket. Intervalo 1-1024; esgotamento é pressão transitória e nunca incrementa falhas. |
| `PROXBOX_AUTH_LOCKOUT_MAX_GLOBAL_IN_FLIGHT` | `256` | Concorrência bcrypt global entre workers, imposta atomicamente pelas reservas compartilhadas. Intervalo 1-4096; pares distintos de origem/chave não a contornam. |
| `PROXBOX_AUTH_MAX_ACTIVE_KEYS` | `32` | Maximo de hashes de chaves de API ativas examinados por uma requisicao. Intervalo 1-1024. Se houver mais chaves ativas, a autenticacao retorna pressao temporaria de capacidade antes do bcrypt; aumente temporariamente o limite para desativar as chaves excedentes. |
| `PROXBOX_AUTH_LOCKOUT_HMAC_KEY` | não definido | Chave explícita opcional para identidades opacas; mínimo de 32 bytes. Quando ausente, o proxbox-api publica atomicamente um arquivo privado irmão por meio de arquivo temporário no mesmo diretório, flush/fsync, `os.replace` e fsync do diretório pai. Mantenha a fonte estável durante rotações da chave de criptografia. |
| `PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE` | `<banco>.auth-lockout.key` | Caminho opcional para a chave de identidade criada/lida automaticamente. O arquivo não pode ter permissões para grupo/outros e deve compartilhar o armazenamento durável do SQLite. Depois do bind do fingerprint não secreto, o startup fixa o material verificado na memória; perda/substituição é fatal no próximo startup, e mutação do arquivo após startup não altera identidades vivas. Use recovery offline `proxbox-auth-lockout rebind-key`. |
| `PROXBOX_TRUSTED_PROXIES` | vazio em raw/Granian; `127.0.0.1/32` no nginx distribuido | CIDRs/IPs de proxies reversos confiaveis, separados por virgula. Entradas invalidas interrompem o startup. Somente esses peers podem fornecer `X-Forwarded-For`, e confianca nunca ignora autenticacao ou bloqueio. O entrypoint nginx de proposito unico sempre adiciona seu hop loopback protegido. Deployments com proxy reverso externo devem configurar explicitamente os CIDRs exatos dos peers proxy e manter a porta da aplicacao privada. Execute Uvicorn/FastAPI CLI com `--no-proxy-headers`; os entrypoints distribuidos ja fazem isso. |
| `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION` | nao definido | Quando `1`, `true` ou `yes`, permite execucao SSH remota no Cloud Image Pipeline. Desabilitado por padrao. Mantenha ausente/falso em staging e producao ate o netbox-packer possuir e validar seu contrato real de consumidor; a fixture consumer-shaped local pertence ao produtor e nao remove este HOLD. |
| `PROXBOX_BACKUP_BATCH_SIZE` | `5` | Tamanho do lote de sync de backups. Reduza para diminuir a pressao de escrita no NetBox. |
| `PROXBOX_BACKUP_BATCH_DELAY_MS` | `200` | Delay em milissegundos entre lotes de backup. |
| `PROXBOX_BULK_BATCH_SIZE` | `50` | Tamanho do lote para requisicoes em massa relacionadas a VMs (volumes, backups). |
| `PROXBOX_BULK_BATCH_DELAY_MS` | `500` | Delay em milissegundos entre lotes em massa. |
| `PROXBOX_NETBOX_GET_CACHE_TTL` | `60` | TTL em segundos do cache de GETs no NetBox. `0` desabilita o cache. |
| `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES` | `4096` | Maximo de entradas armazenadas no cache de GETs do NetBox antes de eviccao LRU. |
| `PROXBOX_NETBOX_GET_CACHE_MAX_BYTES` | `52428800` (50 MiB) | Tamanho total maximo em bytes do cache de GETs do NetBox. |
| `PROXBOX_DEBUG_CACHE` | nao definido | Quando `1`, `true` ou `yes`, emite logs detalhados de hit/miss/evict do cache. |
| `PROXBOX_CUSTOM_FIELDS_REQUEST_DELAY` | `0.5` | Delay em segundos entre requisicoes na criacao de custom fields no NetBox, para evitar overruns no PostgreSQL. |
| `PROXBOX_GENERATED_DIR` | `$XDG_DATA_HOME/proxbox/generated/proxmox` | Override do diretorio de saida da CLI geradora de schema (`proxbox-schema generate`). |
| `PROXBOX_CORS_EXTRA_ORIGINS` | (vazio) | Lista de origens CORS extras, separadas por virgula. |
| `PROXBOX_EXPOSE_INTERNAL_ERRORS` | nao definido | Quando `1`, `true` ou `yes`, respostas HTTP 500 incluem detalhes internos da excecao. |
| `PROXBOX_STRICT_STARTUP` | nao definido | Quando `1`, `true` ou `yes`, falha no mount de rotas Proxmox geradas interrompe o startup. |
| `PROXBOX_SKIP_NETBOX_BOOTSTRAP` | nao definido | Quando `1`, `true` ou `yes`, nao cria o cliente NetBox padrao no startup. |
| `PROXBOX_ENCRYPTION_KEY` | nao definido | Chave secreta para criptografar credenciais em repouso. Veja [Criptografia de credenciais](#criptografia-de-credenciais) abaixo. |
| `PROXBOX_ENCRYPTION_KEY_FILE` | nao definido | Caminho opcional para arquivo local usado somente quando env e configuracao do plugin estao vazias. O fallback padrao e `<repo_root>/data/encryption.key`. |
| `PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS` | nao definido | Permite explicitamente writes de credenciais sem chave. Desligado por padrao: startup e operacoes sem credencial continuam disponiveis, mas writes de credenciais falham fechado. Use apenas em lab isolado. |

### Tratando erros de NetBox sobrecarregado

Quando o pool de conexoes PostgreSQL do NetBox esta saturado, o proxbox-api retorna erros `netbox_overwhelmed`. Para mitigar:

1. **Reduza a concorrencia**: Defina `PROXBOX_NETBOX_MAX_CONCURRENT=1` para serializar requisicoes
2. **Aumente os retries**: Mais tentativas com delays maiores dao tempo ao NetBox para recuperar
3. **Estenda o TTL do cache**: Use `PROXBOX_NETBOX_GET_CACHE_TTL=300` para reduzir fetches redundantes

A logica de retry aplica backoff agressivo (ate 30 segundos) quando erros de sobrecarga sao detectados.

## Comportamento de CORS

- Origens sao montadas a partir de endpoints NetBox mais origens de desenvolvimento padrao.
- Metodos sao liberados para todos (`allow_methods=["*"]`).

## Criptografia de credenciais

O proxbox-api armazena tokens de API do NetBox e senhas/tokens do Proxmox em um banco SQLite local. Quando uma chave de criptografia esta configurada, esses campos sao criptografados em repouso usando **Fernet** (AES-128-CBC com HMAC-SHA256).

### Ordem de resolucao da chave

O proxbox-api resolve a chave de criptografia na seguinte ordem de prioridade:

1. **Variavel de ambiente `PROXBOX_ENCRYPTION_KEY`** — prioridade maxima, aplicada imediatamente no startup.
2. **`ProxboxPluginSettings.encryption_key`** — buscada na API de configuracoes do plugin no NetBox (configuravel na pagina `/plugins/proxbox/settings/`). So e consultada quando a env var nao esta definida.
3. **Arquivo local** — `PROXBOX_ENCRYPTION_KEY_FILE`, ou o padrao `<repo_root>/data/encryption.key`, somente depois que as duas fontes anteriores estiverem vazias.
4. **Nenhuma** — sem chave configurada. Startup e operacoes sem credencial continuam disponiveis, mas writes de credenciais sao recusados, exceto quando `PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS` habilita explicitamente armazenamento plaintext apenas para lab. Um log `CRITICAL` e emitido.

### Definindo a chave

Gere uma chave segura:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Defina via variavel de ambiente:

```bash
export PROXBOX_ENCRYPTION_KEY="<cole a chave aqui>"
```

Ou defina na pagina de configuracoes do plugin no NetBox em **Encryption** → **Encryption key**.

### Compatibilidade retroativa

Se as credenciais ja estavam armazenadas em texto puro antes da criptografia ser ligada, elas continuam funcionando — `decrypt_value` retorna o valor inalterado quando nenhum prefixo `enc:` esta presente. Elas sao recriptografadas na proxima vez que o endpoint for salvo.

Se a chave de criptografia mudar depois das credenciais ja terem sido criptografadas, o proxbox-api emite um warning e retorna o ciphertext bruto (inutilizavel como credencial). Salve cada endpoint novamente com as credenciais corretas apos a rotacao da chave.
