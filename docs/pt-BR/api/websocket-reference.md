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

- Aceita conexao e escuta um frame de texto legado ou um envelope JSON de
  comando correlacionado por execucao.
- Comandos suportados:
  - `Full Update Sync`
  - `Full Update`
  - `Sync Nodes`
  - `Sync Virtual Machines`
- Executa as tarefas de sync correspondentes e envia mensagens de status.

Clientes legados enviam diretamente a string do comando. Esse comportamento e
todos os frames de resposta legados permanecem inalterados. Clientes que
descobrem `capabilities.ws_run_id >= 1` no `GET /version` autenticado podem
enviar este envelope fechado:

```json
{"command": "Full Update", "run_id": "sync_run_20261014"}
```

O `run_id` deve corresponder a `^[A-Za-z0-9_-]{8,64}$`. Os dois campos sao
obrigatorios, e campos desconhecidos sao rejeitados sem iniciar a sincronizacao.
Um envelope e limitado a 4096 bytes UTF-8; JSON grande demais, malformado ou
excessivamente aninhado recebe a orientacao normal de comando invalido, e a
conexao continua utilizavel. Cada frame de progresso, terminal e erro produzido
para um envelope aceito inclui o mesmo `run_id`. Objetos JSON existentes recebem
o campo diretamente. Uma mensagem de texto do fluxo do comando e representada
como `{"message": "...", "run_id": "..."}` em uma execucao correlacionada.

Depois das duas etapas de um full update correlacionado, o servidor emite
exatamente um terminal agregado apos a ultima etapa:

```json
{
  "object": "full-update",
  "end": true,
  "run_id": "sync_run_20261014",
  "status": "completed",
  "stages": {
    "devices": "completed",
    "virtual-machines": "completed"
  }
}
```

Os valores das etapas sao `completed`, `skipped` ou `failed`. Quando a
sincronizacao de devices nao retorna nodes sincronizados, `virtual-machines`
fica como `skipped`. Uma falha de etapa produz um frame `error_detail`
correlacionado e um agregado final com `status: "failed"`. Contagens de falha
diferentes de zero retornadas depois de reconciliacao parcial de devices ou de
interfaces obrigatorias de VM contam como falhas de etapa. Um comando
correlacionado individual tambem emite `error_detail` para esse resultado, mas
nao emite um agregado. Comandos enviados como string nunca recebem o frame
agregado.

A resposta autenticada de versao anuncia o suporte de forma aditiva:

```json
{"version": "<backend-version>", "capabilities": {"ws_run_id": 1}}
```

Comando invalido:

- Retorna orientacao com lista de comandos validos.

## Notas

- Fluxos WebSocket dependem de endpoint NetBox valido e sessoes Proxmox disponiveis.
- Operacoes longas criam journal entries em objetos do plugin NetBox para auditabilidade.
- Os payloads de progresso sao normalizados pelo mesmo bridge usado pelo SSE, produzindo frames `step`, `error` e `complete`.
