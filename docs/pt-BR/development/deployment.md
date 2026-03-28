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
