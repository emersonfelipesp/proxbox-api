# Deploy e GitHub Pages

A documentacao e publicada automaticamente no GitHub Pages usando a branch `gh-pages`.

## Workflow

Arquivo:

- `.github/workflows/docs.yml`

Comportamento:

- PRs para `main` com alteracoes em docs executam build estrito.
- Push em `main` com alteracoes em docs executa build e deploy.
- Tambem pode ser executado manualmente (`workflow_dispatch`).

## Destino de publicacao

- Branch: `gh-pages`
- Pasta publicada: `site/`

## Contrato de implantacao do servico

O workflow privado do Gitea mantem staging e producao separados. Um push
revisado em `develop` implanta somente staging. A producao e manual, executa
somente a partir de `main` canonica e usa por padrao um pacote imutavel exato;
o commit canonico de `main` e apenas uma substituicao explicita. A producao
exige CI verde para a origem exata, autorizacao de uso unico vinculada a
execucao e aos artefatos solicitados, validacao do recibo assinado de conclusao
e limpeza incondicional do material de autorizacao. Dispatches manuais em
`develop` sao rejeitados e nao funcionam como implantacoes de staging. Nao ha
desvio emergencial de CI porque a autorizacao assinada nao vincula esse desvio.
