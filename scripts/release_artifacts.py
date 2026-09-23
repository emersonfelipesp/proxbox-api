#!/usr/bin/env python3
"""Create, verify, and retrieve one immutable Python release artifact set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
SHA_RE = re.compile(r"^[a-f0-9]{40}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _registry_origin() -> str:
    """Return the validated package authority supplied by the trusted workflow."""
    value = os.environ.get("GITEA_PACKAGE_REGISTRY_ORIGIN", "").rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseArtifactError("Package registry origin must be an HTTPS authority")
    return value


class ReleaseArtifactError(ValueError):
    """The artifact violates the immutable release contract."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def canonical_name(value: str) -> str:
    """Return the PEP 503 spelling used by the registry contract."""
    return re.sub(r"[-_.]+", "-", value).lower()


def _record(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or SAFE_NAME_RE.fullmatch(path.name) is None
        or metadata.st_size > MAX_ARTIFACT_BYTES
    ):
        raise ReleaseArtifactError(f"Unsafe release artifact: {path.name!r}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ARTIFACT_BYTES:
                raise ReleaseArtifactError("Release artifact exceeds its size bound")
            digest.update(chunk)
    return {"name": path.name, "sha256": digest.hexdigest(), "size": size}


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def create_manifest(*, dist: Path, package: str, version: str, source_sha: str) -> dict[str, Any]:
    """Describe exactly one wheel and one source distribution."""
    if SHA_RE.fullmatch(source_sha) is None:
        raise ReleaseArtifactError("Source SHA must be canonical lowercase 40-hex")
    # `uv build` drops a `.gitignore` marker into its output directory. It is
    # tooling state rather than a release artifact, and `ls` hides it, so
    # counting it here fails the release with a message that describes a
    # directory the operator cannot see anything wrong with.
    files = sorted(
        path for path in dist.iterdir() if path.is_file() and not path.name.startswith(".")
    )
    wheel = [path for path in files if path.name.endswith(".whl")]
    sdist = [path for path in files if path.name.endswith(".tar.gz")]
    if len(files) != 2 or len(wheel) != 1 or len(sdist) != 1:
        raise ReleaseArtifactError("Release set must contain exactly one wheel and one sdist")
    normalized = canonical_name(package).replace("-", "_")
    expected_prefix = f"{normalized}-{version}"
    if not all(path.name.startswith(expected_prefix) for path in files):
        raise ReleaseArtifactError("Artifact filename does not match package/version")
    return {
        "artifacts": [_record(path) for path in files],
        "package": canonical_name(package),
        "schema": 1,
        "source_sha": source_sha,
        "version": version,
    }


def write_manifest(
    *, dist: Path, package: str, version: str, source_sha: str, output: Path
) -> dict[str, Any]:
    """Create a canonical manifest and return it."""
    manifest = create_manifest(dist=dist, package=package, version=version, source_sha=source_sha)
    output.write_bytes(_manifest_bytes(manifest))
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    """Load an exact-schema canonical manifest."""
    raw = path.read_bytes()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReleaseArtifactError("Manifest exceeds its size bound")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseArtifactError("Manifest is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "artifacts",
        "package",
        "schema",
        "source_sha",
        "version",
    }:
        raise ReleaseArtifactError("Manifest schema is not exact")
    if value.get("schema") != 1 or _manifest_bytes(value) != raw:
        raise ReleaseArtifactError("Manifest is not canonical schema 1 JSON")
    return value


def verify_manifest(
    *, manifest_path: Path, dist: Path, package: str, version: str, source_sha: str
) -> dict[str, Any]:
    """Require a manifest to match independently hashed local files."""
    expected = create_manifest(dist=dist, package=package, version=version, source_sha=source_sha)
    actual = load_manifest(manifest_path)
    if actual != expected:
        raise ReleaseArtifactError("Manifest does not match the local artifact bytes")
    return actual


def manifest_sha256(manifest: dict[str, Any]) -> str:
    """Return the digest operators place in final promotion evidence."""
    return hashlib.sha256(_manifest_bytes(manifest)).hexdigest()


def release_manifest_package(manifest: dict[str, Any]) -> str:
    """Return the immutable generic-package identity for build provenance."""
    return f"{manifest['package']}-release-manifest"


def _request(
    url: str,
    *,
    token: str,
    maximum: int,
    method: str = "GET",
    payload: bytes | None = None,
) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    registry = urllib.parse.urlsplit(_registry_origin())
    if parsed.scheme != "https" or parsed.netloc != registry.netloc:
        raise ReleaseArtifactError("Only the configured HTTPS Gitea origin is allowed")
    headers = {"Accept": "application/json", "User-Agent": "release-artifacts/1"}
    if token:
        headers["Authorization"] = f"token {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise ReleaseArtifactError(f"Registry returned HTTP {response.status}")
            content = response.read(maximum + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ReleaseArtifactError("Registry request failed") from exc
    if len(content) > maximum:
        raise ReleaseArtifactError("Registry response exceeds its size bound")
    return content


def _quoted(value: str) -> str:
    if SAFE_NAME_RE.fullmatch(value) is None:
        raise ReleaseArtifactError("Registry identity contains unsafe characters")
    return urllib.parse.quote(value, safe="")


def _require_manifest_metadata(
    *,
    metadata: object,
    owner: str,
    repository: str,
    package: str,
    version: str,
) -> None:
    if not isinstance(metadata, dict):
        raise ReleaseArtifactError("Gitea release manifest metadata is invalid")
    repo = metadata.get("repository")
    identity = (
        metadata.get("type"),
        metadata.get("name"),
        metadata.get("version"),
        repo.get("full_name") if isinstance(repo, dict) else None,
    )
    if identity != ("generic", package, version, f"{owner}/{repository}"):
        raise ReleaseArtifactError("Gitea release manifest identity is invalid")


def _require_single_manifest_file(files: object) -> tuple[int, str]:
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise ReleaseArtifactError("Gitea release manifest inventory is invalid")
    row = files[0]
    if row.get("name") != "release-manifest.json":
        raise ReleaseArtifactError("Gitea release manifest inventory is invalid")
    size, digest = row.get("size"), row.get("sha256")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_RESPONSE_BYTES:
        raise ReleaseArtifactError("Gitea release manifest inventory is invalid")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest.lower()) is None:
        raise ReleaseArtifactError("Gitea release manifest inventory is invalid")
    return size, digest.lower()


def fetch_gitea_manifest(
    *, owner: str, repository: str, package: str, version: str, token: str = ""
) -> dict[str, Any]:
    """Fetch the original repository-linked manifest created by the builder."""
    manifest_package = f"{canonical_name(package)}-release-manifest"
    base = (
        f"{_registry_origin()}/api/v1/packages/"
        f"{_quoted(owner)}/generic/{_quoted(manifest_package)}/{_quoted(version)}"
    )
    metadata = json.loads(_request(base, token=token, maximum=MAX_RESPONSE_BYTES))
    files = json.loads(_request(f"{base}/files", token=token, maximum=MAX_RESPONSE_BYTES))
    _require_manifest_metadata(
        metadata=metadata,
        owner=owner,
        repository=repository,
        package=manifest_package,
        version=version,
    )
    size, digest = _require_single_manifest_file(files)
    url = (
        f"{_registry_origin()}/api/packages/"
        f"{_quoted(owner)}/generic/{_quoted(manifest_package)}/{_quoted(version)}/"
        "release-manifest.json"
    )
    raw = _request(url, token=token, maximum=size)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ReleaseArtifactError("Downloaded release manifest differs from Gitea inventory")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseArtifactError("Gitea release manifest is not valid JSON") from exc
    if not isinstance(value, dict) or _manifest_bytes(value) != raw:
        raise ReleaseArtifactError("Gitea release manifest is not canonical JSON")
    return cast(dict[str, Any], value)


def publish_gitea_manifest(
    *,
    owner: str,
    repository: str,
    manifest: dict[str, Any],
    token: str,
) -> dict[str, Any]:
    """Publish the builder's original manifest and verify its immutable bytes."""
    if not token:
        raise ReleaseArtifactError("Gitea package token is unavailable")
    package = release_manifest_package(manifest)
    version = str(manifest["version"])
    raw = _manifest_bytes(manifest)
    upload_url = (
        f"{_registry_origin()}/api/packages/"
        f"{_quoted(owner)}/generic/{_quoted(package)}/{_quoted(version)}/release-manifest.json"
    )
    try:
        _request(
            upload_url,
            token=token,
            maximum=MAX_RESPONSE_BYTES,
            method="PUT",
            payload=raw,
        )
    except ReleaseArtifactError:
        # A retry after a completed upload may receive a conflict. The exact
        # authenticated read-back below accepts only identical immutable bytes.
        pass
    link_url = (
        f"{_registry_origin()}/api/v1/packages/"
        f"{_quoted(owner)}/generic/{_quoted(package)}/-/link/{_quoted(repository)}"
    )
    try:
        _request(
            link_url,
            token=token,
            maximum=MAX_RESPONSE_BYTES,
            method="POST",
            payload=b"",
        )
    except ReleaseArtifactError:
        # Gitea may apply the repository link and still answer HTTP 400 when
        # the package was linked automatically or a retry observes the link.
        # The authenticated read-back below is the authority: it requires the
        # exact owner, repository, package, version, file, size, and digest.
        pass
    verified = fetch_gitea_manifest(
        owner=owner,
        repository=repository,
        package=str(manifest["package"]),
        version=version,
        token=token,
    )
    if verified != manifest:
        raise ReleaseArtifactError("Published release manifest changed")
    return verified


def link_gitea_package(*, owner: str, repository: str, package: str, token: str) -> None:
    """Associate the PyPI package with its source repository; verification is authoritative."""
    if not token:
        raise ReleaseArtifactError("Gitea package token is unavailable")
    url = (
        f"{_registry_origin()}/api/v1/packages/"
        f"{_quoted(owner)}/pypi/{_quoted(canonical_name(package))}/-/link/"
        f"{_quoted(repository)}"
    )
    try:
        _request(url, token=token, maximum=MAX_RESPONSE_BYTES, method="POST", payload=b"")
    except ReleaseArtifactError:
        # Existing links may return a conflict. The exact metadata verification
        # that follows is the authority and still fails if the link is absent.
        return


def _require_package_identity(
    *,
    metadata: object,
    owner: str,
    repository: str,
    package: str,
    version: str,
    allow_missing_link: bool = False,
) -> None:
    if not isinstance(metadata, dict):
        raise ReleaseArtifactError("Gitea package identity or repository link is invalid")
    repo = metadata.get("repository")
    actual_repo = repo.get("full_name") if isinstance(repo, dict) else None
    identity = (
        metadata.get("type"),
        canonical_name(str(metadata.get("name", ""))),
        metadata.get("version"),
    )
    if identity != ("pypi", package, version):
        raise ReleaseArtifactError("Gitea package identity or repository link is invalid")
    if actual_repo != f"{owner}/{repository}" and not (allow_missing_link and actual_repo is None):
        raise ReleaseArtifactError("Gitea package identity or repository link is invalid")


def _artifact_inventory(files: object, *, require_complete: bool = True) -> list[dict[str, object]]:
    if not isinstance(files, list) or len(files) > 2 or (require_complete and len(files) != 2):
        raise ReleaseArtifactError("Gitea release file inventory has an invalid size")
    inventory: list[dict[str, object]] = []
    names: set[str] = set()
    for row in files:
        inventory.append(_artifact_inventory_row(row, names))
    return inventory


def _artifact_inventory_row(row: object, names: set[str]) -> dict[str, object]:
    if not isinstance(row, dict):
        raise ReleaseArtifactError("Gitea file inventory is malformed")
    name, size, digest = row.get("name"), row.get("size"), row.get("sha256")
    if not isinstance(name, str) or SAFE_NAME_RE.fullmatch(name) is None or name in names:
        raise ReleaseArtifactError("Gitea file inventory entry is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_ARTIFACT_BYTES:
        raise ReleaseArtifactError("Gitea file inventory entry is invalid")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest.lower()) is None:
        raise ReleaseArtifactError("Gitea file inventory entry is invalid")
    names.add(name)
    return {"name": name, "size": size, "sha256": digest.lower()}


def _download_artifact(
    *, owner: str, package: str, version: str, row: dict[str, object], token: str
) -> tuple[str, bytes]:
    name = cast(str, row["name"])
    size = cast(int, row["size"])
    digest = cast(str, row["sha256"])
    download = (
        f"{_registry_origin()}/api/packages/"
        f"{_quoted(owner)}/pypi/files/{_quoted(package)}/{_quoted(version)}/{_quoted(name)}"
    )
    content = _request(download, token=token, maximum=size)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise ReleaseArtifactError("Downloaded artifact differs from Gitea inventory")
    return name, content


def _download_gitea_artifacts(
    *,
    owner: str,
    repository: str,
    package: str,
    version: str,
    source_sha: str,
    dist: Path,
    expected_manifest: dict[str, Any],
    token: str = "",
) -> dict[str, Any]:
    """Download the exact repository-linked artifact set and verify its bytes."""
    if not token:
        raise ReleaseArtifactError("Gitea package token is unavailable")
    package = canonical_name(package)
    if (
        expected_manifest.get("package") != package
        or expected_manifest.get("version") != version
        or expected_manifest.get("source_sha") != source_sha
    ):
        raise ReleaseArtifactError("Gitea release manifest does not match the protected tag")
    base = (
        f"{_registry_origin()}/api/v1/packages/"
        f"{_quoted(owner)}/pypi/{_quoted(package)}/{_quoted(version)}"
    )
    metadata = json.loads(_request(base, token=token, maximum=MAX_RESPONSE_BYTES))
    files = json.loads(_request(f"{base}/files", token=token, maximum=MAX_RESPONSE_BYTES))
    _require_package_identity(
        metadata=metadata,
        owner=owner,
        repository=repository,
        package=package,
        version=version,
    )
    inventory = _artifact_inventory(files)
    dist.mkdir(parents=True, exist_ok=True)
    for row in inventory:
        name, content = _download_artifact(
            owner=owner,
            package=package,
            version=version,
            row=row,
            token=token,
        )
        (dist / name).write_bytes(content)
    downloaded_manifest = create_manifest(
        dist=dist, package=package, version=version, source_sha=source_sha
    )
    if downloaded_manifest != expected_manifest:
        raise ReleaseArtifactError("Gitea artifacts differ from the original build manifest")
    return expected_manifest


def fetch_gitea_artifacts(
    *,
    owner: str,
    repository: str,
    package: str,
    version: str,
    source_sha: str,
    dist: Path,
    token: str = "",
) -> dict[str, Any]:
    """Download artifacts and bind them to the published immutable manifest."""
    published_manifest = fetch_gitea_manifest(
        owner=owner,
        repository=repository,
        package=package,
        version=version,
        token=token,
    )
    return _download_gitea_artifacts(
        owner=owner,
        repository=repository,
        package=package,
        version=version,
        source_sha=source_sha,
        dist=dist,
        expected_manifest=published_manifest,
        token=token,
    )


def verify_gitea_artifacts(
    *,
    owner: str,
    repository: str,
    manifest_path: Path,
    dist: Path,
    token: str,
) -> dict[str, Any]:
    """Verify registry bytes against the unpublished local manifest."""
    manifest = load_manifest(manifest_path)
    return _download_gitea_artifacts(
        owner=owner,
        repository=repository,
        package=str(manifest["package"]),
        version=str(manifest["version"]),
        source_sha=str(manifest["source_sha"]),
        dist=dist,
        expected_manifest=manifest,
        token=token,
    )


def _gitea_package_exists(*, owner: str, package: str, version: str, token: str) -> bool:
    """Return package presence while failing closed on every response except 404."""
    if not token:
        raise ReleaseArtifactError("Gitea package token is unavailable")
    url = (
        f"{_registry_origin()}/api/v1/packages/"
        f"{_quoted(owner)}/pypi/{_quoted(canonical_name(package))}/{_quoted(version)}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"token {token}",
            "User-Agent": "release-artifacts/1",
        },
        method="GET",
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            response.read(MAX_RESPONSE_BYTES + 1)
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise ReleaseArtifactError(f"Registry returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ReleaseArtifactError("Registry request failed") from exc


def prepare_upload(
    *,
    owner: str,
    repository: str,
    manifest_path: Path,
    dist: Path,
    upload_dist: Path,
    resume_existing: bool,
    token: str,
) -> str:
    """Stage missing files or prove that an explicitly resumed version is identical."""
    manifest = load_manifest(manifest_path)
    package = str(manifest["package"])
    version = str(manifest["version"])
    source_sha = str(manifest["source_sha"])
    verify_manifest(
        manifest_path=manifest_path,
        dist=dist,
        package=package,
        version=version,
        source_sha=source_sha,
    )
    if upload_dist.exists() and any(upload_dist.iterdir()):
        raise ReleaseArtifactError("Upload staging directory must be empty")
    upload_dist.mkdir(parents=True, exist_ok=True)
    expected = {str(row["name"]): row for row in manifest["artifacts"]}
    if not _gitea_package_exists(owner=owner, package=package, version=version, token=token):
        for name in expected:
            shutil.copyfile(dist / name, upload_dist / name)
        return "upload"
    if not resume_existing:
        raise ReleaseArtifactError(
            "Package version already exists; explicit resume mode is required"
        )
    base = (
        f"{_registry_origin()}/api/v1/packages/"
        f"{_quoted(owner)}/pypi/{_quoted(package)}/{_quoted(version)}"
    )
    metadata = json.loads(_request(base, token=token, maximum=MAX_RESPONSE_BYTES))
    files = json.loads(_request(f"{base}/files", token=token, maximum=MAX_RESPONSE_BYTES))
    _require_package_identity(
        metadata=metadata,
        owner=owner,
        repository=repository,
        package=package,
        version=version,
        allow_missing_link=True,
    )
    existing: set[str] = set()
    for row in _artifact_inventory(files, require_complete=False):
        name, content = _download_artifact(
            owner=owner, package=package, version=version, row=row, token=token
        )
        expected_row = expected.get(name)
        if expected_row is None or len(content) != expected_row["size"]:
            raise ReleaseArtifactError("Existing registry artifacts differ from the manifest")
        if hashlib.sha256(content).hexdigest() != expected_row["sha256"]:
            raise ReleaseArtifactError("Existing registry artifacts differ from the manifest")
        existing.add(name)
    for name in expected.keys() - existing:
        shutil.copyfile(dist / name, upload_dist / name)
    return "reuse" if existing == expected.keys() else "upload"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and verify immutable release artifacts")
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest")
    manifest.add_argument("--dist", type=Path, required=True)
    manifest.add_argument("--package", required=True)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--source-sha", required=True)
    manifest.add_argument("--manifest", type=Path, required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--dist", type=Path, required=True)
    verify.add_argument("--package", required=True)
    verify.add_argument("--version", required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--manifest", type=Path, required=True)

    publish = commands.add_parser("publish-manifest")
    publish.add_argument("--owner", required=True)
    publish.add_argument("--repository", required=True)
    publish.add_argument("--manifest", type=Path, required=True)

    link = commands.add_parser("link-package")
    link.add_argument("--owner", required=True)
    link.add_argument("--repository", required=True)
    link.add_argument("--package", required=True)

    prepare = commands.add_parser("prepare-upload")
    prepare.add_argument("--owner", required=True)
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--dist", type=Path, required=True)
    prepare.add_argument("--upload-dist", type=Path, required=True)
    prepare.add_argument("--resume-existing", action="store_true")

    registry = commands.add_parser("verify-registry")
    registry.add_argument("--owner", required=True)
    registry.add_argument("--repository", required=True)
    registry.add_argument("--manifest", type=Path, required=True)
    registry.add_argument("--dist", type=Path, required=True)

    fetch = commands.add_parser("fetch-gitea")
    fetch.add_argument("--owner", required=True)
    fetch.add_argument("--repository", required=True)
    fetch.add_argument("--package", required=True)
    fetch.add_argument("--version", required=True)
    fetch.add_argument("--source-sha", required=True)
    fetch.add_argument("--dist", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    token = os.environ.get("GITEA_PACKAGE_TOKEN", "")
    if args.command == "manifest":
        write_manifest(
            dist=args.dist,
            package=args.package,
            version=args.version,
            source_sha=args.source_sha,
            output=args.manifest,
        )
    elif args.command == "verify":
        verify_manifest(
            manifest_path=args.manifest,
            dist=args.dist,
            package=args.package,
            version=args.version,
            source_sha=args.source_sha,
        )
    elif args.command == "publish-manifest":
        publish_gitea_manifest(
            owner=args.owner,
            repository=args.repository,
            manifest=load_manifest(args.manifest),
            token=token,
        )
    elif args.command == "link-package":
        link_gitea_package(
            owner=args.owner,
            repository=args.repository,
            package=args.package,
            token=token,
        )
    elif args.command == "prepare-upload":
        print(
            prepare_upload(
                owner=args.owner,
                repository=args.repository,
                manifest_path=args.manifest,
                dist=args.dist,
                upload_dist=args.upload_dist,
                resume_existing=args.resume_existing,
                token=token,
            )
        )
    elif args.command == "verify-registry":
        verify_gitea_artifacts(
            owner=args.owner,
            repository=args.repository,
            manifest_path=args.manifest,
            dist=args.dist,
            token=token,
        )
    elif args.command == "fetch-gitea":
        fetch_gitea_artifacts(
            owner=args.owner,
            repository=args.repository,
            package=args.package,
            version=args.version,
            source_sha=args.source_sha,
            dist=args.dist,
            token=token,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseArtifactError as exc:
        raise SystemExit(str(exc)) from exc
