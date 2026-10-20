# proxbox-api 0.0.29

## Estado da versão

Estas notas descrevem a versão planejada `0.0.29`. A preparação atual do pacote usa
`0.0.29rc1`. Uma candidata não comprova a disponibilidade da versão final nem a aprovação
para produção.

## Resumo

A sincronização de clusters agora resolve os clusters nativos do NetBox pela propriedade
exata do endpoint Proxmox. O backend solicita ao plugin uma associação atômica antes de
alterar os metadados do cluster nativo. Nomes iguais não selecionam o cluster de outro
endpoint. Endpoints de cluster e standalone usam o mesmo contrato.

Esta versão mantém a correção de `0.0.28.post1` para inicializar o serviço NetBox padrão antes
de descriptografar credenciais Proxmox armazenadas. Não há migração de banco de dados do
backend nem alteração de dependências.

## Identidade e compatibilidade

| Componente | Versão ou contrato |
| --- | --- |
| Candidata proxbox-api | `0.0.29rc1`, com destino à final `0.0.29` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 e 3.13 |
| Migração de banco do backend | Nenhuma |
| Plugin complementar | Build verificado de `netbox-proxbox` com a ação atômica `sync-cluster` do endpoint |

A sincronização requer
`POST /api/plugins/proxbox/endpoints/proxmox/{id}/sync-cluster/` no plugin complementar.
A ação deve associar o endpoint, o cluster nativo, o registro de acompanhamento e o sidecar
tipado em uma única transação. A resposta contém o cluster nativo, o identificador do registro
de acompanhamento e o booleano estrito `created`. O booleano estrito opcional `changed`
indica alterações substanciais no cluster nativo. Sua ausência equivale a `false`.

Verifique a capacidade e as permissões do token de serviço na versão selecionada do plugin.
Estas notas não afirmam que a versão final `0.0.30` do plugin esteja disponível. O plugin
mantém sua própria migração de esquema e qualificação de versão.

## Resolução do nó de VMs

A descoberta do nó de VMs e contêineres agora consulta `cluster/resources` do Proxmox com
`type=vm` e compara o tipo do convidado retornado, além do VMID. Isso evita consultas
inválidas com `type=qemu` ou `type=lxc` e impede a confusão entre uma VM e um contêiner
com o mesmo ID numérico. A clonagem entre nós envia a operação ao nó que possui o
template e mantém o nó de destino solicitado como alvo. O mock HTTP independente
reconhece o mesmo filtro amplo de convidados.

## Propriedade do cluster nativo

Atualizações completas, atualizações individuais de clusters e nós e a criação de dependências
de VMs usam a identidade exata do endpoint do plugin. Os IDs do banco do backend são
independentes dos IDs do plugin. O resolvedor confirma o host e a porta da conexão e rejeita
endpoints ambíguos ou desabilitados.

A relação estabelecida permanece válida após renomear o cluster Proxmox e nas novas
tentativas. O backend atualiza metadados pela chave primária retornada pela ação. Ele não
adota nem repara clusters por uma busca global de nome. Nomes iguais em endpoints distintos
não autorizam propriedade compartilhada. Um conflito de nome e localização produz uma falha
explícita. Corrija o inventário ou a localização antes de tentar novamente.

A ausência da ação, uma associação rejeitada, uma resposta inválida ou uma identidade não
resolvida interrompe as escritas no cluster nativo. Não existe alternativa por nome.
Pré-requisitos de tipo e site podem existir quando a ação falha. Uma tentativa sem alterações
retorna `unchanged`. Um novo cluster retorna `created`. Uma alteração substancial pela ação
ou uma atualização posterior de metadados retorna `updated`. A omissão de site e tenant
preserva a localização existente.

## Atualização coordenada

CUIDADO: Um backend antigo ainda pode escrever por nome. Conclua sua substituição antes
de expor a nova relação de propriedade do plugin.

1. Implante primeiro este backend com a proteção, antes de atualizar o plugin.
2. Verifique a revisão exata do backend. Substitua ou drene os processos antigos da implantação e seu trabalho em andamento.
3. Implante e migre o plugin qualificado pelo procedimento de versão aprovado.
4. Verifique a ação atômica, a identidade exata do endpoint e as permissões do token de serviço.
5. Execute a aceitação autenticada em staging para cluster e standalone antes da aprovação de produção.

Enquanto o plugin não fornecer a ação, a sincronização afetada falha antes de escrever
metadados do cluster nativo. Preserve essa proteção durante a atualização. Não restaure
uma alternativa por nome para ocultar a falha.

Valide a relação endpoint/cluster nativo, uma renomeação, uma tentativa sem alterações e
nomes iguais em endpoints distintos. Examine os diagnósticos de migração do plugin para
inventário legado ambíguo. Use o procedimento de recuperação do plugin, sem reparar
registros de acompanhamento por nome. A aceitação autenticada em staging e os gates
da versão continuam obrigatórios antes de produção.

Consulte [Fluxos de sincronização](../sync/workflows.md) para as regras de identidade,
semântica da resposta, comportamento de localização e sequência de implantação.
