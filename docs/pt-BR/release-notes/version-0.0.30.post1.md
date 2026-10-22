# proxbox-api 0.0.30.post1

## Resumo

Este post-release corrige o gravador de journal do NetBox usado pelos verbos operacionais. Na
0.0.30, todo verbo que grava um journal entry write-ahead recusava antes de chegar ao Proxmox,
embora o NetBox tivesse criado o entry: o verbo tipado `POST /proxmox/storage/pbs` respondia
HTTP 502, e os verbos start, stop, reboot, snapshot, migrate, delete, backup e snapshot-delete
respondiam HTTP 409. Esta versão não adiciona migração de banco de dados, mudança de contrato da API
nem mudança de dependências.

## Identidade da versão

| Componente | Versão ou contrato |
| --- | --- |
| proxbox-api | `0.0.30.post1` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 e 3.13 |
| Migração do banco de dados | Nenhuma |

## Journal entry write-ahead

Os verbos falham de forma fechada quando não conseguem gravar o journal entry write-ahead, e
precisam do identificador dele para gravar a atualização terminal. A camada REST do NetBox
responde a uma criação com um objeto de registro cujos campos são acessados pela sua
serialização, mas o gravador de journal aceitava somente um dicionário simples ou um objeto com
atributo `data`. Por isso ele não retornava nada para cada entry que criava, e cada verbo recusava
com "The NetBox audit journal entry returned no ID" ou "write-ahead journal create returned no
id". O entry criado ficava em `result: in_progress`.

O gravador de journal agora retorna os campos desse registro, então os verbos mantêm o
identificador do seu entry, chegam ao Proxmox e finalizam o mesmo entry. Os testes de regressão
usam o tipo de registro que o NetBox retorna em vez de um dicionário simples.

## Atualização

Faça o deploy do pacote exato `proxbox-api 0.0.30.post1` ou do container publicado pelo workflow
de release aprovado e reinicie todos os workers do backend. Os journal entries que a 0.0.30
deixou em `result: in_progress` permanecem como auditoria das requisições recusadas; nenhuma
mudança foi feita no Proxmox para elas. Repita a operação recusada depois da atualização.
