# Standalone mock package-first release

The public package workflow builds `proxmox-mock-api` once, publishes the same wheel and source archive to TestPyPI, validates the uploaded wheel from a clean environment outside the checkout, runs a detached copy of the standalone behavioral tests against that installation, and then publishes the original artifacts to PyPI. Existing-version collisions fail closed on both package indexes and require a new version. A published release never builds or publishes a container image.

Container publication is a separate manual operation in `.github/workflows/publish-proxmox-mock-image.yml`. Configure its `standalone-mock-production` GitHub environment with required reviewers before enabling it. The workflow accepts one exact `version` input, requires that value to match both `proxmox-mock/pyproject.toml` and `release-manifest.toml`, and publishes only the versioned multi-platform tag.
Each platform build records the digest returned by the pinned build action. The
final index consumes those current-run `repository@sha256:` references, never
the mutable architecture tags.

## Qualification inputs

`release-manifest.toml` is intentionally committed with explicit placeholders. It fails closed until a release operator replaces every placeholder using independently verified registry metadata and qualification output. Never invent a digest or copy one from an unverified console log.

For both `linux/amd64` and `linux/arm64`, record:

- the architecture-specific digest for the Debian Bookworm Python base;
- the architecture-specific digest for the exact uv tool image;
- the digest of the dependency wheel inventory downloaded from the committed `uv.lock` export;
- the installed-provenance digest produced after the qualified installation.

Also record the wheel SHA256 and the SHA256 of `requirements.lock`, which must be produced by the workflow's frozen `uv export --no-emit-project` command. The validator rejects placeholders, malformed digests, missing or extra platforms, mutable image references, and version drift.

The release Dockerfile consumes only the qualified context. It creates an empty virtual environment, installs dependency wheels offline with hashes, installs the qualified application wheel offline with its manifest hash, runs `uv pip check`, verifies installed provenance, and performs an import smoke test. It never copies application source into the image.
It also refuses a build when either selected `FROM` image differs from the
current platform's immutable manifest reference. The validated manifest supplies
the application wheel filename, so a version bump does not require a Dockerfile
edit.

## Development image

`Dockerfile.dev` remains the source-build recipe for local and E2E CI use. It uses the committed standalone lock with an exact uv version but is not a publication input. `.github/workflows/ci.yml` names it explicitly, so the release Dockerfile cannot silently fall back to a source build when its qualified context is absent.
