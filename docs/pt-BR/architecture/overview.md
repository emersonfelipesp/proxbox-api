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
  - `/netbox`
  - `/proxmox`
  - `/dcim`
  - `/virtualization`
  - `/extras`
  - `/sync/individual`
- Configuracao de endpoints persistida em SQLite.
- Acesso ao NetBox via clientes `netbox-sdk` sync e async.
- Acesso ao Proxmox via sessoes do SDK sync `proxmox-openapi` e wrappers tipados.
- Rotas Proxmox geradas em runtime sao montadas durante o startup da aplicacao.

## Modelos de dados principais

### `NetBoxEndpoint`

- Campos: `name`, `ip_address`, `domain`, `port`, `token_version`, `token_key`, `token`, `verify_ssl`
- Suporta token NetBox v1 e v2.
- Inclui propriedade computada `url` para criar sessao NetBox.
- O comportamento singleton e aplicado na logica do endpoint de criacao.

### `ProxmoxEndpoint`

- Campos: `name`, `ip_address`, `domain`, `port`, `username`, `password`, `verify_ssl`, `token_name`, `token_value`
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
