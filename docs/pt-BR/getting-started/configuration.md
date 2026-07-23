# Configuracao

`proxbox-api` usa SQLite para configuracao local de bootstrap e dependencias em runtime.

## Localizacao do banco

- Arquivo SQLite padrao: `database.db` na raiz do repositorio.
- ORM: SQLModel.
- As tabelas sao criadas automaticamente no startup.

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

Algumas variaveis permanecem somente em nivel de processo porque sao lidas antes da conexao com o NetBox existir ou sao infraestrutura exclusiva do operador: `PROXBOX_BIND_HOST`, `PROXBOX_RATE_LIMIT`, `PROXBOX_ENCRYPTION_KEY` / `PROXBOX_ENCRYPTION_KEY_FILE`, `PROXBOX_STRICT_STARTUP`, `PROXBOX_SKIP_NETBOX_BOOTSTRAP`, `PROXBOX_GENERATED_DIR` e `PROXBOX_CORS_EXTRA_ORIGINS`. As demais mapeiam 1:1 para campos de `ProxboxPluginSettings` e podem ser editadas pela pagina de configuracoes do plugin no NetBox.

## Variaveis de ambiente

| Variavel | Padrao | Descricao |
|----------|--------|-----------|
| `PROXBOX_NETBOX_TIMEOUT` | `120` | Timeout da API NetBox em segundos. Aplicado ao `netbox-sdk` e as requisicoes internas. |
| `PROXBOX_NETBOX_MAX_RETRIES` | `5` | Numero de tentativas para falhas transientes do NetBox. |
| `PROXBOX_NETBOX_RETRY_DELAY` | `2.0` | Delay inicial, em segundos, para retries do NetBox. |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `1` | Maximo de requisicoes simultaneas ao NetBox. Mantenha baixo (1-2) para evitar agotar o pool de conexoes PostgreSQL do NetBox. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `4` | Maximo de fetches concorrentes de configuracao de VM Proxmox durante o sync de VMs e discos. |
| `PROXBOX_GUEST_AGENT_TIMEOUT` | `15` | Timeout por chamada (segundos, intervalo 1-600) para a requisicao `network-get-interfaces` do guest-agent QEMU. Guests com muitas interfaces (VRRP/alias) podem demorar a enumerar; aumente este valor se as buscas de interface via guest-agent expirarem. Mapeia para o campo `ProxboxPluginSettings.guest_agent_timeout`. |
| `PROXBOX_RECONCILIATION_ENGINE` | `python` | Override opcional para `ProxboxPluginSettings.reconciliation_engine`. Valores validos: `python`, `compare` e `rust`. |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `8` (sync de VM, discos) / `4` (task-history, snapshots) | Maximo de operacoes concorrentes de escrita no NetBox. O padrao varia por servico de sync. |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `8` (maioria dos fluxos) / `4` (task-history) | Maximo de operacoes concorrentes de leitura no Proxmox. O padrao varia por servico de sync. |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `8` | Override legado de concorrencia usado por alguns entrypoints de sync. |
| `PROXBOX_RATE_LIMIT` | `60` | Maximo de requisicoes por minuto por endereco IP. |
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
