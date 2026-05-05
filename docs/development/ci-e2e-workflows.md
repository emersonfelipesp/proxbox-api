# CI and E2E Workflows

This page documents the developer-facing GitHub Actions surface for
`proxbox-api`: fast validation, Docker image smoke tests, the NetBox-backed E2E
matrix, and staged package publication.

## Workflow Map

| Workflow | Trigger | Purpose |
|---|---|---|
| `.github/workflows/ci.yml` | Push, pull request, release, manual dispatch | Runs core checks and the NetBox + Proxmox Docker E2E matrix. |
| `.github/workflows/publish-testpypi.yml` | Version tag, GitHub release, manual dispatch | Publishes immutable package versions through TestPyPI, PyPI release candidates, final PyPI releases, Docker images, and post-publish E2E. |
| `.github/workflows/docker-hub-publish.yml` | Reusable workflow / manual dispatch | Builds and publishes raw, nginx, and granian Docker image variants. |
| `.github/workflows/release-docker-verify.yml` | Release / manual dispatch | Pulls the published Docker image tags and verifies container startup. |
| `.github/workflows/docs.yml` | Docs changes on main / PR | Builds and publishes the MkDocs site. |
| `.github/workflows/nightly-schema-refresh.yml` | Schedule / manual dispatch | Refreshes generated Proxmox schemas and opens a PR when they change. |

## CI Job Flow

```mermaid
flowchart TD
    Push[Push / PR / manual run]
    Core[test\nruff + ty + compile + pytest]
    Py311[test-py311-floor\ncompile + core pytest]
    Free[test-free-threaded\ncontinue-on-error]
    Bind[Docker bind-host smoke\nraw + granian]
    Setup[setup\nbuild E2E matrix]
    BuildNB[build-netbox-image\nonly if registry pull fails]
    E2E[e2e-docker\ntransport x NetBox version matrix]

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

The E2E jobs pull the public NetBox image first. They only download the
source-built NetBox image artifact when that registry pull fails.

## E2E Stack

`ci.yml` starts a real stack and verifies that `proxbox-api` can authenticate,
configure NetBox endpoints, and run sync tests across supported transports.

```mermaid
flowchart LR
    GA[GitHub Actions runner]

    subgraph Stack[Docker network: proxbox-e2e]
        NB[NetBox container\nnetbox-proxbox installed]
        NGINX[Optional HTTPS nginx]
        API[proxbox-api container\nraw, nginx, or granian target]
        PM[Proxmox mock container\nproxmox-sdk:latest]
        PG[(PostgreSQL)]
        RD[(Redis)]
    end

    GA --> NB
    GA --> API
    GA --> PM
    NB --> PG
    NB --> RD
    API -->|NetBox REST| NB
    API -->|Proxmox API| PM
    NGINX --> NB
```

Important E2E rules:

- NetBox readiness waits up to 20 minutes for migrations/search indexing.
- `/api/status/` must be ready before tokens and endpoints are configured.
- Docker-backed Proxmox tests run with the `mock_http` marker.
- The in-process `MockBackend` pass runs separately with the `mock_backend`
  marker.
- Release events run both `dev` and `pypi` `netbox-proxbox` dependency modes;
  normal push/PR CI uses the development mode.

## Release Validation

```mermaid
sequenceDiagram
    participant Tag as Version tag
    participant WF as publish-testpypi.yml
    participant TP as TestPyPI
    participant PY as PyPI
    participant DH as Docker Hub
    participant E2E as NetBox E2E stack

    Tag->>WF: vX.Y.Z or vX.Y.Z.postN
    WF->>WF: Validate pyproject + uv.lock + tag
    WF->>TP: Upload proxbox-api
    WF->>TP: Reinstall exact package on Python 3.11, 3.12, 3.13
    WF->>WF: Run lint, type, compile, import, schema, pytest checks

    Tag->>WF: vX.Y.ZrcN, release event, or publish_target=pypi
    WF->>E2E: Run pre-publish E2E with dev dependencies
    WF->>PY: Upload proxbox-api
    WF->>PY: Reinstall exact package
    WF->>DH: Publish raw, nginx, granian images
    WF->>E2E: Run post-publish E2E with published package + image
```

Package uploads intentionally omit `twine --skip-existing`. If any validation
fails after upload, publish a fixed-forward version: `vX.Y.Z.postN` for TestPyPI
or post-release fixes, and `vX.Y.ZrcN` for PyPI release-candidate retries.
