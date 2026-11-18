# proxbox-api 0.0.32

## Resumo

Esta versão adiciona o Talos Linux como um produto exclusivo do caminho direto do Pipeline de
Build de Imagens Cloud. Um qcow2 nocloud do Talos é baixado pela API do Proxmox, importado sem
modificação, anexado a uma nova máquina virtual com o drive cloud-init padrão e convertido em
template.

Esta versão adiciona um valor aceito, `talos`, ao enum `product_type` existente no schema da
requisição `POST /cloud/templates/images`. Ela não adiciona nova rota, migração de banco de dados
do backend, mudança de dependência nem mudança no schema de resposta.

## Identidade da versão e compatibilidade

| Componente | Versão ou contrato |
| --- | --- |
| proxbox-api | `0.0.32` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 e 3.13 |
| Migração do banco de dados do backend | Nenhuma |
| Plugin companheiro | `netbox-proxbox 0.0.32.post2` (versão pareada, publicada com esta versão) |

## Importação da imagem Talos sem modificação

O Talos não tem shell nem gerenciador de pacotes, portanto o backend não o envia para um caminho
de personalização de imagem baseado em scripts. O build direto baixa a imagem solicitada no
storage de importação do Proxmox, importa-a como disco de boot do template, adiciona o drive
cloud-init padrão e converte a máquina virtual em template. O proxbox-api não gera user-data do
produto, não grava snippet e não modifica a imagem baixada.

A preservação dos bytes da imagem de origem mantém intactos os argumentos de kernel do Talos e do
Omni SideroLink incorporados em um schematic do Image Factory. O Proxmox ainda pode gravar seu
cloud-config padrão no drive cloud-init separado; o Talos ignora esse user-data e lê do drive
somente a configuração de rede.

## Contrato da requisição

Chame `POST /cloud/templates/images` com `product_type="talos"`, um `image_url` explícito para o
qcow2 nocloud do Talos e os campos existentes do build direto, incluindo `endpoint_id` e
`target_node`. A URL de imagem de uso geral definida como padrão nunca é aceita para o Talos.

As requisições Talos usam exclusivamente o caminho direto. Seletores do pipeline e entradas de
personalização como `execute`, `provider`, `user_data_yaml` e `first_boot_script` são rejeitados.
Os tipos de produto existentes e o comportamento de suas requisições permanecem inalterados.

## Atualização e implantação

Implante o pacote exato `proxbox-api 0.0.32` ou o container publicado pelo workflow de release
aprovado. Implante este backend com a versão pareada `netbox-proxbox 0.0.32.post2`; as versões mais
recentes dos dois projetos são sempre pareadas. Mantenha `proxmox-sdk 0.0.15` e
`netbox-sdk 0.0.13`; esta versão não exige atualização de dependências nem migração de banco de
dados do backend.

Antes de criar um template Talos, use uma URL de qcow2 nocloud do Talos Image Factory cujo
schematic contenha a configuração de máquina necessária e quaisquer argumentos de kernel do Omni
SideroLink. Os gates existentes de autorização de escrita no endpoint e de build de templates
continuam aplicáveis à mutação direta no Proxmox.

## Limitações conhecidas

- Builds Talos exigem uma URL de imagem explícita e não podem usar a imagem padrão da requisição.
- Builds Talos não podem usar o pipeline SSH, user-data cloud-init personalizado nem scripts de
  primeiro boot.
- O proxbox-api não inspeciona nem altera o schematic Talos incorporado na imagem; os chamadores
  continuam responsáveis por selecionar o artefato correto do Image Factory.
