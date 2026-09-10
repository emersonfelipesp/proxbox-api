# Sessões do Console do Proxmox

Este documento é o guia canônico de implementação do `proxbox-api` para o broker privado de sessões usado pelo console do Proxmox no NMS. Ele explica como o serviço seleciona um guest, solicita um ticket de `vncproxy` ou `termproxy`, monta a URL WebSocket upstream, fornece um único valor de autenticação limitado ao relay confiável, preserva a política TLS do endpoint e trata falhas.

Este endpoint é exclusivamente serviço a serviço. A resposta contém credenciais temporárias e nunca pode ser devolvida diretamente ao JavaScript do navegador. O `nms-backend` é o consumidor confiável: ele guarda a resposta em um ticket Redis de uso único no servidor e fornece ao navegador somente um token opaco do stream do NMS.

## Responsabilidade e limite de confiança

```text
navegador do NMS
    |
    | somente o token opaco do stream do NMS
    v
relay confiável do nms-backend
    |
    | POST /proxmox/console/sessions autenticado como serviço
    | endpoint_id + vmid + node + vm_type + console_type
    v
proxbox-api
    |
    | carrega o endpoint e as credenciais configuradas
    | chama vncproxy ou termproxy no Proxmox
    | prepara a autenticação privada do WebSocket
    v
API e endpoint WebSocket do Proxmox
```

O `proxbox-api` não autoriza o usuário final contra uma máquina virtual do NetBox. O `nms-backend` faz essa autorização por objeto, no escopo do chamador, e converte a relação de endpoint do NetBox para o ID do banco de dados do `proxbox-api` antes de chamar esta rota. Este serviço confia que o chamador autenticado já autorizou o `endpoint_id` e então valida que o endpoint existe localmente.

## Modos compatíveis

| Workload | `vm_type` | `console_type` | Operação no Proxmox |
|---|---|---|---|
| VM QEMU/KVM | `qemu` | `novnc` | `nodes(node).qemu(vmid).vncproxy.post(websocket=1)` |
| VM QEMU/KVM | `qemu` | `term` | `nodes(node).qemu(vmid).termproxy.post()` |
| Contêiner LXC | `lxc` | `term` | `nodes(node).lxc(vmid).termproxy.post()` |

`ConsoleSessionRequest` rejeita LXC/noVNC. Valores desconhecidos, IDs não positivos, node vazio e campos JSON extras também falham na validação antes da consulta ao endpoint.

## Contratos de requisição e resposta

`POST /proxmox/console/sessions` recebe:

```json
{
  "endpoint_id": 1,
  "vmid": 544,
  "node": "pve01",
  "vm_type": "qemu",
  "console_type": "novnc"
}
```

`endpoint_id` é a chave primária local de `ProxmoxEndpoint` no banco do `proxbox-api`. Ele não é a chave primária da máquina virtual no NetBox e pode ser diferente da chave do endpoint no `netbox-proxbox`.

A resposta `ConsoleSessionResponse` é material privado de transporte:

| Campo | Significado | Sensibilidade |
|---|---|---|
| `ticket` | Ticket temporário do console do Proxmox | Segredo; somente no servidor |
| `port` | Porta efêmera retornada pelo Proxmox | Metadado privado de roteamento |
| `proxmox_host` | Host do endpoint configurado | Topologia privada |
| `proxmox_port` | Porta HTTPS configurada | Topologia privada |
| `ws_url` | URL upstream `wss://` completa | Contém segredo e ticket |
| `console_type` | `novnc` ou `term` | Valor de roteamento não secreto |
| `verify_ssl` | Política TLS persistida no endpoint | Política controlada pelo servidor |
| `websocket_auth` | Um valor privado `authorization` ou `cookie` | Segredo; somente no servidor |

A rota retorna os dados necessários para um relay confiável abrir o WebSocket do Proxmox. Ela não é um contrato público de sessão para o navegador. O `nms-backend` transforma essa resposta em um contrato público muito menor.

## Mapa do código

| Arquivo ou símbolo | Responsabilidade |
|---|---|
| `ConsoleSessionRequest` | Validação estrita de endpoint, guest e modo, incluindo a rejeição de LXC/noVNC. |
| `ConsoleSessionResponse` | Contrato privado para o relay confiável. |
| `_load_endpoint()` | Carrega o `ProxmoxEndpoint` local exato; IDs ausentes retornam 404. |
| `_connect_endpoint()` | Converte a configuração persistida e cria `ProxmoxSession`; falhas viram respostas 502 sanitizadas. |
| `_request_console_proxy()` | Seleciona node, API QEMU/LXC e `vncproxy`/`termproxy`. |
| `_console_ticket()` | Normaliza formatos de resposta do SDK e extrai ticket e porta válidos. |
| `_console_port()` | Aceita inteiro ou decimal ASCII de 1 a 65535 e rejeita booleanos e outros formatos. |
| `_console_websocket_auth()` | Obtém e converte a autenticação privada do WebSocket da sessão ativa. |
| `_build_ws_url()` | Codifica o ticket e monta a URL exata de `vncwebsocket`. |
| `create_console_session()` | Orquestra todo o fluxo do endpoint até a resposta privada. |
| `ProxmoxSession.get_websocket_auth()` | Seleciona `Authorization` de token da API ou `PVEAuthCookie` de sessão por senha. |

Os símbolos da rota ficam em `proxbox_api/routes/proxmox/console.py`; a autenticação da sessão fica em `proxbox_api/session/proxmox_core.py`.

## Resolução do endpoint e da sessão

`_load_endpoint()` consulta o banco assíncrono pelo `endpoint_id` exato. Um registro desconhecido retorna HTTP 404 sem contato com o Proxmox.

`_connect_endpoint()` passa o registro por `_parse_db_endpoint()` e chama `ProxmoxSession.create()`. A configuração persistida fornece host, porta, credenciais, modo de autenticação e `verify_ssl`. Uma falha registra somente o ID do endpoint e a classe da exceção e retorna HTTP 502 com `Unable to connect to Proxmox endpoint.`.

O navegador nunca pode selecionar ou substituir credenciais e políticas TLS persistidas.

## Aquisição do ticket

`_request_console_proxy()` monta o recurso do Proxmox usando a requisição validada:

```python
guest = px.session.nodes(req.node)
guest = guest.qemu(req.vmid) if req.vm_type == "qemu" else guest.lxc(req.vmid)
```

Em seguida, chama `vncproxy.post(websocket=1)` para noVNC ou `termproxy.post()` para terminal. `resolve_async()` normaliza resultados síncronos e assíncronos do SDK. A rota não inicia, desliga ou altera a configuração do guest; ela solicita somente o proxy efêmero do console.

## Normalização da resposta

Versões do SDK podem retornar dicionário, objeto com `model_dump()`, objeto legado com `dict()` ou payload sob `data`. `_console_ticket()` normaliza esses formatos.

O ticket deve ser uma string não vazia. `_console_port()` aceita somente um inteiro entre 1 e 65535 ou uma string decimal composta apenas por caracteres ASCII no mesmo intervalo. Booleanos são rejeitados explicitamente. Valores ausentes, inválidos ou fora do intervalo retornam o detalhe fixo `Proxmox did not return a ticket/port.` com HTTP 502.

## Autenticação do WebSocket

O upgrade WebSocket exige o ticket na URL e a autenticação da sessão ativa. `ProxmoxSession.get_websocket_auth()` fornece exatamente um valor tipado:

- `kind="authorization"` com a autorização do token da API; ou
- `kind="cookie"` com o `PVEAuthCookie` da sessão por senha.

Os valores de `ProxmoxWebSocketAuth` e `ConsoleWebSocketAuth` usam `repr=False`. A rota fornece a credencial somente ao relay. O `nms-backend` valida novamente o tipo e os limites, guarda o valor no ticket de uso único e o anexa somente ao handshake upstream.

Não adicione um segundo campo de autenticação e não exponha o valor em logs, respostas ao navegador ou URLs.

## Montagem da URL WebSocket

Os consoles noVNC e terminal usam:

```text
wss://{host}:{proxmox_port}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket
    ?port={console_port}&vncticket={ticket-codificado}
```

`_build_ws_url()` usa `quote(ticket, safe="")`. O conjunto seguro vazio é obrigatório porque caracteres comuns do ticket poderiam alterar a interpretação da query string. Host e porta vêm do endpoint persistido; node, tipo e VMID vêm da requisição validada.

O `nms-backend` exige `wss://` antes de guardar a URL. O navegador nunca recebe essa URL.

## Política TLS

`verify_ssl` vem somente do `ProxmoxEndpoint` persistido. O schema de requisição não possui campo de verificação TLS, portanto o chamador não pode reduzir essa proteção por sessão. Prefira certificados verificados; use `verify_ssl=false` somente quando o risco específico do endpoint foi aceito na configuração persistida.

## Falhas, logs e invariantes de segurança

Violações do schema retornam 422; endpoint ausente retorna 404; falhas de conexão, proxy, ticket, porta ou autenticação retornam 502 com detalhes limitados. Um `ProxmoxAPIError` pode ser detalhado para o backend confiável, mas o `nms-backend` o converte antes de qualquer resposta ao navegador.

Preserve estas invariantes:

- mantenha a rota autenticada como serviço e nunca a chame diretamente do navegador;
- mantenha a autorização do usuário por objeto no `nms-backend`;
- trate `endpoint_id` como o ID do banco local do `proxbox-api`;
- mantenha `extra="forbid"` e a matriz QEMU/LXC explícita;
- obtenha host, porta, credenciais e política TLS somente do endpoint persistido;
- codifique todo o ticket com `safe=""`;
- forneça exatamente um tipo/valor de autenticação e mantenha segredos fora de repr e logs;
- não reutilize `ConsoleSessionResponse` como contrato público do navegador; e
- mantenha a sanitização de erros na fronteira do relay.

## Cobertura de regressão

`tests/proxmox/test_console_route.py` cobre autenticação por token e senha, os três modos compatíveis, seleção de `vncproxy`/`termproxy`, lookup e falhas do endpoint, normalização de respostas e portas, codificação da URL, política TLS, erros do Proxmox e rejeição de LXC/noVNC.

Execute:

```bash
uv run pytest -q tests/proxmox/test_console_route.py
```

Ao alterar o broker, mantenha este guia, a referência HTTP, o `README.md` e os arquivos de contexto de LLM sincronizados; confira os contratos com o `nms-backend`; teste token, senha, QEMU noVNC, QEMU terminal e LXC terminal; e confirme que nenhum segredo entrou em schema público, repr, log ou resposta ao navegador.
