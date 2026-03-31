# Configuracao

`proxbox-api` usa SQLite para configuracao local de bootstrap e dependencias em runtime.

## Localizacao do banco

- Arquivo SQLite padrao: `database.db` na raiz do repositorio.
- ORM: SQLModel.
- Tabelas sao criadas automaticamente no startup.

## Endpoint NetBox (singleton)

A configuracao de endpoint NetBox e gerenciada por:

- `POST /netbox/endpoint`
- `GET /netbox/endpoint`
- `PUT /netbox/endpoint/{netbox_id}`
- `DELETE /netbox/endpoint/{netbox_id}`

Apenas um registro de endpoint NetBox e permitido.

### Exemplo de payload

```json
{
  "name": "netbox-primary",
  "ip_address": "10.0.0.20",
  "domain": "netbox.local",
  "port": 443,
  "token": "<NETBOX_API_TOKEN>",
  "verify_ssl": true
}
```

## Endpoints Proxmox (multiplos)

Os registros de endpoint Proxmox sao gerenciados por:

- `POST /proxmox/endpoints`
- `GET /proxmox/endpoints`
- `GET /proxmox/endpoints/{endpoint_id}`
- `PUT /proxmox/endpoints/{endpoint_id}`
- `DELETE /proxmox/endpoints/{endpoint_id}`

Regras de autenticacao para create/update:

- Informe `password`, ou ambos `token_name` e `token_value`.
- `token_name` e `token_value` devem ser enviados juntos.

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

- A sessao NetBox e derivada do endpoint NetBox armazenado (singleton).
- Sessoes Proxmox usam por padrao registros de endpoint do banco local.
- O modo legado (`source=netbox`) segue suportado na dependencia de sessoes Proxmox.

## Variaveis de ambiente

| Variavel | Padrao | Descricao |
|----------|--------|-----------|
| `PROXBOX_NETBOX_TIMEOUT` | `120` | Timeout da API NetBox em segundos. Aplicado na config `netbox-sdk` e requisicoes `aiohttp`. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `4` | Numero maximo de tarefas concorrentes na criacao de VMs durante a sincronizacao. |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `8` | Numero maximo de operacoes concorrentes de coleta usadas em sync de backup, snapshot e storage. |
| `PROXBOX_CORS_EXTRA_ORIGINS` | (vazio) | Lista de origens CORS extras, separadas por virgula. |
| `PROXBOX_EXPOSE_INTERNAL_ERRORS` | nao definido | Quando `1`, `true` ou `yes`, respostas HTTP 500 incluem detalhes internos da excecao. |
| `PROXBOX_STRICT_STARTUP` | nao definido | Quando `1`, `true` ou `yes`, falha no mount de rotas Proxmox geradas interrompe o startup da API. |
| `PROXBOX_SKIP_NETBOX_BOOTSTRAP` | nao definido | Quando `1`, `true` ou `yes`, nao cria cliente NetBox padrao no startup (util para desenvolvimento parcial). |

## Comportamento de CORS

- Origens sao montadas a partir de endpoints NetBox mais origens de desenvolvimento padrao.
- Metodos sao liberados para todos (`allow_methods=["*"]`).
