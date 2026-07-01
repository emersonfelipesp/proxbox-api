# Workflows de CI e E2E

Esta pagina documenta a superficie de GitHub Actions para desenvolvedores do
`proxbox-api`: validacao rapida, smoke tests de imagens Docker, matriz E2E com
NetBox e publicacao em etapas.

## Mapa dos workflows

| Workflow | Gatilho | Finalidade |
|---|---|---|
| `.github/workflows/ci.yml` | Push, pull request, release, dispatch manual | Roda checagens principais e a matriz E2E Docker com NetBox + Proxmox. |
| `.github/workflows/publish-testpypi.yml` | Tag de versao, GitHub release, dispatch manual | Publica versoes imutaveis no TestPyPI, candidatos PyPI, releases finais PyPI, imagens Docker e E2E pos-publicacao. |
| `.github/workflows/docker-hub-publish.yml` | Workflow reutilizavel / dispatch manual | Constroi e publica variantes raw, nginx, granian e experimentais PyO3/Rust da imagem Docker. |
| `.github/workflows/release-docker-verify.yml` | Release / dispatch manual | Baixa as tags Docker publicadas, incluindo as tags experimentais PyO3/Rust, e verifica startup dos conteineres. |
| `.github/workflows/docs.yml` | Mudancas de docs em main / PR | Constroi e publica o site MkDocs. |
| `.github/workflows/nightly-schema-refresh.yml` | Agendamento / dispatch manual | Atualiza schemas Proxmox gerados e abre PR quando houver mudanca. |

## Fluxo do CI

```mermaid
flowchart TD
    Push[Push / PR / execucao manual]
    Core[test\nruff + ty + compile + pytest]
    Py311[test-py311-floor\ncompile + pytest core]
    Free[test-free-threaded\ncontinue-on-error]
    Bind[Docker bind-host smoke\nraw + granian]
    Setup[setup\ngera matriz E2E]
    BuildNB[build-netbox-image\npull ou build NetBox uma vez]
    BuildSvc[prepare-e2e-service-images\nPostgreSQL + Redis + nginx]
    BuildPM[prepare-proxmox-image\nimagens mock Proxmox]
    BuildPB[build-proxbox-image\ntargets Proxbox API]
    E2E[e2e-docker\nmatriz transporte x versao NetBox]

    Push --> Core
    Push --> Py311
    Push --> Free
    Push --> Bind
    Push --> Setup
    Setup --> BuildNB
    Setup --> BuildPM
    Setup --> BuildPB
    Push --> BuildSvc
    Core --> E2E
    Setup --> E2E
    BuildNB --> E2E
    BuildSvc --> E2E
    BuildPM --> E2E
    BuildPB --> E2E
```

O CI prepara imagens Docker uma vez como artefatos temporarios do workflow, e
cada job da matriz E2E carrega esses artefatos antes de subir a stack. Isso
evita pulls repetidos do Docker Hub e rebuilds do Proxbox API em uma matriz
grande de versoes do NetBox. Imagens oficiais de Python, PostgreSQL, Redis,
nginx e a base fallback do NetBox sao baixadas por `mirror.gcr.io/library` para
evitar falhas por cota do Docker Hub. A imagem mock do Proxmox e construida a
partir do pacote local `proxmox-mock/` para cada marcador de servico `pve`,
`pbs` e `pdm`. O job do NetBox baixa a imagem publica quando disponivel e faz
fallback para build a partir do codigo-fonte quando a imagem do registro nao
existe. Esse build fallback segue a base atual do `netbox-docker`,
`ubuntu:26.04`, usando a referencia via mirror.

## Stack E2E

O `ci.yml` sobe uma stack real e verifica que o `proxbox-api` consegue
autenticar, configurar endpoints do NetBox e rodar testes de sincronizacao em
todos os transportes suportados.

```mermaid
flowchart LR
    GA[GitHub Actions runner]

    subgraph Stack[Docker network: proxbox-e2e]
        NB[Container NetBox\nnetbox-proxbox instalado]
        NGINX[nginx HTTPS opcional]
        API[Container proxbox-api\ntarget raw, nginx ou granian]
        PM[Container mock Proxmox\nproxmox-mock local]
        PG[(PostgreSQL)]
        RD[(Redis)]
    end

    GA --> NB
    GA --> API
    GA --> PM
    NB --> PG
    NB --> RD
    API -->|REST NetBox| NB
    API -->|API Proxmox| PM
    NGINX --> NB
```

Regras importantes do E2E:

- A prontidao do NetBox aguarda ate 20 minutos por migracoes/indexacao.
- `/api/status/` precisa estar pronto antes de configurar tokens e endpoints.
- Imagens Docker sao carregadas a partir de artefatos preparados; os jobs da
  matriz E2E nao fazem pull do Docker Hub nem rebuild direto dos conteineres
  Proxbox API.
- Containers mock do Proxmox usam o pacote local de mock schema-driven e expoem
  `PROXMOX_MOCK_SERVICE` para validar o marcador ativo em testes PBS/PDM.
- Testes Docker com mock Proxmox usam o marker `mock_http`.
- A passagem em processo com `MockBackend` roda separadamente com o marker
  `mock_backend`.
- Eventos de release rodam modos `dev` e `pypi` do `netbox-proxbox`; CI normal
  de push/PR usa o modo de desenvolvimento.

## Validacao de release

```mermaid
sequenceDiagram
    participant Tag as Tag de versao
    participant WF as publish-testpypi.yml
    participant TP as TestPyPI
    participant PY as PyPI
    participant DH as Docker Hub
    participant E2E as Stack E2E NetBox

    Tag->>WF: vX.Y.Z ou vX.Y.Z.postN
    WF->>WF: Validar pyproject + uv.lock + tag
    WF->>TP: Publicar proxbox-api
    WF->>TP: Reinstalar versao exata em Python 3.11, 3.12, 3.13
    WF->>WF: Rodar lint, tipos, compile, import, schema e pytest

    Tag->>WF: vX.Y.ZrcN, release event, ou publish_target=pypi
    WF->>E2E: Rodar E2E pre-publicacao com dependencias dev
    WF->>PY: Publicar proxbox-api
    WF->>PY: Reinstalar pacote exato
    WF->>DH: Publicar imagens raw, nginx, granian + variantes experimentais PyO3/Rust
    WF->>E2E: Rodar E2E pos-publicacao com pacote + imagem publicados
```

Uploads de pacote intencionalmente nao usam `twine --skip-existing`. Se alguma
validacao falhar depois do upload, publique uma versao fix-forward:
`vX.Y.Z.postN` para TestPyPI ou correcoes pos-release, e `vX.Y.ZrcN` para novas
tentativas de release candidate no PyPI.
