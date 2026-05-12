# Resolvedor de Colisões de Nome de VM

O modelo `virtualization.VirtualMachine` do NetBox impõe unicidade em
`(cluster, tenant, name)` com `nulls_distinct=False`. Quando o proxbox-api
sincroniza duas VMs Proxmox que compartilham o mesmo `name` e caem no mesmo
cluster NetBox, o NetBox rejeita o segundo `POST` com erro `400`.

O resolvedor atribui sufixos determinísticos ``" (N)"`` para que ambos os
registros coexistam e detecta renomeações manuais para que um registro
NetBox editado pelo operador não seja sobrescrito silenciosamente na
próxima sincronização.

## Localização

`proxbox_api/services/name_collision.py`

- `NameResolution` — dataclass congelada retornada pelo resolvedor.
- `_pick_suffix(candidate, used)` — função pura que escolhe o menor sufixo
  livre ``" (N)"`` (ou retorna o nome puro quando não há colisão).
- `resolve_unique_vm_name(...)` — entrada assíncrona usada tanto pelo
  caminho de sincronização em lote (`sync_vm.py`) quanto pelo caminho
  individual (`services/sync/individual/vm_sync.py`).

A correspondência de nomes é insensível a maiúsculas/minúsculas via
`str.casefold`. O `resolved_name` retornado preserva a caixa do candidato.

## Quando o resolvedor executa

### Sincronização em lote (`/full-update`)

Em `_run_full_update_vm_batch`, imediatamente após o carregamento do
snapshot NetBox e antes da construção da fila de operações. O pré-passo:

1. Agrupa as VMs preparadas por id de cluster NetBox.
2. Dentro de cada cluster, ordena as VMs por
   `(proxmox_cluster_name.casefold(), proxmox_vmid)` para que a VM de menor
   VMID mantenha o nome puro entre re-execuções.
3. Constrói `used_names_in_cluster` a partir do snapshot, removendo o
   registro atualmente associado a cada VMID (para que a mesma VM possa
   manter seu nome na re-sincronização sem colidir consigo mesma).
4. Chama `resolve_unique_vm_name(...)` por VM e altera
   `prepared.desired_payload["name"]` quando um sufixo ou renomeação de
   operador é aplicado.
5. Emite `WebSocketSSEBridge.emit_duplicate_name_resolved(...)` para cada
   VM renomeada.

### Sincronização individual (`/sync/virtual-machines/{vmid}`)

`sync_vm_individual` emite um único
`GET /api/virtualization/virtual-machines/?cluster_id=<id>&limit=0`,
constrói o conjunto `used_names_in_cluster` e o mapa
`existing_vm_by_vmid`, chama `resolve_unique_vm_name(...)` e ajusta o
campo `name` do payload antes da reconciliação.

## Detecção de renomeação manual

Se o NetBox tem uma `VirtualMachine` com
`custom_fields.proxmox_vm_id` igual ao VMID em sincronização **e** cujo
`name` atual não é o candidato puro nem um sufixo algorítmico
(`gateway (2)`, `gateway (3)`…), o resolvedor retorna o nome do operador
com `operator_renamed=True`. O chamador emite o frame
`duplicate_name_resolved` com `operator_renamed: true` e não renomeia.

Nomes de operador que parecem algorítmicos (por exemplo, `gateway (2)`
digitado manualmente) não podem ser distinguidos da saída do resolvedor e
serão tratados como nomes atribuídos pelo resolvedor em sincronizações
subsequentes.

## Limites

- **Por cluster NetBox.** Duas VMs em clusters NetBox distintos não
  colidem estruturalmente e ficam com o nome puro — mesmo que os rótulos
  de cluster Proxmox sejam diferentes. Isso espelha a unicidade
  estrutural do NetBox.
- **Identificador estável.** `custom_fields.proxmox_vm_id` é o vínculo
  cruzado durável. O resolvedor pode inverter qual VM mantém o nome puro
  se os nomes de cluster Proxmox forem reordenados, mas o vínculo por
  VMID permanece.
- **Registros legados.** Uma VM NetBox sem o campo customizado
  `proxmox_vm_id` não pode ser correlacionada por VMID; o resolvedor a
  trata como um registro não-Proxbox e não a considerará uma renomeação
  manual.

## Forma do frame SSE

```json
{
  "event": "duplicate_name_resolved",
  "cluster": "cluster-a",
  "original_name": "gateway",
  "resolved_name": "gateway (2)",
  "vmid": 101,
  "suffix_index": 2,
  "operator_renamed": false
}
```

Autoridade do schema:

- `proxbox_api.schemas.stream_messages.DuplicateNameResolvedMessage`
- builder: `build_duplicate_name_resolved_message`
- emissor: `WebSocketSSEBridge.emit_duplicate_name_resolved`

O espelho do lado do netbox-proxbox é
`netbox_proxbox.schemas.backend_proxy.SseDuplicateNameResolvedPayload`, e
`contracts/proxbox_api_sse_schema.json` é fixado por
`tests/test_sse_schema_mirror.py`.
