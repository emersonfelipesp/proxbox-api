# proxbox-api 0.0.30

## Resumo

O NetBox agora pode adicionar um Proxmox Backup Server (PBS) a um cluster Proxmox como storage
por meio de um verbo operacional tipado, protegido e auditado, de modo que um backup de uma
máquina virtual para um PBS pode começar pelo NetBox. O provisionamento de nuvem agora pode
clonar um template para o storage local de outro nó e conectar interfaces de rede extras.

Esta versão não adiciona migração de banco de dados do backend nem mudança de dependências.

## Identidade da versão e compatibilidade

| Componente | Versão ou contrato |
| --- | --- |
| proxbox-api | `0.0.30` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 e 3.13 |
| Migração do banco de dados do backend | Nenhuma |
| Plugin companheiro | Igual à `0.0.29`: um build verificado do `netbox-proxbox` com a ação atômica `sync-cluster` do endpoint |

## Registro tipado de storage PBS

`POST /proxmox/storage/pbs?endpoint_id={id}` adiciona um storage Proxmox do tipo `pbs` com
conteúdo `backup` e `prune-backups=keep-all=1` ao cluster do endpoint. A retenção é fixa porque,
sem ela, o Proxmox aplica a retenção do storage ou do nó depois de cada backup e pode apagar
backups PBS antigos, cuja retenção pertence aos prune jobs do PBS. A rota gerada
`POST /proxmox/api2/storage` continua somente leitura.

- O gate `allow_writes` do endpoint executa antes da validação do corpo.
- Somente `endpoint_id` fica na URL. O corpo JSON contém `storage`, `server`, `port` (padrão
  `8007`), `datastore`, `namespace` opcional, `username` (um id de token de API no formato
  `user@realm!token`), o `password` somente para escrita, a `fingerprint` SHA-256 obrigatória e
  `nodes` opcional.
- O verbo só cria. Um storage existente igual responde `already_present` sem escrita; um
  diferente responde HTTP 409 `storage_conflict` com os nomes dos campos diferentes; uma
  resposta de criação perdida causa uma única leitura da configuração e nunca uma segunda
  requisição.
- Um cabeçalho `Idempotency-Key` repete a resposta por 60 segundos, no escopo do endpoint, do
  verbo e do id do storage.
- Cada requisição que passa pelo gate grava um journal entry do NetBox no `ProxmoxEndpoint`
  espelhado do netbox-proxbox: um entry write-ahead e uma atualização terminal, ou um único
  entry terminal para um corpo inválido. Uma requisição cancelada ainda conclui a chamada ao
  Proxmox, a atualização do journal e a entrada de cache. Uma atualização terminal que falha é
  repetida duas vezes; se ainda falhar, a resposta é HTTP 502 com `journal_finalized=false`, e
  um replay com a mesma chave dentro da janela de 60 segundos repara o entry original sem outra
  escrita no Proxmox.
- O segredo do token nunca aparece em uma resposta, log, comentário de journal, exceção,
  detalhe de validação ou entrada de cache. Um corpo inválido nomeia somente os campos
  declarados que falharam, e uma recusa do Proxmox informa o status HTTP e os nomes dos
  parâmetros rejeitados.

A credencial Proxmox do endpoint precisa de `Datastore.Allocate` em `/storage`. O plugin
netbox-pbs usa este verbo na ação "Register as Proxmox storage".

## Clones de nuvem entre nós e interfaces de rede extras

O Proxmox recusa um clone entre nós quando o storage pedido não é compartilhado ou não está
habilitado no nó do template. O provisionamento entre nós agora lê o inventário de storage do
cluster uma vez:

- Um storage de destino ausente ou indisponível no nó de destino é recusado com
  `target_storage_unavailable`, e uma consulta que falha com `storage_lookup_failed`, antes de
  criar qualquer clone.
- Um storage compartilhado no nó do template mantém o clone direto. Caso contrário, a VM é
  clonada sem storage explícito, e uma etapa separada `relocate_disk` move cada disco de dados
  elegível (não Cloud-Init, não CD-ROM e ainda não no destino) para o storage pedido com
  `move_disk` e `delete=0`, então as cópias de origem ficam como discos não usados e nada é
  apagado. As respostas JSON retornam as tarefas de movimento em `relocate_upids`, e o fluxo de
  eventos informa a etapa `relocate_disk`.
- Uma falha de movimento, uma leitura de configuração que falha ou é malformada, ou uma
  configuração sem discos é recusada com `relocate_disk_failed`, com o VMID, o nó de destino, a
  tarefa de clone e as tarefas por disco já despachadas; a configuração e a inicialização não
  executam.

As requisições de provisionamento também aceitam até oito entradas `extra_networks` para `net1`
a `net31`, cada uma com índice único, bridge validada, tag VLAN opcional, modelo e flag de
firewall opcional. Um endereço MAC existente é preservado, uma configuração ilegível é recusada
com `config_read_failed`, e interfaces extras são recusadas quando a rede de nuvem gerenciada é
obrigatória.

## Limitação conhecida

Como nos outros verbos operacionais, uma atualização terminal de journal pendente fica somente
na janela de idempotência em memória. Depois de 60 segundos, ou depois de um restart, um entry
cuja atualização terminal falhou continua `in_progress` embora a escrita no Proxmox tenha
terminado.

## Status da versão

`0.0.30rc1` é o release candidate desta versão. A disponibilidade do pacote e em produção
segue os gates de release.
