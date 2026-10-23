# proxbox-api 0.0.30.post2

## Resumo

Este post-release restaura a sincronização de clusters com versões do netbox-proxbox que não têm
a ação `sync-cluster`, como a 0.0.29.post3. Desde a 0.0.29, toda sincronização que resolve um
cluster nativo do NetBox chamava essa ação e parava quando o NetBox respondia com a página "Page
Not Found". Foram afetadas as atualizações completas, as sincronizações individuais de cluster,
node, VM, device e storage, e a criação de VMs. Esta versão não adiciona migração de banco de
dados, mudança de contrato da API nem mudança de dependências.

## Identidade da versão

| Componente | Versão ou contrato |
| --- | --- |
| proxbox-api | `0.0.30.post2` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 e 3.13 |
| Migração do banco de dados | Nenhuma |

## Plugins sem a ação de claim

Antes de fazer o claim de um cluster, o backend agora envia um GET para a URL exata da ação. A
requisição não usa cache, não segue redirecionamentos e lê um corpo limitado. Somente um HTTP 404
com corpo HTML prova que o plugin não tem essa rota. Qualquer outra resposta, incluindo um 404
JSON, 403, 405, erro de servidor ou timeout, mantém sem mudanças o claim atômico do endpoint. Um
plugin que tem a ação, portanto, nunca usa o novo caminho.

Quando a rota não existe, o backend adota um cluster nativo existente somente para leitura, e
apenas pelos registros de acompanhamento do próprio plugin, que pertencem a cada endpoint. Toda
leitura desse caminho ignora os caches. O token NetBox do backend precisa pertencer a um
superusuário, comprovado quando o próprio usuário corresponde a `is_superuser=true` e não
corresponde a `is_superuser=false`, para que os registros lidos sejam o conjunto completo. O
endpoint é resolvido de novo a partir de uma página completa e precisa ser o mesmo endpoint ativo
com a mesma identidade de transporte, todos os registros de acompanhamento vêm de uma única
página completa e o cluster é lido pelo ID com a identidade validada. Ele
adota o cluster nativo C para o endpoint somente quando todas estas condições são verdadeiras:

- Exatamente um registro de acompanhamento do endpoint tem o nome exato do cluster, e esse
  registro aponta para C.
- Todo registro de acompanhamento vinculado do endpoint aponta para C.
- Nenhum registro de acompanhamento de outro endpoint aponta para C.
- O nome atual de C é igual ao nome do cluster.

Essas condições são a mesma regra de unicidade que a migração do netbox-proxbox 0.0.31 usa para
vincular endpoints aos seus clusters, então o backend adota somente pares que a própria
atualização do plugin vincula. O backend lê todos os registros de acompanhamento uma vez e faz
todas as comparações, então um servidor que ignora um filtro de listagem não consegue ampliar a
correspondência. Ele nunca cria, vincula, renomeia ou associa um cluster por nome entre
endpoints, e nunca grava o cluster adotado nem o sidecar de estado de sincronização dele; o
cluster é apenas referenciado pelos devices e VMs do endpoint. Com qualquer token que não seja de um
superusuário, a adoção é recusada. Em todos os outros casos, a sincronização para com uma mensagem que cita a ação
ausente e a atualização do plugin.

## Atualização

Faça o deploy do pacote exato `proxbox-api 0.0.30.post2` ou do container publicado pelo workflow
de release aprovado e reinicie todos os workers do backend. Clusters que ainda não estão
vinculados ao seu endpoint por um registro de acompanhamento continuam precisando do
netbox-proxbox 0.0.31 ou posterior. Este caminho de compatibilidade será removido quando todos os
ambientes suportados executarem um plugin com a ação.
