"""Executable contracts for package-first staging and production deployment."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".gitea/workflows/deploy-production.yml"
HELPER = REPO_ROOT / "scripts/release_artifacts.py"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_artifacts_deploy", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _production_step(name: str) -> dict:
    return next(
        step for step in _workflow()["jobs"]["production"]["steps"] if step.get("name") == name
    )


def _staging_step(name: str) -> dict:
    return next(
        step for step in _workflow()["jobs"]["staging"]["steps"] if step.get("name") == name
    )


def _run(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_staging_and_production_are_separate_scopes() -> None:
    workflow = _workflow()
    triggers = workflow.get("on") or workflow[True]
    assert triggers["push"]["branches"] == ["develop"]
    assert workflow["jobs"]["staging"]["if"] == (
        "${{ github.event_name == 'push' && github.ref == 'refs/heads/develop' }}"
    )
    assert workflow["jobs"]["production"]["if"] == (
        "${{ github.event_name == 'workflow_dispatch' }}"
    )
    validate = _production_step("Validate source-aware production request")["run"]
    assert 'test "$WORKFLOW_REF" = refs/heads/main' in validate
    assert "deploy_request_id" in triggers["workflow_dispatch"]["inputs"]
    assert "deploy_request_sha256" in triggers["workflow_dispatch"]["inputs"]


def test_staging_uses_the_exact_validated_control_binary() -> None:
    script = _staging_step("Deploy reviewed develop commit")["run"]
    assert "/usr/bin/find /opt -xdev -mindepth 4 -maxdepth 4" in script
    assert "-path '*/deploy/bin/deploy-app'" in script
    assert "-user root ! -perm /022" in script
    assert '-print0 > "$deploy_app_candidates"' in script
    assert "mapfile -d '' -t deploy_apps < \"$deploy_app_candidates\"" in script
    assert "< <(/usr/bin/find" not in script
    assert "-print\n" not in script
    assert 'test "${#deploy_apps[@]}" = 1' in script
    assert 'deploy_app="${deploy_apps[0]}"' in script
    assert 'test -x "$deploy_app"' in script
    assert "/usr/bin/stat -c '%a'" in script
    assert "8#$deploy_app_mode & 8#022" in script
    assert '"$deploy_app" proxbox-api-staging "$GITHUB_SHA"' in script
    assert "-name deploy-app" not in script


def test_package_is_default_and_main_is_an_explicit_override() -> None:
    workflow = _workflow()
    triggers = workflow.get("on") or workflow[True]
    deploy_source = triggers["workflow_dispatch"]["inputs"]["deploy_source"]
    assert deploy_source["default"] == "latest_package"
    assert deploy_source["options"] == ["latest_package", "main_branch"]
    validate = _production_step("Validate source-aware production request")["run"]
    assert "latest_package)" in validate
    assert "main_branch)" in validate
    assert 'test -z "$PACKAGE_VERSION"' in validate


def test_manual_develop_dispatch_fails_before_capability_use(tmp_path: Path) -> None:
    validate = _production_step("Validate source-aware production request")["run"]
    base = {
        "DEPLOY_SOURCE": "latest_package",
        "PACKAGE_VERSION": "1.2.3",
        "WORKFLOW_SHA": "a" * 40,
        "DEPLOY_REQUEST_ID": "b" * 32,
        "DEPLOY_REQUEST_SHA256": "c" * 64,
        "GITHUB_RUN_ATTEMPT": "1",
        "DEPLOY_CONTROL_URL": "http://127.0.0.1:16001",
    }
    rejected = _run(validate, {**base, "WORKFLOW_REF": "refs/heads/develop"}, tmp_path)
    assert rejected.returncode != 0
    accepted = _run(validate, {**base, "WORKFLOW_REF": "refs/heads/main"}, tmp_path)
    assert accepted.returncode == 0, accepted.stderr


def test_preflight_precedes_single_use_claim_and_host_mutation() -> None:
    steps = _workflow()["jobs"]["production"]["steps"]
    names = [step.get("name") for step in steps]
    read = names.index("Read the deployment authorization")
    resolve = names.index("Resolve the deployed source SHA")
    gate = names.index("Require a green CI status for the deployed SHA")
    bind = names.index("Bind exact package artifacts before deployment")
    claim = names.index("Claim the deployment authorization for this exact run")
    package_deploy = names.index("Deploy exact Gitea package")
    main_deploy = names.index("Deploy the canonical main commit the request authorizes")
    assert read < resolve < gate < bind < claim < min(package_deploy, main_deploy)
    assert claim == min(package_deploy, main_deploy) - 1
    assert "/verify" in steps[read]["run"] and "/claim" not in steps[read]["run"]
    assert "/claim" in steps[claim]["run"]
    assert steps[gate]["env"]["CI_GATE_TIMEOUT_SECONDS"] == "0"


def test_package_binding_covers_the_exact_authorized_identity() -> None:
    script = _production_step("Bind exact package artifacts before deployment")["run"]
    expected_literals = (
        '"owner": "emersonfelipesp"',
        '"repository": "proxbox-api"',
        '"target_id": 6',
        '"package_name": "proxbox-api"',
        '"package_type": "pypi"',
        '"release_manifest_package": "proxbox-api-release-manifest"',
        '"workflow_id": "deploy-production.yml"',
        '"workflow_ref": "main"',
        '"package_source_sha": sys.argv[2]',
    )
    for literal in expected_literals:
        assert literal in script
    assert "release_manifest_sha256" in script
    assert 'request["artifacts"]' in script
    assert 'manifest.get("source_sha")' in script
    assert "refs/remotes/gitea/release-main" in script


def test_failed_host_deploy_cannot_write_completion_marker(tmp_path: Path) -> None:
    step = _production_step("Deploy exact Gitea package")
    stub = tmp_path / "deploy-app-package"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    stub.chmod(0o700)
    github_env = tmp_path / "github-env"
    github_env.touch()
    script = step["run"].replace(
        "/usr/bin/find /opt -xdev -type f -name deploy-app-package -user root ! -perm /022 -print",
        f"printf '%s\\n' '{stub}'",
    )
    result = _run(
        script,
        {
            "PACKAGE_VERSION": "1.2.3",
            "DEPLOY_REQUEST_ID": "a" * 32,
            "PROOF_PATH": str(tmp_path / "proof.json"),
            "DEPLOY_REQUEST_SHA256": "b" * 64,
            "GITHUB_RUN_ID": "42",
            "GITHUB_ENV": str(github_env),
        },
        tmp_path,
    )
    assert result.returncode != 0
    assert github_env.read_text(encoding="utf-8") == ""


def test_claimed_proof_cleanup_is_unconditional_and_executed(tmp_path: Path) -> None:
    step = _production_step("Destroy the claimed proof")
    assert step["if"] == "always()"
    runner_temp = tmp_path / "runner"
    proof_root = runner_temp / "deploy-proof-42-1"
    proof_root.mkdir(parents=True)
    (proof_root / "claimed-proof.json").write_text("{}", encoding="utf-8")
    environment = {
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    result = _run(step["run"], environment, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not proof_root.exists()

    proof_root.symlink_to(tmp_path / "missing")
    dangling = _run(step["run"], environment, tmp_path)
    assert dangling.returncode != 0


def _manifest(helper: ModuleType, tmp_path: Path) -> dict:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "proxbox_api-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "proxbox_api-1.2.3.tar.gz").write_bytes(b"sdist")
    return helper.create_manifest(
        dist=dist, package="proxbox_api", version="1.2.3", source_sha="a" * 40
    )


def test_signed_receipt_is_validated_and_tampering_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _helper()
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(
        ["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private_key)],
        check=True,
        capture_output=True,
    )
    public_der = subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
            "-outform",
            "PEM",
        ],
        check=True,
        capture_output=True,
    )
    del public_der
    der = subprocess.run(
        ["/usr/bin/openssl", "pkey", "-pubin", "-in", str(public_key), "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    key_digest = hashlib.sha256(der).hexdigest()
    monkeypatch.setattr(helper, "RECEIPT_PUBLIC_KEY", public_key)
    monkeypatch.setattr(helper, "RECEIPT_PUBLIC_KEY_SHA256", key_digest)
    manifest = _manifest(helper, tmp_path)
    receipt_namespace = "control"
    evidence = {
        "artifacts": manifest["artifacts"],
        "deploy_source": "latest_package",
        "deployment_generation": "b" * 64,
        "deployment_run_id": 42,
        "deployment_status": "success",
        "environment": "production",
        "manifest_sha256": helper.manifest_sha256(manifest),
        f"{receipt_namespace}_request_id": "c" * 32,
        f"{receipt_namespace}_request_sha256": "d" * 64,
        f"{receipt_namespace}_workflow_sha": "e" * 40,
        "observed_runtime_identity": "proxbox_api==1.2.3@sha256:" + "f" * 64,
        "package": "proxbox-api",
        "repository": "emersonfelipesp/proxbox-api",
        "schema": 2,
        "signing_key_sha256": key_digest,
        "source_sha": "a" * 40,
        "target": "proxbox-api",
        "version": "1.2.3",
    }
    payload = tmp_path / "payload.json"
    payload.write_bytes(helper._manifest_bytes(evidence))
    signature = tmp_path / "signature.bin"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkeyutl",
            "-sign",
            "-rawin",
            "-inkey",
            str(private_key),
            "-in",
            str(payload),
            "-out",
            str(signature),
        ],
        check=True,
        capture_output=True,
    )
    evidence["signature"] = base64.b64encode(signature.read_bytes()).decode("ascii")
    identity = {
        "run_id": 42,
        "request_id": "c" * 32,
        "request_sha256": "d" * 64,
        "workflow_sha": "e" * 40,
    }
    assert (
        helper.validate_release_attestation(
            evidence=evidence,
            manifest=manifest,
            repository="emersonfelipesp/proxbox-api",
            **identity,
        )
        == evidence
    )
    altered = {**evidence, "deployment_generation": "0" * 64}
    with pytest.raises(helper.ReleaseArtifactError, match="signature is invalid"):
        helper.validate_release_attestation(
            evidence=altered,
            manifest=manifest,
            repository="emersonfelipesp/proxbox-api",
            **identity,
        )
    replay_cases = (
        {**identity, "run_id": 43},
        {**identity, "request_id": "0" * 32},
        {**identity, "request_sha256": "0" * 64},
        {**identity, "workflow_sha": "0" * 40},
    )
    for replay in replay_cases:
        with pytest.raises(helper.ReleaseArtifactError, match="another request or run"):
            helper.validate_release_attestation(
                evidence=evidence,
                manifest=manifest,
                repository="emersonfelipesp/proxbox-api",
                **replay,
            )


def test_pinned_receipt_public_key_matches_the_trusted_digest() -> None:
    helper = _helper()
    der = subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-pubin",
            "-in",
            str(helper.RECEIPT_PUBLIC_KEY),
            "-outform",
            "DER",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert hashlib.sha256(der).hexdigest() == helper.RECEIPT_PUBLIC_KEY_SHA256


def test_receipt_key_is_visible_to_the_clean_index() -> None:
    helper = _helper()
    relative = Path(".gitea/deploy-receipt-public.pem")
    subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(relative)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    stage_row = subprocess.run(
        ["git", "ls-files", "--stage", "--", str(relative)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    metadata, indexed_path = stage_row.split("\t", 1)
    mode, object_id, stage = metadata.split()
    assert (mode, stage, indexed_path) == ("100644", "0", str(relative))
    indexed_pem = subprocess.run(
        ["git", "cat-file", "blob", object_id],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    indexed_der = subprocess.run(
        ["/usr/bin/openssl", "pkey", "-pubin", "-inform", "PEM", "-outform", "DER"],
        input=indexed_pem,
        check=True,
        capture_output=True,
    ).stdout
    assert hashlib.sha256(indexed_der).hexdigest() == helper.RECEIPT_PUBLIC_KEY_SHA256


def _attestation_publish_identity() -> dict[str, object]:
    return {
        "run_id": 42,
        "request_id": "c" * 32,
        "request_sha256": "d" * 64,
        "workflow_sha": "e" * 40,
    }


def test_existing_attestation_resumes_only_after_authenticated_exact_byte_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _helper()
    monkeypatch.setenv("GITEA_PACKAGE_REGISTRY_ORIGIN", "https://registry.example.test")
    manifest = {"package": "proxbox-api", "version": "1.2.3"}
    evidence = {"control_request_id": "c" * 32, "deployment_status": "success"}
    expected = helper._manifest_bytes(evidence)
    calls: list[tuple[str, str]] = []

    def request(url: str, *, token: str, method: str = "GET", **_kwargs: object) -> bytes:
        assert token == "registry-token"
        calls.append((method, url))
        if method == "PUT":
            raise helper.ReleaseArtifactError("already exists")
        if url.endswith("/completion.json"):
            return expected
        return json.dumps(
            {
                "type": "generic",
                "name": "proxbox-api-control-attestation",
                "version": "1.2.3",
                "repository": {"full_name": "emersonfelipesp/proxbox-api"},
            }
        ).encode()

    monkeypatch.setattr(helper, "_request", request)
    monkeypatch.setattr(helper, "validate_release_attestation", lambda **_kwargs: evidence)
    assert (
        helper.publish_gitea_attestation(
            owner="emersonfelipesp",
            repository="proxbox-api",
            manifest=manifest,
            evidence=evidence,
            token="registry-token",
            **_attestation_publish_identity(),
        )
        == evidence
    )
    assert [method for method, _url in calls] == ["PUT", "GET", "GET"]


def test_attestation_partial_failure_is_idempotently_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _helper()
    monkeypatch.setenv("GITEA_PACKAGE_REGISTRY_ORIGIN", "https://registry.example.test")
    manifest = {"package": "proxbox-api", "version": "1.2.3"}
    evidence = {"control_request_id": "c" * 32, "deployment_status": "success"}
    expected = helper._manifest_bytes(evidence)
    upload_attempts = 0

    def request(url: str, *, token: str, method: str = "GET", **_kwargs: object) -> bytes:
        nonlocal upload_attempts
        assert token == "registry-token"
        if method == "PUT":
            upload_attempts += 1
            if upload_attempts > 1:
                raise helper.ReleaseArtifactError("already exists")
            return b""
        if method == "POST":
            raise helper.ReleaseArtifactError("link response lost")
        if url.endswith("/completion.json"):
            return expected
        return json.dumps(
            {
                "type": "generic",
                "name": "proxbox-api-control-attestation",
                "version": "1.2.3",
                "repository": {"full_name": "emersonfelipesp/proxbox-api"},
            }
        ).encode()

    monkeypatch.setattr(helper, "_request", request)
    monkeypatch.setattr(helper, "validate_release_attestation", lambda **_kwargs: evidence)
    arguments = {
        "owner": "emersonfelipesp",
        "repository": "proxbox-api",
        "manifest": manifest,
        "evidence": evidence,
        "token": "registry-token",
        **_attestation_publish_identity(),
    }
    assert helper.publish_gitea_attestation(**arguments) == evidence
    assert helper.publish_gitea_attestation(**arguments) == evidence
    assert upload_attempts == 2


def test_existing_attestation_rejects_nonidentical_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _helper()
    monkeypatch.setenv("GITEA_PACKAGE_REGISTRY_ORIGIN", "https://registry.example.test")
    manifest = {"package": "proxbox-api", "version": "1.2.3"}
    evidence = {"control_request_id": "c" * 32, "deployment_status": "success"}

    def request(url: str, *, method: str = "GET", **_kwargs: object) -> bytes:
        if method == "PUT":
            raise helper.ReleaseArtifactError("already exists")
        if url.endswith("/completion.json"):
            return b"{}\n"
        return json.dumps(
            {
                "type": "generic",
                "name": "proxbox-api-control-attestation",
                "version": "1.2.3",
                "repository": {"full_name": "emersonfelipesp/proxbox-api"},
            }
        ).encode()

    monkeypatch.setattr(helper, "_request", request)
    monkeypatch.setattr(helper, "validate_release_attestation", lambda **_kwargs: evidence)
    with pytest.raises(helper.ReleaseArtifactError, match="bytes changed"):
        helper.publish_gitea_attestation(
            owner="emersonfelipesp",
            repository="proxbox-api",
            manifest=manifest,
            evidence=evidence,
            token="registry-token",
            **_attestation_publish_identity(),
        )


def test_production_workflow_has_no_dynamic_code_execution() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    forbidden = ("eval(", "exec(", "os.system(", "pickle.loads(")
    assert not any(token in text for token in forbidden)
    assert re.search(r"if:\s*always\(\)", text)
