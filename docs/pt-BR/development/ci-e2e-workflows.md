# Workflows de CI e E2E

Esta pagina documenta a superficie de GitHub Actions para desenvolvedores do
`proxbox-api`: validacao rapida, smoke tests de imagens Docker, matriz E2E com
NetBox e publicacao em etapas.

## Mapa dos workflows

| Workflow | Gatilho | Finalidade |
|---|---|---|
| `.github/workflows/ci.yml` | Push, pull request, release, dispatch manual | Roda checagens principais e a matriz E2E Docker com NetBox + Proxmox. |
| `.github/workflows/publish-testpypi.yml` | Tag de versao, GitHub release, dispatch manual | Publica versoes imutaveis no TestPyPI, candidatos PyPI, releases finais PyPI, imagens Docker e E2E pos-publicacao. |
| `.github/workflows/docker-hub-publish.yml` | Workflow reutilizavel / dispatch manual | Constroi e publica variantes raw, nginx e granian da imagem Docker. |
| `.github/workflows/release-docker-verify.yml` | Release / dispatch manual | Baixa as tags Docker publicadas e verifica startup dos conteineres. |
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
    BuildNB[build-netbox-image\nso se pull do registro falhar]
    E2E[e2e-docker\nmatriz transporte x versao NetBox]

    Push --> Core
    Push --> Py311
    Push --> Free
    Push --> Bind
    Push --> Setup
    Setup --> BuildNB
    Core --> E2E
    Setup --> E2E
    BuildNB --> E2E
```

Os jobs E2E tentam baixar primeiro a imagem publica do NetBox. Eles so baixam o
artefato de imagem construido a partir do codigo-fonte quando esse pull falha.

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
        PM[Container mock Proxmox\nproxmox-sdk:latest]
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
    WF->>DH: Publicar imagens raw, nginx, granian
    WF->>E2E: Rodar E2E pos-publicacao com pacote + imagem publicados
```

Uploads de pacote intencionalmente nao usam `twine --skip-existing`. Se alguma
validacao falhar depois do upload, publique uma versao fix-forward:
`vX.Y.Z.postN` para TestPyPI ou correcoes pos-release, e `vX.Y.ZrcN` para novas
tentativas de release candidate no PyPI.
