# Release Publishing

This page documents the staged `proxbox-api` package-release workflow. The
workflow validates packages on TestPyPI first, promotes release candidates on
PyPI, then publishes the final PyPI release and Docker images only after the
package is installable.

## Release State Machine

```mermaid
flowchart TD
    Start([Choose target release\nX.Y.Z])
    Bump[Bump package version\npyproject.toml + uv.lock]
    TagTest[Create tag vX.Y.Z\nor vX.Y.Z.postN]
    Prepare[Build dist\nvalidate tag/version/uv.lock]
    TestUpload[Upload to TestPyPI\nwithout --skip-existing]
    TestInstall[Install proxbox-api from TestPyPI\non Python 3.11, 3.12, 3.13]
    TestChecks[Run lint, type, compile,\nimport, schema, pytest checks]
    TestFailed{Any TestPyPI\nvalidation failed?}
    PostBump[Bump to next vX.Y.Z.postN]
    RCTag[Create PyPI candidate tag\nvX.Y.ZrcN]
    RCChecks[Run candidate checks\nbefore upload]
    RCUpload[Upload vX.Y.ZrcN to PyPI]
    RCInstall[Install rcN from PyPI\non Python 3.11, 3.12, 3.13]
    Docker[Publish Docker images\nraw, nginx, granian]
    E2E[Run post-publish E2E\npublished package + Docker image]
    RCFailed{RC failed?}
    NextRC[Bump to vX.Y.ZrcN+1]
    FinalTag[Create or dispatch final tag\nvX.Y.Z]
    FinalUpload[Upload vX.Y.Z to PyPI]
    FinalInstall[Install final from PyPI]
    FinalDocker[Publish final Docker images]
    FinalE2E[Run final post-publish E2E]
    FinalFailed{Post-release fix needed?}
    PostFix[Bump to vX.Y.Z.postN]
    Done([Release is green])

    Start --> Bump --> TagTest --> Prepare --> TestUpload --> TestInstall --> TestChecks --> TestFailed
    TestFailed -- yes --> PostBump --> TagTest
    TestFailed -- no --> RCTag --> RCChecks --> RCUpload --> RCInstall --> Docker --> E2E --> RCFailed
    RCFailed -- yes --> NextRC --> RCTag
    RCFailed -- no --> FinalTag --> FinalUpload --> FinalInstall --> FinalDocker --> FinalE2E --> FinalFailed
    FinalFailed -- yes --> PostFix --> TagTest
    FinalFailed -- no --> Done
```

## Workflow Lanes

```mermaid
sequenceDiagram
    participant Tag as Version tag
    participant WF as publish-testpypi.yml
    participant TP as TestPyPI
    participant PY as PyPI
    participant DH as Docker Hub
    participant E2E as E2E stack

    Tag->>WF: vX.Y.Z or vX.Y.Z.postN
    WF->>WF: Validate pyproject + uv.lock + tag
    WF->>TP: Upload package
    WF->>TP: Reinstall exact package version
    WF->>WF: Run local checks from source

    Tag->>WF: vX.Y.ZrcN, release event, or publish_target=pypi
    WF->>WF: Run candidate checks and pre-publish E2E
    WF->>PY: Upload package
    WF->>PY: Reinstall exact package version
    WF->>DH: Publish raw, nginx, and granian images
    WF->>E2E: Verify published PyPI package and Docker image
```

## Workflow Rules

- `pyproject.toml`, `uv.lock`, and the Git tag must describe the same version.
- Normal and `.postN` tag pushes publish to TestPyPI.
- `rcN` tag pushes, GitHub releases, or manual dispatch with
  `publish_target=pypi` publish to PyPI.
- Package uploads intentionally omit `twine --skip-existing`; if a version was
  consumed by any package index, fix forward with the next `.postN` or `rcN`.
- PyPI publication must pass package reinstall validation before Docker images
  are published.
- Docker image tags use the same version as the PyPI package that passed
  validation.

## Operator Checklist

1. Bump `pyproject.toml` and refresh `uv.lock`.
2. Tag `vX.Y.Z` and let the workflow publish to TestPyPI.
3. If TestPyPI validation fails after upload, bump to `vX.Y.Z.post1`, then
   `post2`, until green.
4. Tag `vX.Y.Zrc1` for PyPI release-candidate validation. If it fails after
   upload, continue with `rc2`, `rc3`, and so on.
5. Publish the final `vX.Y.Z` to PyPI only after an RC lane is green.
6. Use `vX.Y.Z.postN` for any code or packaging fix discovered after final
   publication.
