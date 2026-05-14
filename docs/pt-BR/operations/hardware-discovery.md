# Descoberta de Hardware

Etapa opcional que enriquece o `dcim.Device` de cada nó Proxmox e os
`dcim.Interface` de suas NICs com informações de chassi e enlace que a API
REST do Proxmox VE não expõe. Os dados são coletados via SSH no nó executando
`dmidecode`, `ip -o link show` e `ethtool`.

A etapa é **desabilitada por padrão**. Com o flag desligado, nenhum socket SSH
é aberto durante a sincronização.

## Arquitetura

```
proxbox-api  (orquestrador — sem import de paramiko)
  └── proxbox_api/services/hardware_discovery.py
        ├── is_enabled()                    checagem do flag de configuração
        ├── fetch_credential(node_id)       HTTPS+Bearer para netbox-proxbox
        └── run_for_nodes(nb, nodes, *, bridge)
              importa proxmox_sdk.ssh.RemoteSSHClient
              importa proxmox_sdk.node.hardware.discover_node
              laço sequencial por nó, exceção → frame SSE de warning

proxmox-sdk (biblioteca — dona de todas as primitivas SSH + parsers)
  ├── proxmox_sdk.ssh.RemoteSSHClient
  └── proxmox_sdk.node.hardware.{dmidecode,ethtool,facts,discover}

netbox-proxbox (plugin NetBox)
  ├── ProxboxPluginSettings.hardware_discovery_enabled
  └── modelo NodeSSHCredential + endpoint REST
        /api/plugins/proxbox/ssh-credentials/by-node/{node_id}/credentials/
```

A invariante "sem `paramiko` em `proxbox_api/`" é fixada pelo teste
`tests/test_hardware_discovery_no_paramiko_import.py`, que percorre a AST do
pacote e falha se encontrar qualquer import de `paramiko`, `asyncssh`,
`fabric`, etc.

## Habilitando a etapa

1. No NetBox → Plugins → Proxbox → Settings, ative
   **Hardware discovery enabled**.
2. Para cada nó, crie uma `NodeSSHCredential` (NetBox → Plugins → Proxbox →
   SSH Credentials). Configure:
   - username
   - chave privada (ed25519 recomendado) ou senha
   - fingerprint SHA-256 da chave do host (não há TOFU; o fingerprint precisa
     ser capturado e fixado previamente)
   - `sudo_required` (padrão `True` para `dmidecode`)
3. No nó Proxmox, provisione um usuário dedicado `proxbox-discovery` com uma
   entrada de sudoers restrita apenas a `/usr/sbin/dmidecode -t 1` e
   `/usr/sbin/dmidecode -t 3`.
4. Dispare uma sincronização. Após cada nó ser sincronizado, a etapa de
   descoberta roda sequencialmente para os nós que possuem IP primário.

Veja `netbox-proxbox/docs/configuration/hardware-discovery.md` para o passo a
passo do operador (geração de chave, fixação de fingerprint na UI,
configuração no nó de `authorized_keys` com `command=`).

## Frames SSE

Em caso de sucesso, o orquestrador emite um frame `hardware_discovery` por nó
via `WebSocketSSEBridge.emit_hardware_discovery_progress()`:

```json
{
  "type": "hardware_discovery",
  "node": "pve01",
  "node_id": 42,
  "chassis_serial": "ABCD1234",
  "chassis_manufacturer": "Dell Inc.",
  "chassis_product": "PowerEdge R740",
  "nic_count": 4
}
```

Em caso de falha, um frame `item_progress` genérico com campo `warning` é
emitido. Códigos:

| Warning | Causa |
|---|---|
| `hardware_discovery_no_primary_ip` | Nó sem `primary_ip4`/`primary_ip` no NetBox. |
| `hardware_discovery_no_credential` | Nenhuma `NodeSSHCredential` para o `node_id`. |
| `hardware_discovery_timeout` | Timeout na conexão ou execução SSH. |
| `hardware_discovery_auth_failed` | Falha de autenticação SSH. |
| `host_key_mismatch` | Host-key do nó não bate com o fingerprint fixado. Credenciais NÃO são enviadas. |
| `hardware_discovery_failed: <exc>` | Catch-all para qualquer outra exceção. |

## Custom fields no NetBox

Seis custom fields são criados pelo bootstrap em
`proxbox_api/routes/extras/__init__.py::create_custom_fields()` no grupo já
existente **Proxmox**:

| Campo | Objeto | Tipo |
|---|---|---|
| `hardware_chassis_serial` | `dcim.device` | text |
| `hardware_chassis_manufacturer` | `dcim.device` | text |
| `hardware_chassis_product` | `dcim.device` | text |
| `nic_speed_gbps` | `dcim.interface` | integer |
| `nic_duplex` | `dcim.interface` | text |
| `nic_link` | `dcim.interface` | boolean |

As escritas passam pelo PATCH com drift-detect existente
(`netbox_rest.rest_patch_async`), então um segundo sync consecutivo bem-sucedido
não gera nenhum `extras.ObjectChange` para esses campos.

## Fronteira de segurança

- Credenciais ficam criptografadas (Fernet) no netbox-proxbox; texto puro
  existe apenas na memória do orquestrador pela duração de uma única sessão
  SSH.
- O endpoint REST de credenciais exige Bearer token que case com
  `FastAPIEndpoint.token`.
- O `RemoteSSHClient` do proxmox-sdk garante:
  - fixação de fingerprint SHA-256 (recusa conexão em mismatch — sem TOFU)
  - `run()` aceita apenas `list[str]` (sem interpolação de shell)
  - allowlist de comandos (`["dmidecode", "ip", "ethtool"]` é imposto pelo
    orquestrador)
  - cap de saída, timeouts de conexão/exec
  - regexes de redator de logs para manter material de chave fora do
    `caplog`/`logger`
- O orquestrador roda os nós sequencialmente, por cluster, para que um nó
  travado não impeça os demais.

## Cobertura de testes

| Teste | Garante |
|---|---|
| `tests/test_hardware_discovery_no_paramiko_import.py` | Guard estático via AST: nenhum import de biblioteca SSH em `proxbox_api/`. |
| `tests/test_hardware_discovery_flag_off.py` | Flag desligado → zero construções de `RemoteSSHClient`. |
| `tests/test_hardware_discovery_orchestrator.py` | Despacho sequencial, formato do frame de sucesso, mapeamento de cada classe de falha para o código de warning correto. |
| `tests/test_hardware_discovery_credential_fetch.py` | Formato da URL HTTPS+Bearer, 404 → `MissingCredential`, 5xx/JSON inválido/corpo não-dict → `HardwareDiscoveryError`, nenhum vazamento de segredo em DEBUG. |
