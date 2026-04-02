# Visao Geral da Arquitetura

`proxbox-api` e organizado em camadas de rotas FastAPI, dependencias de sessao, servicos de sync e camadas de schema.

## Camadas de alto nivel

- Camada de API: `proxbox_api/main.py`, `proxbox_api/routes/*`
- Camada de sessao: `proxbox_api/session/*`
- Camada de servicos: `proxbox_api/services/sync/*`
- Camada de schemas e enums: `proxbox_api/schemas/*`, `proxbox_api/enum/*`
- Camada de persistencia: `proxbox_api/database.py`
- Camada utilitaria: decorators, logger, cache e excecoes

## Componentes de runtime

- App FastAPI com grupos de rotas:
  - `/netbox`
  - `/proxmox`
  - `/dcim`
  - `/virtualization`
  - `/extras`
- Configuracao de endpoints persistida em SQLite.
- Acesso a NetBox via `netbox-sdk` sync proxy.
- Acesso a Proxmox via sessoes `proxmoxer`.

## Modelos de dados principais

### `NetBoxEndpoint`

- Campos: `name`, `ip_address`, `domain`, `port`, `token`, `verify_ssl`
- Inclui propriedade computada `url` para criar sessao NetBox.
- Comportamento singleton e aplicado na logica do endpoint de criacao.

### `ProxmoxEndpoint`

- Campos: `name`, `ip_address`, `domain`, `port`, `username`, `password`, `verify_ssl`, `token_name`, `token_value`
- Suporta autenticacao por senha ou por token.

## Fluxo de startup

1. A app inicializa e tenta criar tabelas do banco.
2. Carrega endpoint NetBox, se existir.
3. Inicializa sessao NetBox e salva na camada de compatibilidade.
4. Monta origins de CORS.
5. Inclui routers.

## Extensao de OpenAPI

`proxbox_api/openapi_custom.py` substitui a geracao de OpenAPI do FastAPI e embute metadados do OpenAPI Proxmox gerado quando disponivel:

- Arquivos-fontes: `proxbox_api/generated/proxmox/<version-tag>/openapi.json` (artefatos versionados; `latest` e a tag de versao padrao).
- Campos de extensao:
  - `info.x-proxmox-generated-openapi`
  - `x-proxmox-generated-openapi`

## Ciclo de sync

- Endpoints de sync orquestram descoberta no Proxmox e criacao de objetos no NetBox.
- Journal entries fornecem rastreabilidade.
- Endpoints WebSocket e SSE fornecem progresso em tempo real por objeto.
