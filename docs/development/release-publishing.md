# Release Publishing

This page documents the staged `proxbox-api` package-release workflow. The
workflow validates release candidates on TestPyPI first, then promotes the
final release to PyPI and publishes Docker images only after PyPI installation
succeeds.

For the broader CI job map and NetBox-backed E2E matrix, see
[CI and E2E Workflows](ci-e2e-workflows.md).

## Release State Machine

```mermaid
flowchart TD
    Start([Choose target release\nX.Y.Z])
    Bump[Bump package version\npyproject.toml + uv.lock]
    RCTag[Create release-candidate tag\nvX.Y.ZrcN]
    RCCI[CI builds dist\nvalidates tag/version/lockfile]
    RCUpload[Upload vX.Y.ZrcN to TestPyPI\nwithout --skip-existing]
    RCValidate[Install rcN from TestPyPI\non Python 3.11, 3.12, 3.13]
    RCChecks[Run lint, type, compile,\nimport, schema, pytest checks]
    RCE2E[E2E Docker\nproxbox-api rcN from TestPyPI]
    RCFailed{Any TestPyPI\nvalidation failed?}
    NextRC[Bump to vX.Y.ZrcN+1]
    FinalTag[Create or dispatch final tag\nvX.Y.Z]
    FinalUpload[Upload vX.Y.Z to PyPI]
    FinalValidate[Install final from PyPI\non Python 3.11, 3.12, 3.13]
    Docker[Publish Docker images\nraw, nginx, granian\n+ experimental PyO3/Rust]
    FinalE2E[Run post-publish E2E\npublished package + Docker image]
    FinalFailed{Post-release fix needed?}
    Post[Bump to vX.Y.Z.postN\npublish .postN to PyPI]
    Done([Release is green])

    Start --> Bump --> RCTag --> RCCI --> RCUpload --> RCValidate --> RCChecks --> RCE2E --> RCFailed
    RCFailed -- yes --> NextRC --> RCTag
    RCFailed -- no --> FinalTag --> FinalUpload --> FinalValidate --> Docker --> FinalE2E --> FinalFailed
    FinalFailed -- yes --> Post --> FinalTag
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

    Tag->>WF: vX.Y.ZrcN
    WF->>WF: Validate pyproject + uv.lock + tag
    WF->>TP: Upload package
    WF->>TP: Reinstall exact rcN version
    WF->>WF: Run local checks from TestPyPI install

    Tag->>WF: vX.Y.Z, vX.Y.Z.postN, release event, or publish_target=pypi
    WF->>WF: Run candidate checks and pre-publish E2E
    WF->>E2E: Wait for NetBox migrations and /api/status/ readiness
    WF->>PY: Upload package
    WF->>PY: Reinstall exact package version
    WF->>DH: Publish raw, nginx, granian, and experimental PyO3/Rust images
    WF->>E2E: Verify published PyPI package and Docker image
```

## Workflow Rules

- `pyproject.toml`, `uv.lock`, and the Git tag must describe the same version.
- `rcN` tag pushes publish to TestPyPI for release-candidate validation.
- Non-rc tag pushes (`vX.Y.Z`, `vX.Y.Z.postN`), GitHub releases, or manual
  dispatch with `publish_target=pypi` publish to PyPI.
- Package uploads intentionally omit `twine --skip-existing`; if a version was
  consumed by any package index, fix forward with the next `.postN` or `rcN`.
- PyPI publication must pass package reinstall validation before Docker images
  are published.
- Docker image tags use the same version as the PyPI package that passed
  validation. Experimental PyO3/Rust images add `-pyo3-rust` tag suffixes and
  opt-in aliases (`experimental`, `pyo3-rust`, and HTTPS variant suffixes).
- Pre-publish and post-publish E2E jobs allow NetBox up to 20 minutes to finish
  migrations/search indexing and require `/api/status/` readiness before
  configuring tokens or backend endpoints.

## Operator Checklist

1. Bump `pyproject.toml` and refresh `uv.lock`.
2. Tag `vX.Y.Zrc1` for TestPyPI release-candidate validation. If validation
   fails after upload, continue with `rc2`, `rc3`, and so on.
3. Publish the final `vX.Y.Z` to PyPI only after an rc lane is green.
4. Use `vX.Y.Z.postN` for any code or packaging fix discovered after final
   PyPI publication.
