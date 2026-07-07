# Provisionamento Firecracker via Host-Agent

`proxbox-api` define o contrato HTTP entre o NMS Cloud e um host-agent Firecracker. O inventario do NetBox continua no `netbox-proxbox`: pools, hosts, imagens e registros `FirecrackerMicroVM` sao resolvidos antes desta API ser chamada.

## Fluxo

```mermaid
sequenceDiagram
    participant NMSB as nms-backend
    participant API as proxbox-api
    participant Agent as Firecracker host-agent

    NMSB->>API: POST /cloud/firecracker/provision[/stream]
    API->>Agent: GET /health
    API->>Agent: GET /capabilities
    API->>Agent: POST /assets/prepare
    API->>Agent: POST /microvms
    API->>Agent: POST /microvms/{microvm_id}/actions/start
    API-->>NMSB: status, microvm_id, instance_ref, guest_ip
```

O endpoint sem stream retorna JSON final. O endpoint com stream envia Server-Sent Events e termina com `event: complete`.

## Endpoints

| Metodo | Caminho | Finalidade |
|---|---|---|
| `POST` | `/cloud/firecracker/provision` | Provisiona uma micro-VM em um host-agent e retorna JSON final |
| `POST` | `/cloud/firecracker/provision/stream` | Provisiona uma micro-VM e envia progresso por SSE |

Ambos usam o middleware normal `X-Proxbox-API-Key`. `X-Proxbox-Actor` e opcional e entra nos metadados enviados ao host-agent.

## Limite de confianca

`nms-backend` deve resolver no NetBox o host Firecracker, pool, imagem e o
registro `FirecrackerMicroVM` antes de chamar o proxbox-api. A requisicao ainda
leva `host_agent_base_url` e `host_agent_token` opcional, entao o proxbox-api
valida a URL antes de qualquer chamada externa: apenas `http` e `https` sao
aceitos, a URL precisa ter host, credenciais/query/fragmento sao recusados e o
host precisa passar pelo guard SSRF compartilhado. O token so e encaminhado
para esse host-agent validado.

A rota SSE retorna `An unexpected error occurred.` por padrao em falhas. Use
`PROXBOX_EXPOSE_INTERNAL_ERRORS=true` apenas em depuracao confiavel, quando
detalhes do host-agent puderem ser expostos ao cliente.

## Eventos SSE

| Evento | Payload |
|---|---|
| `provision_step` | `{step, label, status}` para `host_agent_health`, `capabilities`, `prepare_assets`, `create_microvm` e `start_microvm` |
| `terminal_line` | Linha de progresso legivel, hoje usada para caminhos de assets |
| `complete` | `FirecrackerProvisionResponse` final em sucesso ou `{ok:false,error}` em falha |

## Resposta

Respostas de sucesso incluem `ok`, `microvm_id`, `instance_ref`, `host_id`,
`host_pool_id`, `image_id`, `status`, `guest_ip` e `detail`. Quando
`netbox_microvm_id` e informado, `instance_ref` usa o formato
`firecracker:<id>` e e o identificador usado pelo NMS Cloud.
