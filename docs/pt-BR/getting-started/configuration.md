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

## Comportamento de sessoes em runtime

- A sessao NetBox e derivada do endpoint NetBox armazenado.
- O valor `verify_ssl` do endpoint NetBox tambem e usado nas buscas de plugin settings, entao certificados self-signed funcionam de forma consistente quando a verificacao esta desabilitada.
- As sessoes Proxmox usam por padrao registros de endpoint do banco local.
- O modo legado (`source=netbox`) continua suportado na dependencia de sessoes Proxmox.

## Variaveis de ambiente

| Variavel | Padrao | Descricao |
|----------|--------|-----------|
| `PROXBOX_NETBOX_TIMEOUT` | `120` | Timeout da API NetBox em segundos. Aplicado ao `netbox-sdk` e as requisicoes internas. |
| `PROXBOX_NETBOX_MAX_RETRIES` | `5` | Numero de tentativas para falhas transientes do NetBox. |
| `PROXBOX_NETBOX_RETRY_DELAY` | `2.0` | Delay inicial, em segundos, para retries do NetBox. |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `1` | Maximo de requisicoes simultaneas ao NetBox. Mantenha baixo (1-2) para evitar agotar o pool de conexoes PostgreSQL do NetBox. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `4` | Maximo de tarefas concorrentes de escrita no sync de VMs. |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `8` (sync de VM) / `4` (task-history, snapshots) | Maximo de operacoes concorrentes de escrita no NetBox. O padrao varia por servico de sync. |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `8` (maioria dos fluxos) / `4` (task-history) | Maximo de operacoes concorrentes de leitura no Proxmox. O padrao varia por servico de sync. |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `8` | Override legado de concorrencia usado por alguns entrypoints de sync. |
| `PROXBOX_RATE_LIMIT` | `60` | Maximo de requisicoes por minuto por endereco IP. |
| `PROXBOX_BACKUP_BATCH_SIZE` | `5` | Tamanho do lote de sync de backups. Reduza para diminuir a pressao de escrita no NetBox. |
| `PROXBOX_BACKUP_BATCH_DELAY_MS` | `200` | Delay em milissegundos entre lotes de backup. |
| `PROXBOX_CORS_EXTRA_ORIGINS` | (vazio) | Lista de origens CORS extras, separadas por virgula. |
| `PROXBOX_EXPOSE_INTERNAL_ERRORS` | nao definido | Quando `1`, `true` ou `yes`, respostas HTTP 500 incluem detalhes internos da excecao. |
| `PROXBOX_STRICT_STARTUP` | nao definido | Quando `1`, `true` ou `yes`, falha no mount de rotas Proxmox geradas interrompe o startup. |
| `PROXBOX_SKIP_NETBOX_BOOTSTRAP` | nao definido | Quando `1`, `true` ou `yes`, nao cria o cliente NetBox padrao no startup. |

### Tratando erros de NetBox sobrecarregado

Quando o pool de conexoes PostgreSQL do NetBox esta saturado, o proxbox-api retorna erros `netbox_overwhelmed`. Para mitigar:

1. **Reduza a concorrencia**: Defina `PROXBOX_NETBOX_MAX_CONCURRENT=1` para serializar requisicoes
2. **Aumente os retries**: Mais tentativas com delays maiores dao tempo ao NetBox para recuperar
3. **Estenda o TTL do cache**: Use `PROXBOX_NETBOX_GET_CACHE_TTL=300` para reduzir fetches redundantes

A logica de retry aplica backoff agressivo (ate 30 segundos) quando erros de sobrecarga sao detectados.

## Comportamento de CORS

- Origens sao montadas a partir de endpoints NetBox mais origens de desenvolvimento padrao.
- Metodos sao liberados para todos (`allow_methods=["*"]`).
