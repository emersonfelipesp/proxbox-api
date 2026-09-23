"""Contracts for the package-only Gitea publication control."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".gitea/workflows/publish-gitea.yml"
HELPER = REPO_ROOT / "scripts/release_artifacts.py"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_artifacts", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_publication_is_manual_package_only_and_serialized() -> None:
    workflow = _workflow()
    triggers = workflow[True]
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["tag_name"]["required"] is True
    assert inputs["resume_existing"]["default"] is False
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "proxbox-api-package-publication",
        "cancel-in-progress": False,
    }
    text = WORKFLOW.read_text(encoding="utf-8").lower()
    assert "github.com" not in text
    assert "gh release" not in text
    assert "production" not in text
    assert "deploy" not in text
    assert "edgeuno" not in text


def test_jobs_bind_to_canonical_main_and_existing_runner() -> None:
    jobs = _workflow()["jobs"]
    assert set(jobs) == {"validate-version", "build-artifacts", "publish-gitea"}
    for job in jobs.values():
        condition = str(job["if"])
        assert "emersonfelipesp/proxbox-api" in condition
        assert "refs/heads/main" in condition
    assert jobs["validate-version"]["runs-on"] == "mirror-host"
    assert jobs["build-artifacts"]["runs-on"] == "ci-untrusted-python312"
    assert jobs["publish-gitea"]["runs-on"] == "mirror-host"
    assert jobs["build-artifacts"]["needs"] == "validate-version"
    assert jobs["publish-gitea"]["needs"] == ["validate-version", "build-artifacts"]


def test_registry_bytes_are_verified_before_manifest_publication() -> None:
    publish = _workflow()["jobs"]["publish-gitea"]
    names = [step["name"] for step in publish["steps"]]
    preflight = names.index("Preflight immutable package state")
    upload = names.index("Publish to Gitea Package Registry")
    link = names.index("Link package to source repository")
    verify = names.index("Verify package in Gitea registry")
    manifest = names.index("Publish repository-linked release manifest")
    assert preflight < upload < link < verify < manifest
    assert _step(publish, "Publish to Gitea Package Registry")["if"] == (
        "env.ARTIFACT_ACTION == 'upload'"
    )
    upload_run = _step(publish, "Publish to Gitea Package Registry")["run"]
    assert '"${GITHUB_WORKSPACE}/.venv/bin/python" -m twine upload' in upload_run
    verify_run = _step(publish, "Verify package in Gitea registry")["run"]
    assert "verify-registry" in verify_run
    assert "release-manifest.json" in verify_run


def test_candidate_tag_is_bound_across_jobs() -> None:
    jobs = _workflow()["jobs"]
    outputs = jobs["validate-version"]["outputs"]
    assert "source_sha" in outputs
    assert "tag_object" in outputs
    build = jobs["build-artifacts"]
    assert outputs["source_sha"] == "${{ steps.extract.outputs.source_sha }}"
    assert outputs["tag_object"] == "${{ steps.extract.outputs.tag_object }}"
    assert build["env"]["SOURCE_SHA"] == "${{ needs.validate-version.outputs.source_sha }}"
    assert build["env"]["EXPECTED_TAG_OBJECT"] == (
        "${{ needs.validate-version.outputs.tag_object }}"
    )
    checkout = _step(build, "Checkout exact public tag without Node.js")["run"]
    assert 'test "${RESOLVED_SHA}" = "${SOURCE_SHA}"' in checkout
    assert 'test "${TAG_OBJECT}" = "${EXPECTED_TAG_OBJECT}"' in checkout
    assert "git merge-base --is-ancestor" in checkout


def test_historical_tag_uses_helper_from_verified_control_main() -> None:
    jobs = _workflow()["jobs"]
    validate = jobs["validate-version"]
    assert "helper_blob" in validate["outputs"]
    extract = _step(validate, "Extract and validate version")["run"]
    assert "refs/release-policy/control-main:scripts/release_artifacts.py" in extract


def test_publication_materializes_helper_after_artifact_transfer() -> None:
    publish = _workflow()["jobs"]["publish-gitea"]
    names = [step["name"] for step in publish["steps"]]
    materialize_name = "Materialize audited publication helper"
    assert names.index(materialize_name) > names.index("Download exact release artifacts")
    assert names.index(materialize_name) < names.index("Create exact release manifest")
    materialize = _step(publish, materialize_name)["run"]
    assert '/usr/bin/git cat-file blob "${EXPECTED_HELPER_BLOB}"' in materialize
    assert "mktemp -d" in materialize
    assert "git hash-object" in materialize
    assert "RELEASE_ARTIFACTS_HELPER=" in materialize


@pytest.mark.parametrize(
    "name",
    [
        "Create exact release manifest",
        "Preflight immutable package state",
        "Link package to source repository",
        "Verify package in Gitea registry",
        "Publish repository-linked release manifest",
    ],
)
def test_each_helper_use_rechecks_immutable_blob(name: str) -> None:
    publish = _workflow()["jobs"]["publish-gitea"]
    step = _step(publish, name)
    run = step["run"]
    assert step["env"]["EXPECTED_HELPER_BLOB"] == (
        "${{ needs.validate-version.outputs.helper_blob }}"
    )
    assert 'test "$(/usr/bin/git hash-object "${RELEASE_ARTIFACTS_HELPER}")" =' in run
    assert '"${EXPECTED_HELPER_BLOB}"' in run
    assert '/usr/bin/python3 "${RELEASE_ARTIFACTS_HELPER}"' in run
    assert "scripts/release_artifacts.py" not in run


def test_tag_derived_code_is_isolated_from_registry_credentials() -> None:
    jobs = _workflow()["jobs"]
    build = jobs["build-artifacts"]
    publish = jobs["publish-gitea"]
    build_text = json.dumps(build, sort_keys=True)
    publish_text = json.dumps(publish, sort_keys=True)
    assert "secrets.PKG_TOKEN" not in build_text
    assert "TWINE_PASSWORD" not in build_text
    assert "scripts/prepare_offline_release.py" in build_text
    assert "scripts/verify_offline_release_sdist.py" in build_text
    assert "scripts/prepare_offline_release.py" not in publish_text
    assert "scripts/verify_offline_release_sdist.py" not in publish_text
    assert "Checkout exact public tag without Node.js" not in publish_text
    assert "secrets.PKG_TOKEN" in publish_text
    assert _step(build, "Upload exact release artifacts")["uses"].startswith(
        "actions/upload-artifact@"
    )
    assert _step(publish, "Download exact release artifacts")["uses"].startswith(
        "actions/download-artifact@"
    )


def test_untrusted_build_always_uses_verified_pinned_uv() -> None:
    build = _workflow()["jobs"]["build-artifacts"]
    install = _step(build, "Install build tools")["run"]
    assert "command -v uv" not in install
    assert 'UV_SHA256="e490a6464492183c5d4534a5527fb4440f7f2bb2f228162ad7e4afe076dc0224"' in install
    assert 'UV_VERSION="$("${BOOTSTRAP_ROOT}/uv" --version)"' in install
    assert 'echo "UV_BIN=${BOOTSTRAP_ROOT}/uv" >> "${GITHUB_ENV}"' in install
    for name in (
        "Build distributions",
        "Verify the published sdist carries its offline build context",
    ):
        run = _step(build, name)["run"]
        assert '"${UV_BIN}"' in run
        assert not any(line.lstrip().startswith("uv ") for line in run.splitlines())


def test_manifest_is_canonical_and_byte_sensitive(tmp_path: Path) -> None:
    helper = _helper()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "proxbox_api-0.0.23.post1-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "proxbox_api-0.0.23.post1.tar.gz").write_bytes(b"sdist")
    manifest_path = tmp_path / "release-manifest.json"
    source_sha = "a" * 40
    helper.write_manifest(
        dist=dist,
        package="proxbox_api",
        version="0.0.23.post1",
        source_sha=source_sha,
        output=manifest_path,
    )
    helper.verify_manifest(
        manifest_path=manifest_path,
        dist=dist,
        package="proxbox_api",
        version="0.0.23.post1",
        source_sha=source_sha,
    )
    (dist / "proxbox_api-0.0.23.post1-py3-none-any.whl").write_bytes(b"changed")
    with pytest.raises(helper.ReleaseArtifactError, match="does not match"):
        helper.verify_manifest(
            manifest_path=manifest_path,
            dist=dist,
            package="proxbox_api",
            version="0.0.23.post1",
            source_sha=source_sha,
        )


@pytest.mark.parametrize(
    "value",
    ["../escape", "name/part", "name?query", "name#fragment", "", " space"],
)
def test_registry_identity_rejects_hostile_input(value: str) -> None:
    helper = _helper()
    with pytest.raises(helper.ReleaseArtifactError, match="unsafe"):
        helper._quoted(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://registry.example.test",
        "https://user:secret@registry.example.test",
        "https://registry.example.test/path",
        "https://registry.example.test?query=yes",
        "https://registry.example.test#fragment",
        "",
    ],
)
def test_registry_origin_rejects_unsafe_authorities(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    helper = _helper()
    monkeypatch.setenv("GITEA_PACKAGE_REGISTRY_ORIGIN", value)
    with pytest.raises(helper.ReleaseArtifactError, match="HTTPS authority"):
        helper._registry_origin()


def test_registry_origin_accepts_https_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _helper()
    monkeypatch.setenv("GITEA_PACKAGE_REGISTRY_ORIGIN", "https://registry.example.test/")
    assert helper._registry_origin() == "https://registry.example.test"


def test_manifest_rejects_symlink_artifacts(tmp_path: Path) -> None:
    helper = _helper()
    dist = tmp_path / "dist"
    dist.mkdir()
    target = tmp_path / "outside.whl"
    target.write_bytes(b"outside")
    (dist / "proxbox_api-0.0.23.post1-py3-none-any.whl").symlink_to(target)
    (dist / "proxbox_api-0.0.23.post1.tar.gz").write_bytes(b"sdist")
    with pytest.raises(helper.ReleaseArtifactError):
        helper.create_manifest(
            dist=dist,
            package="proxbox_api",
            version="0.0.23.post1",
            source_sha="a" * 40,
        )


def _release_fixture(helper: ModuleType, root: Path) -> tuple[Path, Path, dict]:
    dist = root / "dist"
    dist.mkdir()
    (dist / "proxbox_api-0.0.23.post1-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "proxbox_api-0.0.23.post1.tar.gz").write_bytes(b"sdist")
    manifest_path = root / "release-manifest.json"
    manifest = helper.write_manifest(
        dist=dist,
        package="proxbox_api",
        version="0.0.23.post1",
        source_sha="a" * 40,
        output=manifest_path,
    )
    return dist, manifest_path, manifest


@pytest.mark.parametrize(
    "existing_count, expected_action", [(0, "upload"), (1, "upload"), (2, "reuse")]
)
def test_prepare_upload_recovers_every_partial_artifact_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    existing_count: int,
    expected_action: str,
) -> None:
    helper = _helper()
    dist, manifest_path, manifest = _release_fixture(helper, tmp_path)
    rows = manifest["artifacts"][:existing_count]
    metadata = {
        "type": "pypi",
        "name": "proxbox-api",
        "version": "0.0.23.post1",
        "repository": None,
    }
    monkeypatch.setenv("GITEA_PACKAGE_REGISTRY_ORIGIN", "https://registry.example.test")
    monkeypatch.setattr(helper, "_gitea_package_exists", lambda **_kwargs: True)
    monkeypatch.setattr(
        helper,
        "_request",
        lambda url, **_kwargs: json.dumps(rows if url.endswith("/files") else metadata).encode(),
    )
    local_bytes = {row["name"]: (dist / row["name"]).read_bytes() for row in rows}
    monkeypatch.setattr(
        helper,
        "_download_artifact",
        lambda **kwargs: (kwargs["row"]["name"], local_bytes[kwargs["row"]["name"]]),
    )
    upload_dist = tmp_path / "upload-dist"
    action = helper.prepare_upload(
        owner="emersonfelipesp",
        repository="proxbox-api",
        manifest_path=manifest_path,
        dist=dist,
        upload_dist=upload_dist,
        resume_existing=True,
        token="token",
    )
    assert action == expected_action
    assert {path.name for path in upload_dist.iterdir()} == {
        row["name"] for row in manifest["artifacts"][existing_count:]
    }


def test_prepare_upload_rejects_conflicting_existing_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper = _helper()
    dist, manifest_path, manifest = _release_fixture(helper, tmp_path)
    metadata = {
        "type": "pypi",
        "name": "proxbox-api",
        "version": "0.0.23.post1",
        "repository": None,
    }
    monkeypatch.setenv("GITEA_PACKAGE_REGISTRY_ORIGIN", "https://registry.example.test")
    monkeypatch.setattr(helper, "_gitea_package_exists", lambda **_kwargs: True)
    monkeypatch.setattr(
        helper,
        "_request",
        lambda url, **_kwargs: json.dumps(
            manifest["artifacts"][:1] if url.endswith("/files") else metadata
        ).encode(),
    )
    monkeypatch.setattr(
        helper,
        "_download_artifact",
        lambda **kwargs: (kwargs["row"]["name"], b"conflicting"),
    )
    with pytest.raises(helper.ReleaseArtifactError, match="differ"):
        helper.prepare_upload(
            owner="emersonfelipesp",
            repository="proxbox-api",
            manifest_path=manifest_path,
            dist=dist,
            upload_dist=tmp_path / "upload-dist",
            resume_existing=True,
            token="token",
        )


@pytest.mark.parametrize(
    "repository, allow_missing, succeeds",
    [(None, True, True), (None, False, False), ({"full_name": "other/repo"}, True, False)],
)
def test_package_link_identity_is_exact(
    repository: object, allow_missing: bool, succeeds: bool
) -> None:
    helper = _helper()
    metadata = {
        "type": "pypi",
        "name": "proxbox-api",
        "version": "0.0.23.post1",
        "repository": repository,
    }

    def call() -> None:
        helper._require_package_identity(
            metadata=metadata,
            owner="emersonfelipesp",
            repository="proxbox-api",
            package="proxbox-api",
            version="0.0.23.post1",
            allow_missing_link=allow_missing,
        )

    if succeeds:
        call()
    else:
        with pytest.raises(helper.ReleaseArtifactError, match="repository link"):
            call()
