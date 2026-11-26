# proxbox-api 0.0.33

## Resumo

Esta versão estabelece identidade estável de guests pelos UIDs do Proxmox VE 9.3, corrige a
sincronização de rede de nós em ambientes com vários nós e sincroniza endereços DHCP em tempo de
execução de containers LXC. Ela também atualiza o SDK Proxmox certificado para
`proxmox-sdk 0.0.16.post1`, que adiciona suporte ao schema do Proxmox VE 9.3.

Esta versão não adiciona migração de banco de dados do backend. A versão pareada
`netbox-proxbox 0.0.33` adiciona os campos `proxmox_vm_uid` e `proxmox_cluster_uid` na migração
`0109`. Implante primeiro este backend; versões anteriores do plugin continuam funcionando por
detecção de recursos.

## Identidade da versão e compatibilidade

| Componente | Versão ou contrato |
| --- | --- |
| proxbox-api | `0.0.33rc1` |
| Proxmox SDK | `proxmox-sdk 0.0.16.post1` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 e 3.13 |
| Migração do banco de dados do backend | Nenhuma |
| Plugin companheiro | `netbox-proxbox 0.0.33` |

## Identidade estável de guests no Proxmox VE 9.3

Os UIDs de guests e clusters do Proxmox VE 9.3 agora fornecem identidade estável de VMs durante
mudanças de VMID no mesmo endpoint, migração, rollback de snapshot, backup e restauração.
Correspondências exatas por `(cluster-id, uid)` têm precedência sobre a correspondência legada por
endpoint e VMID, mantendo a proteção contra associação entre clusters no NetBox.

A reutilização de um VMID no mesmo cluster Proxmox cria uma nova VM no NetBox quando os UIDs
armazenado e ativo do guest são diferentes. UIDs ausentes e mudanças do UID do cluster mantêm o
comportamento anterior de correspondência. UIDs de cluster persistem de forma independente quando
UIDs de guests estão temporariamente ausentes, e todas as respostas filtradas por UID são
verificadas no cliente. Uma correspondência por UID fica restrita ao endpoint ativo e a um cluster
NetBox verificado, portanto um UID nunca reassocia uma VM pertencente a outro endpoint configurado.

O backend envia campos de UID somente depois de detectar suporte do serializer por metadados
limitados ou linhas não vazias; linhas vazias permanecem com suporte desconhecido. Essa detecção
mantém compatibilidade com versões anteriores do `netbox-proxbox`. A versão pareada
`netbox-proxbox 0.0.33` persiste as identidades nos campos `proxmox_vm_uid` e
`proxmox_cluster_uid`, introduzidos pela migração `0109`.

## Sincronização de rede dos nós

A sincronização de rede dos nós não para mais depois do primeiro nó. Com
`sync_node_interfaces` habilitado, a reconciliação de interfaces de cada nó agora corresponde
somente às interfaces desse próprio nó. Antes, um nó cuja interface ainda não existia no NetBox
podia corresponder a uma interface de mesmo nome, como `vmbr0` ou `eno1`, de outro nó. O NetBox
rejeitava a movimentação com `Components cannot be moved to a different device.`, e a etapa
`network-interfaces` falhava para todos os nós seguintes. Contribuição de Giovanni Condello.

## Endereços DHCP em tempo de execução de LXC

Containers LXC configurados com `ip=dhcp` agora têm seus endereços IP em tempo de execução
sincronizados. A sincronização completa de VMs e as etapas exclusivas de interfaces e IPs leem os
endereços dos containers no endpoint de interfaces LXC do Proxmox, pois containers não têm QEMU
guest agent. Containers parados são ignorados sem requisição, e uma falha de leitura usa a
configuração estática como fallback. Containers em execução agora também recebem linhas de
interfaces de guest do `netbox-proxbox` com a estratégia padrão `guest_os_model`. Contribuição de
Philipp Schwab.

## Atualização e implantação

Implante o pacote exato `proxbox-api 0.0.33rc1` ou o container candidato pelo workflow de release
aprovado. Implante primeiro este backend, depois a versão pareada `netbox-proxbox 0.0.33` e aplique
a migração `0109`. A implantação do backend primeiro é segura porque os campos de UID são
detectados como recurso e omitidos para plugins anteriores.

O pin do SDK muda de `proxmox-sdk 0.0.15` para `proxmox-sdk 0.0.16.post1` para suportar o schema do
Proxmox VE 9.3. Este repositório não tem migração de banco de dados para esta versão.

## Status da versão

`0.0.33rc1` é o release candidate desta versão. A disponibilidade do pacote e em produção segue
os gates de release.
