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

## Ciclo de vida do cliente NetBox

`proxbox_api/session/netbox.py` e o proprietario local ao processo dos clientes
`netbox-sdk`. Ele mantem no maximo um fingerprint de cliente atual por id de
`NetBoxEndpoint`. Uma alteracao de URL, token, timeout ou TLS substitui a entrada
de forma atomica; o transporte aposentado e destacado dentro do lock e fechado
de forma assincrona depois que o lock e liberado.

As rotas de update e delete de endpoint NetBox aguardam a invalidacao direcionada
antes de retornar; o update republica um cliente padrao novo para consumidores
raw e WebSocket. Invalidacoes repetidas sao seguras e a invalidacao de um
endpoint nao fecha o cliente de outro. Durante o shutdown terminal, a lifespan
recusa novas aquisicoes e fecha clientes ativos ou ja em processo de fechamento
em um bloco `finally`, inclusive quando o startup, uma requisicao ou o cancelamento
falha. Falhas de fechamento registram somente o id do endpoint
e o tipo da excecao, sem credenciais, URLs ou fingerprints de configuracao.

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

1. `create_app()` inicializa os metadados do banco necessarios para compor a app.
2. A app monta static assets, CORS, handlers de excecao, rotas de cache, full-update e WebSocket.
3. Os routers sao incluidos para NetBox, Proxmox, DCIM, virtualization, extras e sync individual.
4. O startup da lifespan adquire o cliente NetBox padrao, gerenciado pelo ciclo de vida, e monta as rotas Proxmox geradas; elas podem falhar em modo open, a menos que `PROXBOX_STRICT_STARTUP` esteja habilitado.
5. O OpenAPI customizado embute o contrato Proxmox gerado quando ele existe.
6. O shutdown da lifespan recusa novos clientes e drena os clientes NetBox em cache ou ja aposentados dentro de um bloco `finally`; um transporte que trava durante o fechamento assincrono e abandonado apos o limite de 10 segundos, com aviso sem segredos.

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
