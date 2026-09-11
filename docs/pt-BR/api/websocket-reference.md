# Referencia WebSocket da API


O comportamento de sincronização e SSH abaixo exige
`PROXBOX_EXECUTION_MODE=legacy` explícito. O padrão `rpc_only` encerra `/ws`,
`/ws/virtual-machines` e `/ssh/sessions/{session_id}/ws` com código 1008 antes
de efeitos, mesmo com ticket antigo válido. A sincronização legada exige um
frame JSON com a chave de API em dez segundos, antes de tokens NetBox,
conexões Proxmox, coletores ou gravação de tags. O SSH legado autentica o
ticket de uso único antes de obter configurações de credenciais armazenadas.
O contador mantém sua autenticação e seu comportamento independentes. Consulte
[Limite interativo exclusivo de RPC](../operations/interactive-rpc-boundary.md)
para encerramento e distinção entre prontidão local e da frota.

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
