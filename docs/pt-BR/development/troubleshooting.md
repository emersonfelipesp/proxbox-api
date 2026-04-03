# Solucao de problemas

## Falta endpoint NetBox

Sintoma:

- Rotas que precisam de sessao NetBox falham com mensagem indicando que nao existe endpoint configurado.

Resolucao:

1. Crie o endpoint com `POST /netbox/endpoint`.
2. Valide com `GET /netbox/endpoint`.
3. Se o bootstrap foi pulado de forma intencional, confirme que `PROXBOX_SKIP_NETBOX_BOOTSTRAP` nao esta definido.

## Erros de autenticacao Proxmox

Sintoma:

- `400` com `Provide password or both token_name/token_value`.
- `400` com `token_name and token_value must be provided together`.

Resolucao:

- Forneca `password` ou o par completo `token_name` + `token_value`.
- Para o endpoint NetBox, lembre que token v1 usa apenas `token`, enquanto token v2 exige `token_key` e `token`.

## Falha no mount de rotas Proxmox geradas

Sintoma:

- Os logs de startup informam que as rotas Proxmox geradas nao puderam ser montadas.

Resolucao:

- Confirme que o snapshot gerado existe em `proxbox_api/generated/proxmox/latest/openapi.json`.
- Recrie o contrato Proxmox em tempo de execucao com `POST /proxmox/viewer/generate` se o snapshot estiver ausente.
- Se a aplicacao precisar falhar fechada, habilite `PROXBOX_STRICT_STARTUP`.

## Problemas de CORS

Sintoma:

- O browser bloqueia requests por politica de CORS.

Resolucao:

- Verifique o dominio do endpoint NetBox.
- Confirme que a origem do frontend esta na allowlist de CORS.
- Confirme que os requests usam o host e a porta esperados.

## Falhas de conexao com Proxmox

Sintoma:

- Excecoes de conexao durante a criacao da sessao.

Resolucao:

- Valide host, porta e credenciais do Proxmox.
- Confira o comportamento de `verify_ssl` e certificados.
- Confirme as permissoes do usuario ou token no Proxmox.
- Em deploys com multiplos endpoints, confirme os valores de target e `source` passados na requisicao.

## Endpoints de sync retornam dados parciais

Sintoma:

- Alguns objetos sincronizam enquanto outros falham.

Resolucao:

- Inspecione os logs da API com `GET /admin/logs`.
- Valide se os objetos obrigatorios do NetBox e os modelos do plugin existem.
- Refaca o sync usando o modo WebSocket para visibilidade ao vivo.

## Estado do banco

Nota:

- O startup atual cria tabelas ausentes sem apagar dados existentes.

Se necessario:

- Faca backup de `database.db` antes de experimentos com schema.

## Problemas com SSE

### Stream vazio ou travado

Sintoma:

- O endpoint SSE `/stream` conecta, mas nenhum evento chega.

Resolucao:

- Verifique se os endpoints NetBox e Proxmox estao configurados (`GET /netbox/endpoint`, `GET /proxmox/endpoints`).
- Confira os logs da API para excecoes durante o sync.
- Confirme que o cliente HTTP nao esta fazendo buffering (use o header `Accept: text/event-stream`).
- Garanta que `Cache-Control: no-cache` seja respeitado por proxies intermediarios.

### Timeout do stream

Sintoma:

- O stream SSE cai antes do sync terminar.

Resolucao:

- Aumente o timeout do cliente ou use um cliente ciente de streaming.
- Para inventarios grandes, considere reduzir `PROXBOX_VM_SYNC_MAX_CONCURRENCY` e `PROXBOX_NETBOX_WRITE_CONCURRENCY` para diminuir a pressao sobre o NetBox.
- Confira se `PROXBOX_NETBOX_TIMEOUT` e suficiente para a latencia do seu servidor NetBox.

### Resposta SSE com erro de hop-by-hop header

Sintoma:

- HTTP 500 com `AssertionError: Hop-by-hop header not allowed` em ambientes com proxy Django.

Resolucao:

- As respostas SSE nao devem incluir `Connection: keep-alive` quando servidas por middleware WSGI.
- Use apenas `Cache-Control: no-cache` e `X-Accel-Buffering: no` nos stream responses.

### Stream retorna event error em vez de complete

Sintoma:

- O sync termina com `event: error` seguido de `event: complete` com `ok: false`.

Resolucao:

- Verifique os campos `error` e `detail` no payload do evento de erro.
- Causas comuns: NetBox indisponivel, falha de autenticacao no Proxmox, modelos obrigatorios do plugin NetBox ausentes.
- Refaca com o modo WebSocket (`/ws`) para logging mais verboso, se necessario.
