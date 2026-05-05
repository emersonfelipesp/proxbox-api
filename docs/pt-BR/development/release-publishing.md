# Publicacao de Release

Esta pagina documenta o workflow de publicacao em etapas do pacote
`proxbox-api`. O workflow valida pacotes no TestPyPI primeiro, promove release
candidates no PyPI, e so publica a release final no PyPI e as imagens Docker
depois que o pacote esta instalavel.

## Maquina de Estados da Release

```mermaid
flowchart TD
    Start([Escolher release alvo\nX.Y.Z])
    Bump[Bump da versao do pacote\npyproject.toml + uv.lock]
    TagTest[Criar tag vX.Y.Z\nou vX.Y.Z.postN]
    Prepare[Build do dist\nvalidar tag/versao/uv.lock]
    TestUpload[Upload para TestPyPI\nsem --skip-existing]
    TestInstall[Instalar proxbox-api do TestPyPI\nem Python 3.11, 3.12, 3.13]
    TestChecks[Rodar lint, tipos, compile,\nimport, schema, pytest]
    TestFailed{Alguma validacao\nTestPyPI falhou?}
    PostBump[Bump para proximo vX.Y.Z.postN]
    RCTag[Criar tag candidata PyPI\nvX.Y.ZrcN]
    RCChecks[Rodar checks da candidata\nantes do upload]
    RCUpload[Upload vX.Y.ZrcN para PyPI]
    RCInstall[Instalar rcN do PyPI\nem Python 3.11, 3.12, 3.13]
    Docker[Publicar imagens Docker\nraw, nginx, granian]
    E2E[Rodar E2E pos-publicacao\npacote + imagem publicados]
    RCFailed{RC falhou?}
    NextRC[Bump para vX.Y.ZrcN+1]
    FinalTag[Criar ou disparar tag final\nvX.Y.Z]
    FinalUpload[Upload vX.Y.Z para PyPI]
    FinalInstall[Instalar final do PyPI]
    FinalDocker[Publicar imagens Docker finais]
    FinalE2E[Rodar E2E final pos-publicacao]
    FinalFailed{Precisa de fix\npos-release?}
    PostFix[Bump para vX.Y.Z.postN]
    Done([Release verde])

    Start --> Bump --> TagTest --> Prepare --> TestUpload --> TestInstall --> TestChecks --> TestFailed
    TestFailed -- sim --> PostBump --> TagTest
    TestFailed -- nao --> RCTag --> RCChecks --> RCUpload --> RCInstall --> Docker --> E2E --> RCFailed
    RCFailed -- sim --> NextRC --> RCTag
    RCFailed -- nao --> FinalTag --> FinalUpload --> FinalInstall --> FinalDocker --> FinalE2E --> FinalFailed
    FinalFailed -- sim --> PostFix --> TagTest
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

    Tag->>WF: vX.Y.Z ou vX.Y.Z.postN
    WF->>WF: Validar pyproject + uv.lock + tag
    WF->>TP: Upload do pacote
    WF->>TP: Reinstalar versao exata do pacote
    WF->>WF: Rodar checks locais a partir do codigo

    Tag->>WF: vX.Y.ZrcN, evento de release, ou publish_target=pypi
    WF->>WF: Rodar checks da candidata e E2E pre-publicacao
    WF->>PY: Upload do pacote
    WF->>PY: Reinstalar versao exata do pacote
    WF->>DH: Publicar imagens raw, nginx e granian
    WF->>E2E: Verificar pacote PyPI e imagem Docker publicados
```

## Regras do Workflow

- `pyproject.toml`, `uv.lock` e a tag Git precisam descrever a mesma versao.
- Push de tags normais e `.postN` publica no TestPyPI.
- Push de tags `rcN`, releases do GitHub, ou dispatch manual com
  `publish_target=pypi` publica no PyPI.
- Uploads de pacote intencionalmente nao usam `twine --skip-existing`; se uma
  versao foi consumida por qualquer indice, corrija para frente com o proximo
  `.postN` ou `rcN`.
- Publicacao no PyPI precisa passar pela validacao de reinstalacao do pacote
  antes das imagens Docker serem publicadas.
- Tags Docker usam a mesma versao do pacote PyPI que passou na validacao.

## Checklist Operacional

1. Atualize `pyproject.toml` e regenere `uv.lock`.
2. Crie a tag `vX.Y.Z` e deixe o workflow publicar no TestPyPI.
3. Se a validacao do TestPyPI falhar depois do upload, atualize para
   `vX.Y.Z.post1`, depois `post2`, ate ficar verde.
4. Crie a tag `vX.Y.Zrc1` para validacao de release candidate no PyPI. Se
   falhar depois do upload, continue com `rc2`, `rc3`, e assim por diante.
5. Publique a final `vX.Y.Z` no PyPI apenas depois de uma lane RC verde.
6. Use `vX.Y.Z.postN` para qualquer fix de codigo ou empacotamento descoberto
   depois da publicacao final.
