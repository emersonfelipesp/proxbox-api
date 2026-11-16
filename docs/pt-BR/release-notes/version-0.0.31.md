# proxbox-api 0.0.31

## Resumo

Esta versão limita a sincronização de discos e snapshots de máquinas virtuais ao endpoint Proxmox
selecionado, resolve o contrato de schema do NetBox uma vez por execução de sincronização em vez
de uma vez por máquina virtual, preserva o `dns_name` administrado pelo operador quando o Proxmox
não fornece hostname e reflete a configuração de inicialização automática do Proxmox nas máquinas
virtuais nativas do NetBox.

Esta versão não adiciona migração de banco de dados do backend, mudança no contrato da API nem
mudança de dependências.

## Identidade da versão e compatibilidade

| Componente | Versão ou contrato |
| --- | --- |
| proxbox-api | `0.0.31rc1` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 e 3.13 |
| Migração do banco de dados do backend | Nenhuma |
| Plugin companheiro | `netbox-proxbox 0.0.32` |

## Sincronização de discos e snapshots no escopo do endpoint

Uma sincronização de discos ou snapshots sem seleção explícita de máquinas virtuais ainda lê o
inventário de máquinas virtuais do NetBox, mas agora inicia o trabalho por VM somente para as
máquinas virtuais pertencentes aos endpoints Proxmox da execução. Uma máquina virtual é excluída
quando seu estado de sincronização identifica um endpoint, todas as sessões Proxmox ativas expõem
uma identidade de endpoint e nenhuma corresponde.

Máquinas virtuais sem identidade de endpoint continuam elegíveis, e seleções explícitas de
máquinas virtuais permanecem inalteradas. As máquinas virtuais excluídas não entram na contagem,
não emitem registros de progresso e não produzem avisos de item ignorado. As verificações de
propriedade existentes continuam protegendo as escritas no NetBox e a limpeza de objetos antigos.

As etapas de discos e snapshots também informam a duração total e a contagem e o tempo de espera
das requisições ao NetBox observadas durante a etapa. Uma requisição ao NetBox que leva pelo menos
cinco segundos emite um aviso com o método HTTP e o caminho, sem valores da query.

## Uma resolução do contrato de schema do NetBox por execução

A validação de payloads de máquinas virtuais agora reutiliza um contrato OpenAPI do NetBox
resolvido dentro de cada processo proxbox-api. Workers concorrentes compartilham a primeira
resolução em vez de baixar e analisar o schema completo do NetBox para cada máquina virtual.

`PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL` controla a reutilização e tem o padrão de `3600` segundos. O
valor `0` desativa a reutilização. Um contrato obtido do cache em disco ou do fallback incluído
após uma falha da consulta ao vivo é reutilizado por no máximo 60 segundos, permitindo uma nova
tentativa rápida quando o NetBox se recupera. A mudança do endpoint NetBox configurado invalida o
contrato em memória, e o download e a validação do schema executam fora do event loop.

## Nomes DNS do operador e estado de inicialização automática

A reconciliação de endereços IP não interpreta mais a ausência de hostname no Proxmox como uma
instrução para limpar o `dns_name` existente no NetBox. Isso vale para a sincronização individual
e em lote, independentemente da flag de sobrescrita do nome DNS. Endereços novos ainda recebem um
valor vazio compatível com o NetBox quando não há hostname, e um hostname realmente resolvido
continua sujeito à política de sobrescrita configurada.

Máquinas virtuais QEMU e LXC agora gravam o campo nativo de inicialização automática do NetBox a
partir da configuração `onboot` do Proxmox. Máquinas virtuais existentes convergem na próxima
sincronização. Quando a configuração do guest não está disponível, a sincronização não altera o
campo no NetBox.

## Atualização e implantação

Implante o pacote exato `proxbox-api 0.0.31rc1` ou o container candidato pelo workflow de release
aprovado e reinicie todos os workers do backend para que cada processo comece com a nova versão e
um cache em memória vazio do contrato de schema. Implante primeiro este backend e depois a versão pareada
`netbox-proxbox 0.0.32`; as versões mais recentes dos dois projetos são sempre pareadas. Mantenha
`proxmox-sdk 0.0.15` e `netbox-sdk 0.0.13`; esta versão não exige atualização de dependências nem
migração de banco de dados do backend.

Depois da implantação, execute a sincronização para cada endpoint configurado e verifique que as
contagens de discos e snapshots contêm somente as máquinas virtuais desse endpoint, que os nomes
DNS do operador permanecem intactos quando o Proxmox não fornece hostname e que os valores de
inicialização automática correspondem ao Proxmox. Depois de uma atualização do schema do NetBox,
reinicie o proxbox-api ou aguarde o tempo de vida configurado do contrato de schema antes de
validar o novo contrato.

## Limitações conhecidas

- O filtro por endpoint não pode excluir uma máquina virtual cujo estado de sincronização não
  tenha identidade de endpoint, nem uma execução cujas sessões Proxmox não exponham todas as
  identidades de endpoint. As verificações de propriedade existentes continuam impedindo escritas
  entre endpoints.
- O cache do contrato de schema pertence ao processo. Cada processo do backend resolve seu próprio
  contrato, e uma mudança no schema do NetBox fica visível após o fim do tempo de vida ou o restart
  do processo.
- A ausência de hostname preserva um `dns_name` existente; ela não oferece uma forma de solicitar
  que a sincronização limpe um valor administrado pelo operador.
- A reconciliação da inicialização automática exige uma configuração legível do guest no Proxmox.
  Quando essa leitura não está disponível, o NetBox mantém o valor atual.

## Status da versão

`0.0.31rc1` é o release candidate desta versão. A disponibilidade do pacote e em produção segue
os gates de release.
