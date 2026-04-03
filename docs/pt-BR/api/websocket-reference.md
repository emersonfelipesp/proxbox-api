# Referencia WebSocket da API

`proxbox-api` expoe endpoints WebSocket para streaming de progresso de sync e feedback de execucao de comandos.

## `GET /` (WebSocket)

Endpoint:

- `ws://<host>:<port>/`

Comportamento:

- Aceita conexao.
- Envia um contador incremental de mensagens a cada 2 segundos.

Uso:

- Verificacao basica de conectividade.

## `GET /ws/virtual-machines` (WebSocket)

Endpoint:

- `ws://<host>:<port>/ws/virtual-machines`

Comportamento:

- Aceita conexao e envia texto de boas-vindas.
- Dispara o fluxo de sincronizacao de VMs (`create_virtual_machines`).
- Emite eventos JSON de progresso enquanto o sync de VMs executa, quando o modo websocket esta ativo no fluxo.

Uso:

- Monitorar ciclo de sync de VMs em tempo quase real.

## `GET /ws` (WebSocket)

Endpoint:

- `ws://<host>:<port>/ws`

Comportamento:

- Aceita conexao e escuta comandos em texto.
- Comandos suportados:
  - `Full Update Sync`
  - `Sync Nodes`
  - `Sync Virtual Machines`
- Executa as tarefas de sync correspondentes e envia mensagens de status.

Comando invalido:

- Retorna orientacao com lista de comandos validos.

## Notas

- Fluxos WebSocket dependem de endpoint NetBox valido e sessoes Proxmox disponiveis.
- Operacoes longas criam journal entries em objetos do plugin NetBox para auditabilidade.
- Os payloads de progresso sao normalizados pelo mesmo bridge usado pelo SSE, produzindo frames `step`, `error` e `complete`.
