# Publicacao de Release

Esta pagina documenta o workflow de publicacao em etapas do pacote
`proxbox-api`. O workflow valida release candidates no TestPyPI primeiro, e
so promove a release final ao PyPI e publica as imagens Docker depois que a
instalacao a partir do PyPI funciona.

Para o mapa completo dos jobs de CI e da matriz E2E com NetBox, veja
[Workflows de CI e E2E](ci-e2e-workflows.md).

## Maquina de Estados da Release

```mermaid
flowchart TD
    Start([Escolher release alvo\nX.Y.Z])
    Bump[Bump da versao do pacote\npyproject.toml + uv.lock]
    RCTag[Criar tag de release candidate\nvX.Y.ZrcN]
    RCCI[CI faz build do dist\nvalida tag/versao/uv.lock]
    RCUpload[Upload vX.Y.ZrcN para TestPyPI\nsem --skip-existing]
    RCValidate[Instalar rcN do TestPyPI\nem Python 3.11, 3.12, 3.13]
    RCChecks[Rodar lint, tipos, compile,\nimport, schema, pytest]
    RCE2E[E2E Docker\nproxbox-api rcN do TestPyPI]
    RCFailed{Alguma validacao\nTestPyPI falhou?}
    NextRC[Bump para vX.Y.ZrcN+1]
    FinalTag[Criar ou disparar tag final\nvX.Y.Z]
    FinalUpload[Upload vX.Y.Z para PyPI]
    FinalValidate[Instalar final do PyPI\nem Python 3.11, 3.12, 3.13]
    Docker[Publicar imagens Docker\nraw, nginx, granian]
    FinalE2E[Rodar E2E pos-publicacao\npacote PyPI + imagem Docker]
    FinalFailed{Precisa de fix\npos-release?}
    Post[Bump para vX.Y.Z.postN\npublicar .postN no PyPI]
    Done([Release verde])

    Start --> Bump --> RCTag --> RCCI --> RCUpload --> RCValidate --> RCChecks --> RCE2E --> RCFailed
    RCFailed -- sim --> NextRC --> RCTag
    RCFailed -- nao --> FinalTag --> FinalUpload --> FinalValidate --> Docker --> FinalE2E --> FinalFailed
    FinalFailed -- sim --> Post --> FinalTag
    FinalFailed -- nao --> Done
```

## Lanes do Workflow

```mermaid
sequenceDiagram
    participant Tag as Tag de versao
    participant WF as publish-testpypi.yml
    participant TP as TestPyPI
    participant PY as PyPI
    participant DH as Docker Hub
    participant E2E as Stack E2E

    Tag->>WF: vX.Y.ZrcN
    WF->>WF: Validar pyproject + uv.lock + tag
    WF->>TP: Upload do pacote
    WF->>TP: Reinstalar versao exata rcN
    WF->>WF: Rodar checks locais a partir da instalacao TestPyPI

    Tag->>WF: vX.Y.Z, vX.Y.Z.postN, evento de release, ou publish_target=pypi
    WF->>WF: Rodar checks da candidata e E2E pre-publicacao
    WF->>E2E: Aguardar migracoes do NetBox e /api/status/
    WF->>PY: Upload do pacote
    WF->>PY: Reinstalar versao exata do pacote
    WF->>DH: Publicar imagens raw, nginx e granian
    WF->>E2E: Verificar pacote PyPI e imagem Docker publicados
```

## Regras do Workflow

- `pyproject.toml`, `uv.lock` e a tag Git precisam descrever a mesma versao.
- Push de tags `rcN` publica no TestPyPI para validacao de release candidate.
- Push de tags nao-rc (`vX.Y.Z`, `vX.Y.Z.postN`), releases do GitHub, ou
  dispatch manual com `publish_target=pypi` publica no PyPI.
- Uploads de pacote intencionalmente nao usam `twine --skip-existing`; se uma
  versao foi consumida por qualquer indice, corrija para frente com o proximo
  `.postN` ou `rcN`.
- Publicacao no PyPI precisa passar pela validacao de reinstalacao do pacote
  antes das imagens Docker serem publicadas.
- Tags Docker usam a mesma versao do pacote PyPI que passou na validacao.
- Jobs E2E pre-publicacao e pos-publicacao aguardam ate 20 minutos para o
  NetBox concluir migracoes/indexacao e exigem `/api/status/` pronto antes de
  configurar tokens ou endpoints do backend.

## Checklist Operacional

1. Atualize `pyproject.toml` e regenere `uv.lock`.
2. Crie a tag `vX.Y.Zrc1` para validacao de release candidate no TestPyPI. Se
   a validacao falhar depois do upload, continue com `rc2`, `rc3`, e assim
   por diante.
3. Publique a final `vX.Y.Z` no PyPI apenas depois de uma lane rc verde.
4. Use `vX.Y.Z.postN` para qualquer fix de codigo ou empacotamento descoberto
   depois da publicacao final no PyPI.
