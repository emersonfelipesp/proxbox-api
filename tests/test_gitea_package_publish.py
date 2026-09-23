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
    assert set(jobs) == {"validate-version", "publish-gitea"}
    for job in jobs.values():
        assert job["runs-on"] == "mirror-host"
        condition = str(job["if"])
        assert "emersonfelipesp/proxbox-api" in condition
        assert "refs/heads/main" in condition
    assert jobs["publish-gitea"]["needs"] == "validate-version"


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
    assert ".venv/bin/python -m twine upload --non-interactive upload-dist/*" in upload_run
    verify_run = _step(publish, "Verify package in Gitea registry")["run"]
    assert "verify-registry" in verify_run
    assert "release-manifest.json" in verify_run


def test_candidate_tag_is_bound_across_jobs() -> None:
    jobs = _workflow()["jobs"]
    outputs = jobs["validate-version"]["outputs"]
    assert "source_sha" in outputs
    assert "tag_object" in outputs
    publish = jobs["publish-gitea"]
    assert outputs["source_sha"] == "${{ steps.extract.outputs.source_sha }}"
    assert outputs["tag_object"] == "${{ steps.extract.outputs.tag_object }}"
    assert publish["env"]["SOURCE_SHA"] == "${{ needs.validate-version.outputs.source_sha }}"
    assert publish["env"]["EXPECTED_TAG_OBJECT"] == (
        "${{ needs.validate-version.outputs.tag_object }}"
    )
    checkout = _step(publish, "Checkout exact public tag without Node.js")["run"]
    assert 'test "${RESOLVED_SHA}" = "${SOURCE_SHA}"' in checkout
    assert 'test "${TAG_OBJECT}" = "${EXPECTED_TAG_OBJECT}"' in checkout
    assert "git merge-base --is-ancestor" in checkout


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
