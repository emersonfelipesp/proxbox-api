"""Static release workflow contracts.

These checks keep the package publication pipeline aligned with the staged
TestPyPI -> PyPI release process without running a publishing workflow.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GITEA_CI_WORKFLOW_PATH = REPO_ROOT / ".gitea" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-testpypi.yml"
GITEA_PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".gitea" / "workflows" / "publish-gitea.yml"
GITEA_ARTIFACT_WORKFLOW_PATH = REPO_ROOT / ".gitea" / "workflows" / "artifact-v3-compatibility.yml"
GITEA_DEPLOY_WORKFLOW_PATH = REPO_ROOT / ".gitea" / "workflows" / "deploy-production.yml"
GITEA_PROMOTE_WORKFLOW_PATH = REPO_ROOT / ".gitea" / "workflows" / "promote-final-tag.yml"
RELEASE_ARTIFACTS_PATH = REPO_ROOT / "scripts" / "release_artifacts.py"
PREPARE_OFFLINE_RELEASE_PATH = REPO_ROOT / "scripts" / "prepare_offline_release.py"
VERIFY_OFFLINE_RELEASE_PATH = REPO_ROOT / "scripts" / "verify_offline_release_sdist.py"
CI_MATRIX_PATH = REPO_ROOT / "scripts" / "ci_e2e_matrix.py"
CI_GATE_PATH = REPO_ROOT / "scripts" / "gitea_ci_gate.py"
RUNNER_GATE_PATH = REPO_ROOT / "scripts" / "gitea_release_runner_gate.py"
BUILD_BOUNDARY_PATH = REPO_ROOT / "scripts" / "gitea_release_build_boundary.py"
HANDOFF_PATH = REPO_ROOT / "scripts" / "gitea_release_handoff.py"
RUNNER_ACCEPTANCE_PATH = REPO_ROOT / ".gitea" / "release-runner-acceptance.json"
RELEASE_CONTROL_DOC_PATHS = (
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "docs" / "development" / "release-publishing.md",
    REPO_ROOT / "docs" / "pt-BR" / "development" / "release-publishing.md",
)
SIGNED_HANDOFF_DOC_PATHS = (
    REPO_ROOT / ".github" / "CLAUDE.md",
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "docs" / "development" / "release-publishing.md",
    REPO_ROOT / "docs" / "pt-BR" / "development" / "release-publishing.md",
    REPO_ROOT / "docs" / "release-notes" / "version-0.0.20.md",
)


def _load_release_artifacts():
    spec = importlib.util.spec_from_file_location("release_artifacts", RELEASE_ARTIFACTS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_offline_release_preparer():
    spec = importlib.util.spec_from_file_location(
        "prepare_offline_release", PREPARE_OFFLINE_RELEASE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_offline_sdist_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_offline_release_sdist", VERIFY_OFFLINE_RELEASE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ci_gate():
    spec = importlib.util.spec_from_file_location("gitea_ci_gate", CI_GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_runner_gate():
    spec = importlib.util.spec_from_file_location("gitea_release_runner_gate", RUNNER_GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_build_boundary():
    spec = importlib.util.spec_from_file_location(
        "gitea_release_build_boundary", BUILD_BOUNDARY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_handoff():
    spec = importlib.util.spec_from_file_location("gitea_release_handoff", HANDOFF_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ci_matrix():
    spec = importlib.util.spec_from_file_location("ci_e2e_matrix", CI_MATRIX_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NETBOX_VERSIONS_PATH = REPO_ROOT / ".github" / "netbox-versions.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_order(text: str, *needles: str) -> None:
    cursor = -1
    for needle in needles:
        position = text.find(needle)
        assert position > cursor, f"{needle!r} was not found after offset {cursor}"
        cursor = position


def test_ci_e2e_uses_http_mock_for_container_path_and_backend_mock_separately():
    workflow = _read(CI_WORKFLOW_PATH)

    assert "Run E2E tests (Docker proxmox mock)" in workflow
    assert 'uv run pytest tests/e2e/ -m "mock_http" --tb=short -v' in workflow
    assert "Run E2E tests with in-process MockBackend" in workflow
    assert 'uv run pytest tests/e2e/ -m "mock_backend" --tb=short -v' in workflow


def test_every_event_e2e_matrix_stays_within_github_limit_and_keeps_coverage():
    ci_matrix = _load_ci_matrix()
    versions = json.loads(NETBOX_VERSIONS_PATH.read_text(encoding="utf-8"))

    for event, mode in (
        ("push", "dev"),
        ("pull_request", "dev"),
        ("workflow_dispatch", "pypi"),
        ("release", "dev"),
    ):
        rows = ci_matrix.generate_matrix(
            event=event,
            mode_input=mode,
            netbox_versions=versions,
        )["include"]
        assert 0 < len(rows) <= ci_matrix.MAX_GITHUB_MATRIX_JOBS
        assert {row["netbox_version"] for row in rows} == set(versions)
        assert {row["network_stack"] for row in rows} == {"ipv4", "ipv6"}
        assert {row["proxbox_docker_target"] for row in rows} == {"raw", "nginx", "granian"}
        assert {row["proxmox_service"] for row in rows} == {"pve", "pbs", "pdm"}

    release_rows = ci_matrix.generate_matrix(
        event="release", mode_input="dev", netbox_versions=versions
    )["include"]
    assert len(release_rows) == 162
    assert {row["netbox_proxbox_mode"] for row in release_rows} == {"dev", "pypi"}


def test_ci_e2e_explicitly_opts_into_legacy_custom_field_coverage():
    workflow = _read(CI_WORKFLOW_PATH)
    e2e_block = workflow.split("e2e-docker:", 1)[1]

    _assert_order(
        e2e_block,
        "Wait for NetBox and create API token",
        '"${{ matrix.netbox_public_url }}/api/plugins/proxbox/settings/"',
        "--data '{\"custom_fields_enabled\":true}'",
        "Create Proxbox custom fields in NetBox",
        'uv run pytest tests/e2e/ -m "mock_http" --tb=short -v',
    )


def test_primary_ci_enforces_repository_coverage_ratchet():
    workflow = yaml.safe_load(_read(CI_WORKFLOW_PATH))
    test_steps = {
        step["name"]: step for step in workflow["jobs"]["test"]["steps"] if "name" in step
    }
    config = tomllib.loads(_read(PYPROJECT_PATH))
    coverage_run = config["tool"]["coverage"]["run"]
    coverage_report = config["tool"]["coverage"]["report"]
    expected_omits = {
        "proxbox_api/e2e/*",
        "proxbox_api/generated/*",
    }

    assert coverage_run["source"] == ["proxbox_api"]
    assert coverage_run["branch"] is True
    assert set(coverage_run["omit"]) == expected_omits
    assert set(coverage_report["omit"]) == expected_omits
    assert coverage_report["fail_under"] >= 65.40
    assert coverage_report["precision"] == 2

    coverage_step = test_steps["Core tests with coverage"]
    coverage_command = coverage_step["run"]
    assert "--ignore=tests/e2e" in coverage_command
    assert "--ignore=tests/test_generated_proxmox_routes.py" in coverage_command
    assert "--cov=proxbox_api" in coverage_command
    assert "--cov-branch" in coverage_command
    assert "--cov-report=term-missing" in coverage_command
    assert "--cov-report=xml:coverage.xml" in coverage_command

    upload_step = test_steps["Upload coverage report"]
    assert upload_step["if"] == "${{ always() }}"
    assert upload_step["uses"] == (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert upload_step["with"] == {
        "name": "coverage-py312",
        "path": "coverage.xml",
        "if-no-files-found": "error",
        "retention-days": 14,
    }


def test_gitea_pr_gate_runs_the_same_coverage_scope_without_secrets():
    workflow_source = _read(GITEA_CI_WORKFLOW_PATH)
    workflow = yaml.safe_load(workflow_source)
    quality_job = workflow["jobs"]["quality"]
    steps = {step["name"]: step for step in quality_job["steps"] if "name" in step}

    assert quality_job["runs-on"] == "ci-untrusted-python312"
    assert "${{ secrets." not in workflow_source
    assert "prod-deploy" not in workflow_source
    assert "mirror-host" not in workflow_source
    assert "curl " not in workflow_source

    checkout_step = steps["Checkout"]
    assert checkout_step["uses"] == ("actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd")
    assert checkout_step["with"]["persist-credentials"] is False

    coverage_step = steps["Core tests with coverage"]
    coverage_command = coverage_step["run"]
    assert "--ignore=tests/e2e" in coverage_command
    assert "--ignore=tests/test_generated_proxmox_routes.py" in coverage_command
    assert "--cov=proxbox_api" in coverage_command
    assert "--cov-report=xml:coverage.xml" in coverage_command
    # The Gitea gate deliberately runs statement-only coverage so coverage.py
    # can use its low-overhead sysmon core on Python 3.12; branch coverage
    # stays in the GitHub-hosted .github/ CI. The worker count is pinned
    # because the runner containers have an 8-CPU quota while os.cpu_count()
    # reports the host's cores.
    assert "--cov-branch" not in coverage_command
    assert "--cov-report=term-missing" not in coverage_command
    assert "-n 8" in coverage_command
    assert "--dist worksteal" in coverage_command
    assert coverage_step["env"]["COVERAGE_CORE"] == "sysmon"

    upload_step = steps["Upload coverage report"]
    assert upload_step["if"] == "${{ always() }}"
    # The Gitea gate pins upload-artifact v3: Gitea's artifact service speaks
    # the v3 protocol only, and the v4 action fails with GHESNotSupportedError
    # after an otherwise-green run. The GitHub workflow keeps its v4 pin.
    assert upload_step["uses"] == (
        "actions/upload-artifact@a8a3f3ad30e3422c9c7b888a15615d19a852ae32"
    )
    assert upload_step["with"] == {
        "name": "coverage-py312-gitea",
        "path": "coverage.xml",
        "if-no-files-found": "error",
        "retention-days": 14,
    }


def test_netbox_e2e_readiness_is_long_enough_for_migrations_and_api_status():
    ci_workflow = _read(CI_WORKFLOW_PATH)
    publish_workflow = _read(PUBLISH_WORKFLOW_PATH)

    assert "timeout-minutes: 45" in ci_workflow
    assert "for i in $(seq 1 600); do" in ci_workflow
    assert "NetBox API did not become ready" in ci_workflow

    assert publish_workflow.count("timeout-minutes: 45") >= 2
    assert publish_workflow.count("for i in $(seq 1 900); do") >= 2
    assert publish_workflow.count("NetBox API did not become ready") >= 2


def test_ci_e2e_loads_prepared_image_artifacts_before_stack_start():
    workflow = _read(CI_WORKFLOW_PATH)
    e2e_block = workflow.split("e2e-docker:", 1)[1]

    assert "prepare-e2e-service-images:" in workflow
    assert "prepare-proxmox-image:" in workflow
    assert "build-proxbox-image:" in workflow
    assert "proxbox-e2e-proxmox-mock:${{ matrix.service }}" in workflow
    assert "emersonfelipesp/proxmox-sdk:latest-${{ matrix.service }}" not in workflow
    assert "proxbox_image_matrix" in workflow
    _assert_order(
        e2e_block,
        "Download NetBox image artifact",
        "Download Proxmox mock image artifact",
        "Download E2E service image artifact",
        "Download Proxbox API image artifact",
        "Load Docker image artifacts",
        "Start E2E stack",
    )
    assert "Resolve NetBox image source" not in workflow
    assert 'docker pull "${PROXMOX_OPENAPI_IMAGE}"' not in e2e_block

    start_backend_block = e2e_block.split("Start Proxbox API backend container", 1)[1]
    start_backend_block = start_backend_block.split(
        "Verify Proxbox API reaches NetBox with requested transport", 1
    )[0]
    assert "docker build" not in start_backend_block
    assert '"${PROXBOX_IMAGE}"' in start_backend_block


def test_netbox_source_build_fallback_uses_current_upstream_base_image():
    ci_workflow = _read(CI_WORKFLOW_PATH)
    publish_workflow = _read(PUBLISH_WORKFLOW_PATH)

    assert "FROM=ubuntu:24.04" not in ci_workflow
    assert "FROM=ubuntu:24.04" not in publish_workflow
    assert "FROM=ubuntu:26.04" not in ci_workflow
    assert "FROM=ubuntu:26.04" not in publish_workflow
    assert ci_workflow.count('--build-arg "FROM=${CI_OFFICIAL_IMAGE_PREFIX}/ubuntu:26.04"') == 1
    assert (
        publish_workflow.count('--build-arg "FROM=${CI_OFFICIAL_IMAGE_PREFIX}/ubuntu:26.04"') == 2
    )


def test_ci_docker_builds_use_mirror_backed_python_base_images():
    workflow = _read(CI_WORKFLOW_PATH)

    assert "CI_OFFICIAL_IMAGE_PREFIX: mirror.gcr.io/library" in workflow
    assert (
        '--build-arg "PYTHON_BASE_IMAGE=${CI_OFFICIAL_IMAGE_PREFIX}/python:3.13-alpine"' in workflow
    )
    assert (
        '--build-arg "PYTHON_BASE_IMAGE=${CI_OFFICIAL_IMAGE_PREFIX}/python:3.13-slim-bookworm"'
        in workflow
    )


def test_publish_workflow_routes_rc_tags_to_testpypi_and_releases_to_pypi():
    workflow = _read(PUBLISH_WORKFLOW_PATH)

    # BaseLoader constructs strings only and preserves Actions' literal `on` key.
    parsed = yaml.load(workflow, Loader=yaml.BaseLoader)  # nosec B506
    dispatch_inputs = parsed["on"]["workflow_dispatch"]["inputs"]

    assert '- "v*rc*"' in workflow
    assert '- "v*"' not in workflow
    assert "publish_target = 'testpypi'" in workflow
    assert "elif event == 'release':" in workflow
    assert "re.search(r'rc\\d+$', version)" in workflow
    assert "Unsupported release event/ref combination" in workflow
    assert "--repository-url https://test.pypi.org/legacy/" in workflow
    assert "--repository-url https://upload.pypi.org/legacy/" in workflow
    dispatch_block = workflow.split("workflow_dispatch:", 1)[1].split("permissions:", 1)[0]
    assert "- testpypi" in dispatch_block
    assert "- pypi" not in dispatch_block
    assert "Manual dispatch is TestPyPI-only and requires an RC version" in workflow
    assert set(dispatch_inputs) == {"publish_target", "source_ref", "expected_version"}
    assert dispatch_inputs["source_ref"]["type"] == "string"


def test_gitea_tag_workflow_builds_only_a_release_control_request():
    workflow = _read(GITEA_PUBLISH_WORKFLOW_PATH)
    parsed = yaml.safe_load(workflow)
    assert parsed["concurrency"] == {
        "group": "release-request-${{ github.repository }}",
        "cancel-in-progress": False,
    }
    gate_sha256 = hashlib.sha256(CI_GATE_PATH.read_bytes()).hexdigest()
    runner_gate_sha256 = hashlib.sha256(RUNNER_GATE_PATH.read_bytes()).hexdigest()
    build_boundary_sha256 = hashlib.sha256(BUILD_BOUNDARY_PATH.read_bytes()).hexdigest()
    handoff_sha256 = hashlib.sha256(HANDOFF_PATH.read_bytes()).hexdigest()
    acceptance_sha256 = hashlib.sha256(RUNNER_ACCEPTANCE_PATH.read_bytes()).hexdigest()
    acceptance = json.loads(RUNNER_ACCEPTANCE_PATH.read_bytes())

    assert "  create:" not in workflow
    assert set(parsed["jobs"]) == {"validate-source", "build-request"}
    assert all(job["runs-on"] == "ci-release-proxbox-api" for job in parsed["jobs"].values())
    assert "refs/heads/develop:refs/remotes/gitea/release-develop" in workflow
    assert "release-manifest.json" in workflow
    assert "scripts/gitea_ci_gate.py" in workflow
    assert "/commits/${SOURCE_SHA}/statuses" not in workflow
    assert "actions/upload-artifact@" not in workflow
    assert "/usr/local/bin/nmc-upload-gitea-artifact" in workflow
    assert "/usr/local/bin/nmc-gitea-checkout" in workflow
    assert "actions/download-artifact@" not in workflow
    assert "astral-sh/setup-uv@" not in workflow
    assert "RUNNER_TOOL_CACHE" not in workflow
    assert "UV_MANAGED_PYTHON" not in workflow
    assert "mirror-host" not in workflow
    assert "packages: write" not in workflow
    assert "secrets." not in workflow
    assert "PKG_TOKEN" not in workflow
    assert "GH_MIRROR_TOKEN" not in workflow
    assert "release-publisher" not in workflow
    assert "twine upload" not in workflow
    assert "gh release create" not in workflow
    assert "git push" not in workflow
    assert "/api/packages/" not in workflow
    assert (
        workflow.count(
            "https://github.com/astral-sh/uv/releases/download/0.11.28/"
            "uv-x86_64-unknown-linux-gnu.tar.gz"
        )
        == 0
    )
    assert workflow.count("e490a6464492183c5d4534a5527fb4440f7f2bb2f228162ad7e4afe076dc0224") == 0
    assert workflow.count("sha256sum --check --strict") >= 7
    assert "python3 -I scripts/gitea_ci_gate.py" in workflow
    assert gate_sha256 == "ac39691b607ab40665026d5c1d2b49f985ea1b3778a393fd24d7710006ad30a5"
    assert gate_sha256 in workflow
    assert "1b7946a1ab787507b24c404b4fe5e3805645cd21bca1dccee215393a798d6dad" in workflow
    assert runner_gate_sha256 == (
        "9f85dd453d285cb1c79483b19ad1781b4ed7df635695d80c6769a09abd178027"
    )
    assert build_boundary_sha256 == (
        "dd9cd17aad595ee976355bae369bc19dcdacc894accb9a02579db07b1f8e7a41"
    )
    assert handoff_sha256 == ("4017b7dc0443e4827f27c0571d41188e63f19bffdcb3e785307de1a084561002")
    assert acceptance_sha256 == ("b299b14006004732f906e1f03e8325094c9aebe9efb56bb579b2c376eb86072e")
    assert workflow.count(runner_gate_sha256) == 2
    assert workflow.count(build_boundary_sha256) == 1
    assert workflow.count(handoff_sha256) == 1
    assert workflow.count(acceptance_sha256) == 2
    assert acceptance["runner_id"] == 0
    assert acceptance["runner_name"] == ""
    assert acceptance["validation_runner_id"] == 0
    assert acceptance["validation_runner_name"] == ""
    assert acceptance["runner_scope_sha256"] == "0" * 64
    assert acceptance["validation_runner_scope_sha256"] == "0" * 64
    assert acceptance["runtime_attestation_sha256"] == "0" * 64
    assert acceptance["network_attestation_sha256"] == "0" * 64
    assert acceptance["attestation_public_key_sha256"] == "0" * 64
    assert acceptance["runtime_image_digest"] == "0" * 64
    assert acceptance["supervisor_policy_sha256"] == "0" * 64
    assert acceptance["registered_labels"] == [
        "ci-release-proxbox-api",
    ]
    build_steps = parsed["jobs"]["build-request"]["steps"]
    completion_run = next(
        step["run"]
        for step in build_steps
        if step["name"] == "Obtain supervisor-signed completion evidence"
    )
    assert isinstance(completion_run, str)
    completion_source = completion_run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    completion_tree = ast.parse(completion_source)
    completion_imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(completion_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    gate_index = next(
        index
        for index, step in enumerate(build_steps)
        if step["name"].startswith("Prove exact accepted release runner")
    )
    candidate_index = next(
        index
        for index, step in enumerate(build_steps)
        if step["name"] == "Build artifacts across a token-free UID boundary"
    )
    assert gate_index < candidate_index
    assert "/nmc-build/proxbox-api-" in workflow
    assert "docker run" not in yaml.safe_dump(parsed["jobs"]["build-request"])
    assert "/usr/local/bin/nmc-release-attestation-client" in workflow
    assert "2b0bee25d755f284b5e8eee3b8a84536825328913040c8757374efe51c57f75f" in workflow
    assert "os.O_NOFOLLOW" in workflow
    assert "pass_fds=(snapshot,)" in workflow
    assert 'os.memfd_create("nmc-release-client", flags)' in workflow
    assert "fcntl.fcntl(snapshot, 1033, seals)" in workflow
    assert {"ctypes", "fcntl", "hashlib", "os", "stat", "subprocess", "sys"} <= (completion_imports)
    compile(completion_source, "publish-gitea-completion", "exec")
    assert '"--public-key"' in workflow
    assert "runner-completion-attestation.json" in workflow
    assert "runner-completion-attestation.sig" in workflow
    assert "UV_PYTHON_INSTALL_DIR" not in workflow


def test_release_build_boundary_is_token_free_bounded_and_dockerless():
    boundary = _load_build_boundary()
    command = boundary._candidate_command()

    assert "docker run" not in command
    assert "ensurepip" not in command
    assert "pip download" not in command
    assert command.count("--no-config") == 2
    assert "--managed-python" not in command
    assert command.count("--no-python-downloads") == 1
    assert "--offline --no-index --find-links" in command
    assert '"$BUILD_ROOT/venv/bin/python" -m build --no-isolation' in command
    assert "scripts/prepare_offline_release.py" in command
    assert "--require-hashes" not in command
    assert "--only-binary=:all:" not in command
    assert '--python "$UV_PYTHON"' in command
    assert '--python "$BUILD_ROOT/venv/bin/python"' in command
    assert 'cp "$NMC_RUNTIME_WHEELHOUSE"/*.whl docker/build-cache/' in command
    assert "docker/build-cache" in command
    for variable in (
        "GITHUB_TOKEN",
        "GITEA_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
    ):
        assert f'test -z "${{{variable}:-}}"' in command
    assert (
        boundary.process_cpu_ticks("123 (candidate name) R 1 2 3 4 5 6 7 8 9 10 11 12 13 14") == 50
    )
    with pytest.raises(ValueError, match="Malformed"):
        boundary.process_cpu_ticks("malformed")


def test_release_handoff_copies_only_exact_regular_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = _load_handoff()
    build_root = tmp_path / "build"
    source_root = build_root / "source"
    dist_root = source_root / "dist"
    dist_root.mkdir(parents=True)
    version = "0.0.20rc1"
    artifacts = []
    for name, payload in (
        (f"proxbox_api-{version}-py3-none-any.whl", b"wheel"),
        (f"proxbox_api-{version}.tar.gz", b"sdist"),
    ):
        path = dist_root / name
        path.write_bytes(payload)
        artifacts.append(
            {"name": name, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        )
    manifest = {
        "artifacts": sorted(artifacts, key=lambda row: row["name"]),
        "package": "proxbox_api",
        "schema": 1,
        "source_sha": "a" * 40,
        "version": version,
    }
    (source_root / "release-manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    workflow = tmp_path / "publish-gitea.yml"
    workflow.write_text("name: reviewed\n", encoding="utf-8")
    transfer = tmp_path / "transfer"
    monkeypatch.setattr(handoff.os, "geteuid", lambda: 0)

    request = handoff.create_handoff(
        build_root=build_root,
        transfer_root=transfer,
        source_sha="a" * 40,
        tag=f"v{version}",
        version=version,
        run_id=1234,
        run_attempt=1,
        workflow_path=workflow,
    )

    assert request["artifacts"] == [
        {"filename": row["name"], "sha256": row["sha256"], "size": row["size"]}
        for row in manifest["artifacts"]
    ]
    assert {path.name for path in transfer.iterdir()} == {
        f"proxbox_api-{version}-py3-none-any.whl",
        f"proxbox_api-{version}.tar.gz",
        "release-manifest.json",
        "release-request.json",
    }
    assert all(not path.is_symlink() and path.is_file() for path in transfer.iterdir())


def test_release_sdist_uses_a_pinned_network_free_docker_contract():
    dockerfile = _read(REPO_ROOT / "Dockerfile.release")
    manifest = _read(REPO_ROOT / "MANIFEST.in")
    preparer_source = _read(REPO_ROOT / "scripts/prepare_offline_release.py")
    preparer = _load_offline_release_preparer()

    assert "\\\n" not in dockerfile
    assert dockerfile.count("@sha256:") == 2
    assert f"FROM {preparer.PINNED_IMAGES[1]} AS uv-source" in dockerfile
    assert f"FROM {preparer.PINNED_IMAGES[0]} AS raw" in dockerfile
    assert "COPY --from=uv-source /uv /usr/local/bin/uv" in dockerfile
    verifier = _load_offline_sdist_verifier()
    assert verifier.validate_dockerfile(dockerfile) == sorted(verifier.PINNED_IMAGES)
    assert "COPY docker/build-cache /root/.cache/uv" in dockerfile
    assert "uv sync --frozen --offline --no-index --find-links" in dockerfile
    assert not any(
        token in f" {dockerfile.lower().replace(chr(10), ' ')} "
        for token in (" apk ", " apt-get ", " curl ", " wget ", " git clone ")
    )
    assert "include docker/offline-build-inputs.json" in manifest
    assert "recursive-include docker/build-cache *.whl" in manifest
    assert "sort_keys=True" in preparer_source
    assert '"schema": 2' in preparer_source
    assert "PINNED_IMAGES" in preparer_source


def test_release_offline_sdist_job_builds_the_extracted_context_without_network():
    workflow = _read(CI_WORKFLOW_PATH)
    parsed = yaml.safe_load(workflow)
    job = parsed["jobs"]["release-offline-image"]
    source = "\n".join(str(step.get("run", "")) for step in job["steps"])
    actions = [step["uses"] for step in job["steps"] if "uses" in step]

    assert job["name"] == "Build extracted offline release sdist"
    assert job["runs-on"] == "ubuntu-latest"
    assert actions == [
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
        "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78",
    ]
    assert "scripts/prepare_offline_release.py" in source
    assert "scripts/verify_offline_release_sdist.py" in source
    assert "python -m build --no-isolation --sdist" in source
    assert "--require-hashes" in source
    assert "--only-binary=:all:" in source
    assert "--platform musllinux_1_2_x86_64" in source
    assert "--platform musllinux_1_1_x86_64" in source
    assert "--python-version 3.13" in source
    assert "--abi cp313 --abi abi3 --abi none" in source
    assert source.count("docker pull") == 2
    assert "docker build --network=none --pull=false --target raw" in source
    assert "$RUNNER_TEMP/proxbox-release-context" in source
    assert "branches: [main, develop, testing]" in workflow
    assert 'tags: ["v*"]' in workflow


def test_offline_sdist_verifier_rejects_variable_copy_sources_and_unsafe_members(
    tmp_path: Path,
) -> None:
    verifier = _load_offline_sdist_verifier()
    version = "0.0.20rc1"

    def make_sdist(path: Path, dockerfile: bytes, *, hostile_link: bool = False) -> None:
        wheel = b"wheel-bytes"
        uv_lock = b"version = 1\n"
        lock = {
            "dockerfile_sha256": hashlib.sha256(dockerfile).hexdigest(),
            "files": [
                {
                    "path": "docker/build-cache/package-1.0-py3-none-any.whl",
                    "sha256": hashlib.sha256(wheel).hexdigest(),
                    "size": len(wheel),
                }
            ],
            "images": sorted(verifier.PINNED_IMAGES),
            "schema": 2,
            "uv_lock_sha256": hashlib.sha256(uv_lock).hexdigest(),
        }
        files = {
            "Dockerfile": dockerfile,
            "docker/build-cache/package-1.0-py3-none-any.whl": wheel,
            "docker/offline-build-inputs.json": (
                json.dumps(lock, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            ),
            "pyproject.toml": b"[project]\nname='proxbox_api'\nversion='0.0.20rc1'\n",
            "uv.lock": uv_lock,
        }
        root = f"proxbox_api-{version}"
        with tarfile.open(path, "w:gz") as archive:
            root_member = tarfile.TarInfo(root)
            root_member.type = tarfile.DIRTYPE
            archive.addfile(root_member)
            for name, payload in files.items():
                member = tarfile.TarInfo(f"{root}/{name}")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            if hostile_link:
                member = tarfile.TarInfo(f"{root}/escape")
                member.type = tarfile.SYMTYPE
                member.linkname = "../../etc/passwd"
                archive.addfile(member)

    accepted = tmp_path / "accepted.tar.gz"
    make_sdist(
        accepted,
        (
            f"FROM {verifier.PINNED_IMAGES[1]} AS uv-source\n"
            f"FROM {verifier.PINNED_IMAGES[0]} AS raw\n"
            "COPY --from=uv-source /uv /usr/local/bin/uv\n"
            "COPY docker/build-cache /root/.cache/uv\n"
            "RUN uv sync --frozen --offline --no-index --find-links "
            "/root/.cache/uv --no-dev --no-editable\n"
        ).encode(),
    )
    output = verifier.extract_and_verify(accepted, tmp_path / "accepted", version)
    assert (output / "docker/offline-build-inputs.json").is_file()

    base = (
        f"FROM {verifier.PINNED_IMAGES[1]} AS uv-source\nFROM {verifier.PINNED_IMAGES[0]} AS raw\n"
    ).encode()
    hostile_dockerfiles = (
        base + b"COPY --from=$UV_IMAGE /uv /usr/local/bin/uv\n",
        base + b"COPY --from=${UV_IMAGE:-alpine:latest} /uv /usr/local/bin/uv\n",
        base + b"COPY --from=${UV_IMAGE+uv-source} /uv /usr/local/bin/uv\n",
        base + b"COPY --from=alpine:latest /uv /usr/local/bin/uv\n",
        base + b"ADD https://example.invalid/payload /tmp/payload\n",
        b"# syntax=docker/dockerfile:1\n" + base,
        base + b"COPY --from=uv-source \\\n /uv /usr/local/bin/uv\n",
        (
            b"# emersonfelipesp/proxbox-api:0.0.19.post5@sha256:"
            + b"f" * 64
            + b"\n# ghcr.io/astral-sh/uv:0.11.28@sha256:"
            + b"e" * 64
            + b"\nFROM alpine:latest AS raw\n"
        ),
    )
    for index, dockerfile in enumerate(hostile_dockerfiles, start=1):
        hostile = tmp_path / f"variable-{index}.tar.gz"
        make_sdist(hostile, dockerfile)
        with pytest.raises(verifier.OfflineSdistError, match="Dockerfile"):
            verifier.extract_and_verify(hostile, tmp_path / f"variable-{index}", version)

    linked = tmp_path / "linked.tar.gz"
    make_sdist(linked, b"FROM scratch\n", hostile_link=True)
    with pytest.raises(verifier.OfflineSdistError, match="link or special"):
        verifier.extract_and_verify(linked, tmp_path / "linked", version)


def test_offline_release_preparer_binds_exact_inputs(tmp_path: Path, monkeypatch) -> None:
    preparer = _load_offline_release_preparer()
    dockerfile_source = tmp_path / "Dockerfile.release"
    dockerfile_output = tmp_path / "Dockerfile"
    uv_lock = tmp_path / "uv.lock"
    cache_root = tmp_path / "docker" / "build-cache"
    lock_output = tmp_path / "docker" / "offline-build-inputs.json"
    cache_root.mkdir(parents=True)
    dockerfile = (
        f"FROM {preparer.PINNED_IMAGES[1]} AS uv-source\n"
        f"FROM {preparer.PINNED_IMAGES[0]} AS raw\n"
        "COPY --from=uv-source /uv /usr/local/bin/uv\n"
        "COPY docker/build-cache /root/.cache/uv\n"
        "RUN uv sync --frozen --offline --no-index --find-links "
        "/root/.cache/uv --no-dev --no-editable\n"
    )
    dockerfile_source.write_text(dockerfile, encoding="utf-8")
    uv_lock.write_text("version = 1\n", encoding="utf-8")
    wheel = cache_root / "dependency-1.0-py3-none-any.whl"
    wheel.write_bytes(b"immutable wheel")

    monkeypatch.setattr(preparer, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(preparer, "DOCKERFILE_SOURCE", dockerfile_source)
    monkeypatch.setattr(preparer, "DOCKERFILE_OUTPUT", dockerfile_output)
    monkeypatch.setattr(preparer, "UV_LOCK", uv_lock)
    monkeypatch.setattr(preparer, "CACHE_ROOT", cache_root)
    monkeypatch.setattr(preparer, "LOCK_OUTPUT", lock_output)

    lock = preparer.prepare()

    expected_wheel_sha = hashlib.sha256(b"immutable wheel").hexdigest()
    assert dockerfile_output.read_text(encoding="utf-8") == dockerfile
    assert lock == {
        "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(),
        "files": [
            {
                "path": "docker/build-cache/dependency-1.0-py3-none-any.whl",
                "sha256": expected_wheel_sha,
                "size": len(b"immutable wheel"),
            }
        ],
        "images": sorted(preparer.PINNED_IMAGES),
        "schema": 2,
        "uv_lock_sha256": hashlib.sha256(b"version = 1\n").hexdigest(),
    }
    assert lock_output.read_bytes() == (
        json.dumps(lock, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )


def test_offline_release_preparer_rejects_non_wheel_cache_content(
    tmp_path: Path, monkeypatch
) -> None:
    preparer = _load_offline_release_preparer()
    cache_root = tmp_path / "docker" / "build-cache"
    cache_root.mkdir(parents=True)
    (cache_root / "requirements.txt").write_text("mutable input\n", encoding="utf-8")
    monkeypatch.setattr(preparer, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(preparer, "CACHE_ROOT", cache_root)

    with pytest.raises(preparer.OfflineReleaseError, match="wheels only"):
        preparer._cache_inventory()


def test_release_control_request_binds_exact_repository_run_and_artifacts():
    workflow = _read(GITEA_PUBLISH_WORKFLOW_PATH)
    handoff = _read(HANDOFF_PATH)
    parsed = yaml.safe_load(workflow)
    build_job = parsed["jobs"]["build-request"]
    build_source = yaml.safe_dump(build_job)
    upload_step = build_job["steps"][-1]

    assert parsed["permissions"] == {"actions": "read", "contents": "read"}
    assert build_job["needs"] == "validate-source"
    assert upload_step["name"] == "Upload exact data-only control request"
    assert "/usr/local/bin/nmc-upload-gitea-artifact" in upload_step["run"]
    assert '--root release-transfer --run-id "$GITHUB_RUN_ID"' in upload_step["run"]
    assert '"repository_id": 37' in handoff
    assert '"owner": "emersonfelipesp"' in handoff
    assert '"repository": "proxbox-api"' in handoff
    assert '"schema": 1' in handoff
    assert '"workflow_sha256"' in handoff
    assert '"release_manifest_sha256"' in handoff
    assert '"initiating_run_id"' in handoff
    assert '"initiating_run_attempt"' in handoff
    assert "release-request.json" in workflow
    assert (
        'test "$(find release-transfer -mindepth 1 -maxdepth 1 -type f | wc -l)" -eq 6' in workflow
    )
    assert "secrets." not in build_source
    assert build_source.count("github.token") == 1


def test_operator_docs_match_the_locked_control_dispatch_contract() -> None:
    required = {
        "AGENTS.md": ("validate.yml", "publish.yml", "target run ID", "request SHA-256"),
        "CLAUDE.md": ("validate.yml", "publish.yml", "target run ID", "SHA-256"),
        "release-publishing.md": (
            "validate.yml",
            "publish.yml",
            "target run ID",
            "request SHA-256",
        ),
    }
    for path in RELEASE_CONTROL_DOC_PATHS:
        documentation = _read(path)
        assert "publish=true" not in documentation, path
        for phrase in required[path.name]:
            assert phrase in documentation, (path, phrase)

    for path in SIGNED_HANDOFF_DOC_PATHS:
        documentation = _read(path)
        lowered = documentation.lower()
        assert "four-file" not in lowered, path
        assert "quatro arquivos" not in lowered, path
        assert "six-file" in lowered or "six data files" in lowered or "seis arquivos" in lowered, (
            path
        )
        for filename in (
            "release-manifest.json",
            "release-request.json",
            "runner-completion-attestation.json",
            "runner-completion-attestation.sig",
        ):
            assert filename in documentation, (path, filename)
        if path in RELEASE_CONTROL_DOC_PATHS:
            assert "job-bound ephemeral" in lowered or "efêmer" in lowered or "efemer" in lowered, (
                path
            )


def test_gitea_job_token_is_confined_to_trusted_evidence_steps():
    parsed = yaml.safe_load(_read(GITEA_PUBLISH_WORKFLOW_PATH))
    validate_source = yaml.safe_dump(parsed["jobs"]["validate-source"])
    build_source = yaml.safe_dump(parsed["jobs"]["build-request"])

    assert validate_source.count("github.token") == 3
    assert validate_source.count("GITEA_API_TOKEN") == 2
    assert build_source.count("github.token") == 1
    assert build_source.count("GITEA_API_TOKEN") == 1
    candidate_step = next(
        step
        for step in parsed["jobs"]["build-request"]["steps"]
        if step["name"] == "Build artifacts across a token-free UID boundary"
    )
    candidate_source = yaml.safe_dump(candidate_step)
    assert "github.token" not in candidate_source
    assert "GITEA_API_TOKEN" not in candidate_source
    assert "/usr/local/bin/nmc-gitea-checkout" in validate_source
    assert "actions/checkout@" not in build_source


def test_gitea_artifact_v3_compatibility_probe_is_bounded_and_disposable():
    workflow = _read(GITEA_ARTIFACT_WORKFLOW_PATH)
    # BaseLoader constructs strings only and preserves Actions' literal `on` key.
    parsed = yaml.load(workflow, Loader=yaml.BaseLoader)  # nosec B506

    assert parsed["on"] == {"pull_request": "", "workflow_dispatch": ""}
    assert parsed["permissions"] == {"contents": "read"}
    assert set(parsed["jobs"]) == {"upload-probe", "download-probe"}
    assert all(job["runs-on"] == "ci-untrusted-python312" for job in parsed["jobs"].values())
    assert parsed["jobs"]["download-probe"]["needs"] == "upload-probe"
    assert workflow.count("dc2c74581ade8cb95ad4ce2cd0ceddd82968531606eb02a8fadf60905b379f6b") == 2
    assert "actions/upload-artifact@c6a3b2bd78b3985e4b2f15397fec357f0fd808de" in workflow
    assert "actions/download-artifact@ad191675b41f6a5b46da9a048cb6893812da158b" in workflow
    assert "mirror-host" not in workflow


def test_source_distribution_is_a_valid_raw_docker_build_context(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(dist), str(REPO_ROOT)],
        check=True,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    archives = list(dist.glob("proxbox_api-*.tar.gz"))
    assert len(archives) == 1

    extract_root = tmp_path / "extracted"
    extract_root.mkdir()
    with tarfile.open(archives[0], "r:gz") as archive:
        archive.extractall(extract_root, filter="data")
    roots = list(extract_root.iterdir())
    assert len(roots) == 1 and roots[0].is_dir()
    context = roots[0]

    runtime_inputs = {
        "Dockerfile",
        "uv.lock",
        "docker/entrypoint-granian.sh",
        "docker/entrypoint-nginx.sh",
        "docker/entrypoint-raw.sh",
        "docker/nginx/proxbox-https.conf.template",
        "docker/supervisor/proxbox.conf",
        "docker/supervisor/supervisord.conf",
    }
    assert all((context / path).is_file() for path in runtime_inputs)

    logical_lines: list[str] = []
    pending = ""
    for raw_line in (context / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_lines.append(pending)
        pending = ""
    assert not pending

    stage_bases: dict[str, str] = {}
    stage_sources: dict[str, list[str]] = {}
    stage_dependencies: dict[str, set[str]] = {}
    current_stage = ""
    for line in logical_lines:
        tokens = shlex.split(line)
        instruction = tokens[0].upper()
        if instruction == "FROM":
            assert len(tokens) >= 2
            current_stage = tokens[-1] if len(tokens) >= 4 and tokens[-2].upper() == "AS" else ""
            if current_stage:
                stage_bases[current_stage] = tokens[1]
                stage_sources[current_stage] = []
                stage_dependencies[current_stage] = set()
        elif instruction == "COPY" and current_stage:
            stage_copy = next(
                (
                    token.removeprefix("--from=")
                    for token in tokens[1:]
                    if token.startswith("--from=")
                ),
                None,
            )
            if stage_copy:
                stage_dependencies[current_stage].add(stage_copy)
                continue
            sources = [token for token in tokens[1:-1] if not token.startswith("--")]
            stage_sources[current_stage].extend(sources)

    required_stages: set[str] = set()
    pending_stages = ["raw"]
    while pending_stages:
        stage = pending_stages.pop()
        if stage not in stage_bases or stage in required_stages:
            continue
        required_stages.add(stage)
        pending_stages.append(stage_bases[stage])
        pending_stages.extend(stage_dependencies[stage])
    assert required_stages == {"builder", "runtime-base", "raw"}
    raw_context_sources = {
        source for stage in required_stages for source in stage_sources.get(stage, [])
    }
    assert raw_context_sources == {
        "README.md",
        "pyproject.toml",
        "uv.lock",
        "proxbox_api",
        "docker/entrypoint-raw.sh",
    }
    assert all((context / source).exists() for source in raw_context_sources)

    docker = shutil.which("docker")
    if docker is None:
        return
    docker_help = subprocess.run(
        [docker, "build", "--help"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout
    try:
        docker_info = subprocess.run(
            [docker, "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return
    if "--check" not in docker_help or docker_info.returncode != 0:
        return
    docker_environment = os.environ.copy()
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    docker_environment["DOCKER_CONFIG"] = str(docker_config)
    subprocess.run(
        [docker, "build", "--check", "--target", "raw", "."],
        check=True,
        cwd=context,
        env=docker_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )


def test_repository_deploy_workflow_is_nms_source_aware():
    workflow = _read(GITEA_DEPLOY_WORKFLOW_PATH)

    assert "deploy_source:" in workflow
    assert "default: latest_package" in workflow
    assert "- latest_package" in workflow
    assert "- main_branch" in workflow
    assert "package_version:" in workflow
    assert "proxbox-api-staging" in workflow
    assert "deploy-app-package \\\n            proxbox-api" in workflow
    assert "deploy-app proxbox-api" in workflow
    assert "create-attestation" not in workflow
    assert "export-package-deploy-receipt" in workflow
    assert "GITEA_PACKAGE_TOKEN: ${{ secrets.PKG_TOKEN }}" in workflow
    assert "GITEA_PACKAGE_TOKEN: ${{ github.token }}" not in workflow
    assert "packages: write" not in workflow
    assert "publish-attestation" in workflow
    assert "inputs.skip_ci_gate != true" in workflow
    assert "healthcheck-app proxbox-api" in workflow


def test_final_tag_promotion_requires_main_package_and_nms_evidence():
    workflow = _read(GITEA_PROMOTE_WORKFLOW_PATH)

    assert "github.ref == 'refs/heads/main'" in workflow
    assert "refs/remotes/gitea/release-main" in workflow
    assert "refs/remotes/gitea/release-develop" in workflow
    assert "scripts/release_artifacts.py fetch-gitea" in workflow
    assert "scripts/release_artifacts.py fetch-attestation" in workflow
    assert "https://github.com/emersonfelipesp/proxbox-api.git" in workflow
    assert "GH_TOKEN: ${{ secrets.GH_MIRROR_TOKEN }}" in workflow
    assert 'GIT_ASKPASS="$SECRET_ROOT/askpass"' in workflow
    assert "http.https://github.com/.extraheader" not in workflow
    assert workflow.index("fetch-attestation") < workflow.index("GH_TOKEN:")
    assert "gh release create" not in workflow
    assert "rc[0-9]" not in workflow.split('python3 - "$VERSION"', 1)[1].split("PY", 1)[0]


def test_publish_workflow_never_reuses_consumed_package_versions():
    workflow = _read(PUBLISH_WORKFLOW_PATH)

    assert "--skip-existing" not in workflow
    assert "already_on_pypi" not in workflow


def test_public_publish_workflow_uses_immutable_locked_tooling():
    workflow = _read(PUBLISH_WORKFLOW_PATH)
    parsed = yaml.safe_load(workflow)
    project = tomllib.loads(_read(PYPROJECT_PATH))

    expected_actions = {
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
        "astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    }
    for job in parsed["jobs"].values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []):
            action = step.get("uses") if isinstance(step, dict) else None
            if isinstance(action, str) and not action.startswith("./"):
                assert action in expected_actions
                assert len(action.rsplit("@", 1)[1]) == 40
            if isinstance(action, str) and action.startswith("astral-sh/setup-uv@"):
                assert step.get("with", {}).get("version") == "0.11.28"

    assert project["dependency-groups"]["publish"] == [
        "build==1.5.0",
        "packaging==26.0",
        "setuptools==80.9.0",
        "twine==6.2.0",
        "wheel==0.45.1",
    ]
    assert workflow.count("uv sync --only-group publish --locked") == 3
    assert workflow.count("uv sync --only-group publish --locked --no-install-project") == 2
    assert "uv run --with twine python -m twine check" not in workflow
    assert "uv run --with twine python -m twine upload" not in workflow
    assert workflow.count(".venv/bin/python -m twine upload") == 2
    assert "--username" not in workflow
    assert "--password" not in workflow

    expected_upload_env = {
        "publish-testpypi": {
            "TWINE_PASSWORD": "${{ secrets.TEST_PYPI_TOKEN }}",
            "TWINE_USERNAME": "${{ secrets.TEST_PYPI_USERNAME }}",
        },
        "publish-pypi": {
            "TWINE_PASSWORD": "${{ secrets.PYPI_TOKEN }}",
            "TWINE_USERNAME": "${{ secrets.PYPI_USERNAME }}",
        },
    }
    for job_name, expected_env in expected_upload_env.items():
        job = parsed["jobs"][job_name]
        secret_steps = [step for step in job["steps"] if "${{ secrets." in yaml.safe_dump(step)]
        assert len(secret_steps) == 1
        assert secret_steps[0]["env"] == expected_env
        assert "twine upload" in secret_steps[0]["run"]
        assert job["runs-on"] == "ubuntu-latest"


def test_github_promotion_uses_exact_gitea_artifacts_and_nms_evidence():
    workflow = _read(PUBLISH_WORKFLOW_PATH)

    assert "scripts/release_artifacts.py fetch-gitea" in workflow
    assert "scripts/release_artifacts.py fetch-attestation" in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "Build distribution" not in workflow
    assert "validate-gitea-artifacts:" in workflow
    assert "kind: [wheel, sdist]" in workflow


def test_release_manifest_binds_exact_artifact_bytes(tmp_path: Path) -> None:
    release_artifacts = _load_release_artifacts()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "proxbox_api-0.0.20-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "proxbox_api-0.0.20.tar.gz").write_bytes(b"sdist")
    manifest_path = tmp_path / "release-manifest.json"
    sha = "a" * 40

    manifest = release_artifacts.write_manifest(
        dist=dist,
        package="proxbox_api",
        version="0.0.20",
        source_sha=sha,
        output=manifest_path,
    )
    assert (
        release_artifacts.verify_manifest(
            manifest_path=manifest_path,
            dist=dist,
            package="proxbox_api",
            version="0.0.20",
            source_sha=sha,
        )
        == manifest
    )

    (dist / "proxbox_api-0.0.20.tar.gz").write_bytes(b"changed")
    with pytest.raises(release_artifacts.ReleaseArtifactError):
        release_artifacts.verify_manifest(
            manifest_path=manifest_path,
            dist=dist,
            package="proxbox_api",
            version="0.0.20",
            source_sha=sha,
        )


def test_ci_gate_binds_latest_actions_run_to_authenticated_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _load_ci_gate()
    sha = "a" * 40
    context = "CI / Lint, smoke, and core coverage (push)"
    runs_path = (
        "/repos/emersonfelipesp/proxbox-api/actions/runs?"
        f"branch=develop&event=push&head_sha={sha}&limit=100&page=1"
    )
    jobs_path = "/repos/emersonfelipesp/proxbox-api/actions/runs/12/jobs"
    run = {
        "id": 12,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha,
        "head_branch": "develop",
        "path": "ci.yml@refs/heads/develop",
        "run_attempt": 0,
        "actor": {"login": "emersonfelipesp"},
    }
    job = {
        "id": 34,
        "run_id": 12,
        "run_attempt": 1,
        "name": "Lint, smoke, and core coverage",
        "status": "completed",
        "conclusion": "success",
        "head_sha": sha,
        "runner_name": "ci-untrusted-proxbox-api",
        "labels": ["ci-untrusted-python312"],
        "html_url": "https://git.nmulti.cloud/emersonfelipesp/proxbox-api/actions/runs/12/jobs/34",
    }
    responses = {
        runs_path: {"workflow_runs": [run], "total_count": 1},
        jobs_path: {"jobs": [job], "total_count": 1},
    }
    monkeypatch.setattr(gate, "_request_json", lambda path, *, token: responses[path])

    evidence = gate.validate_ci_gate(
        owner="emersonfelipesp",
        repository="proxbox-api",
        source_sha=sha,
        required_contexts=[context],
        trusted_actor="emersonfelipesp",
        token="test-token",
    )
    assert evidence == {context: {"job_id": 34, "run_attempt": 1, "run_id": 12}}

    runs = responses[runs_path]["workflow_runs"]
    assert isinstance(runs, list)
    runs.insert(0, {**run, "id": 13, "conclusion": "failure"})
    responses[runs_path]["total_count"] = 2
    with pytest.raises(gate.CIGateError, match="run does not match"):
        gate.validate_ci_gate(
            owner="emersonfelipesp",
            repository="proxbox-api",
            source_sha=sha,
            required_contexts=[context],
            trusted_actor="emersonfelipesp",
            token="test-token",
        )
    runs.pop(0)
    responses[runs_path]["total_count"] = 1

    run["run_attempt"] = 1
    assert gate.validate_ci_gate(
        owner="emersonfelipesp",
        repository="proxbox-api",
        source_sha=sha,
        required_contexts=[context],
        trusted_actor="emersonfelipesp",
        token="test-token",
    ) == {context: {"job_id": 34, "run_attempt": 1, "run_id": 12}}

    run["run_attempt"] = 2
    with pytest.raises(gate.CIGateError, match="run attempt is invalid"):
        gate.validate_ci_gate(
            owner="emersonfelipesp",
            repository="proxbox-api",
            source_sha=sha,
            required_contexts=[context],
            trusted_actor="emersonfelipesp",
            token="test-token",
        )
    run["run_attempt"] = 0

    job["run_attempt"] = 2
    with pytest.raises(gate.CIGateError, match="job does not match"):
        gate.validate_ci_gate(
            owner="emersonfelipesp",
            repository="proxbox-api",
            source_sha=sha,
            required_contexts=[context],
            trusted_actor="emersonfelipesp",
            token="test-token",
        )
    job["run_attempt"] = 1

    run["event"] = "pull_request"
    with pytest.raises(gate.CIGateError, match="workflow run is missing"):
        gate.validate_ci_gate(
            owner="emersonfelipesp",
            repository="proxbox-api",
            source_sha=sha,
            required_contexts=[context],
            trusted_actor="emersonfelipesp",
            token="test-token",
        )


def test_ci_gate_requires_exact_github_offline_image_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _load_ci_gate()
    sha = "a" * 40
    context = "CI / Lint, smoke, and core coverage (push)"
    gitea_runs_path = (
        "/repos/emersonfelipesp/proxbox-api/actions/runs?"
        f"branch=develop&event=push&head_sha={sha}&limit=100&page=1"
    )
    gitea_jobs_path = "/repos/emersonfelipesp/proxbox-api/actions/runs/12/jobs"
    gitea_run = {
        "actor": {"login": "emersonfelipesp"},
        "conclusion": "success",
        "event": "push",
        "head_branch": "develop",
        "head_sha": sha,
        "id": 12,
        "path": "ci.yml@refs/heads/develop",
        "run_attempt": 1,
        "status": "completed",
    }
    gitea_job = {
        "conclusion": "success",
        "head_sha": sha,
        "html_url": "https://git.nmulti.cloud/emersonfelipesp/proxbox-api/actions/runs/12/jobs/34",
        "id": 34,
        "labels": ["ci-untrusted-python312"],
        "name": "Lint, smoke, and core coverage",
        "run_attempt": 1,
        "run_id": 12,
        "runner_name": "ci-untrusted-proxbox-api",
        "status": "completed",
    }
    github_runs_path = (
        "/repos/emersonfelipesp/proxbox-api/actions/runs?"
        f"branch=develop&event=push&head_sha={sha}&page=1&per_page=100"
    )
    github_jobs_path = "/repos/emersonfelipesp/proxbox-api/actions/runs/56/jobs?per_page=100&page=1"
    workflow_raw = b"name: CI\n"
    workflow_sha256 = hashlib.sha256(workflow_raw).hexdigest()
    workflow_blob_sha = hashlib.sha1(
        f"blob {len(workflow_raw)}\0".encode() + workflow_raw,
        usedforsecurity=False,
    ).hexdigest()
    github_workflow_path = (
        f"/repos/emersonfelipesp/proxbox-api/contents/.github/workflows/ci.yml?ref={sha}"
    )
    github_run = {
        "actor": {"login": "emersonfelipesp"},
        "conclusion": "success",
        "event": "push",
        "head_branch": "develop",
        "head_repository": {"full_name": "emersonfelipesp/proxbox-api"},
        "head_sha": sha,
        "id": 56,
        "path": ".github/workflows/ci.yml",
        "repository": {"full_name": "emersonfelipesp/proxbox-api"},
        "run_attempt": 1,
        "status": "completed",
    }
    github_job = {
        "conclusion": "success",
        "head_sha": sha,
        "html_url": "https://github.com/emersonfelipesp/proxbox-api/actions/runs/56/job/78",
        "id": 78,
        "labels": ["ubuntu-latest"],
        "name": "Build extracted offline release sdist",
        "run_attempt": 1,
        "run_id": 56,
        "runner_group_name": "GitHub Actions",
        "status": "completed",
    }
    gitea_responses = {
        gitea_runs_path: {"workflow_runs": [gitea_run], "total_count": 1},
        gitea_jobs_path: {"jobs": [gitea_job], "total_count": 1},
    }
    github_responses = {
        github_workflow_path: {
            "content": base64.b64encode(workflow_raw).decode(),
            "encoding": "base64",
            "html_url": (
                "https://github.com/emersonfelipesp/proxbox-api/blob/"
                f"{sha}/.github/workflows/ci.yml"
            ),
            "name": "ci.yml",
            "path": ".github/workflows/ci.yml",
            "sha": workflow_blob_sha,
            "size": len(workflow_raw),
            "type": "file",
        },
        github_runs_path: {"workflow_runs": [github_run], "total_count": 1},
        github_jobs_path: {"jobs": [github_job], "total_count": 1},
    }
    monkeypatch.setattr(gate, "_request_json", lambda path, *, token: gitea_responses[path])
    monkeypatch.setattr(gate, "_request_github_json", lambda path: github_responses[path])

    evidence = gate.validate_ci_gate(
        owner="emersonfelipesp",
        repository="proxbox-api",
        source_sha=sha,
        required_contexts=[context],
        trusted_actor="emersonfelipesp",
        token="test-token",
        github_required_job="Build extracted offline release sdist",
        github_workflow_sha256=workflow_sha256,
    )
    assert evidence["GitHub CI / Build extracted offline release sdist (push)"] == {
        "job_id": 78,
        "run_attempt": 1,
        "run_id": 56,
    }
    github_job["runner_group_name"] = "untrusted"
    with pytest.raises(gate.CIGateError, match="job identity is invalid"):
        gate.validate_ci_gate(
            owner="emersonfelipesp",
            repository="proxbox-api",
            source_sha=sha,
            required_contexts=[context],
            trusted_actor="emersonfelipesp",
            token="test-token",
            github_required_job="Build extracted offline release sdist",
            github_workflow_sha256=workflow_sha256,
        )
    github_job["runner_group_name"] = "GitHub Actions"
    drifted = workflow_raw + b"# drift\n"
    github_responses[github_workflow_path]["content"] = base64.b64encode(drifted).decode()
    github_responses[github_workflow_path]["size"] = len(drifted)
    github_responses[github_workflow_path]["sha"] = hashlib.sha1(
        f"blob {len(drifted)}\0".encode() + drifted,
        usedforsecurity=False,
    ).hexdigest()
    with pytest.raises(gate.CIGateError, match="differ from reviewed policy"):
        gate.validate_ci_gate(
            owner="emersonfelipesp",
            repository="proxbox-api",
            source_sha=sha,
            required_contexts=[context],
            trusted_actor="emersonfelipesp",
            token="test-token",
            github_required_job="Build extracted offline release sdist",
            github_workflow_sha256=workflow_sha256,
        )


def test_release_runner_gate_rejects_sentinel_and_wrong_runner(tmp_path: Path) -> None:
    gate = _load_runner_gate()
    with pytest.raises(gate.RunnerGateError, match="not activated"):
        gate.validate_release_runner(
            acceptance_path=RUNNER_ACCEPTANCE_PATH,
            owner="emersonfelipesp",
            repository="proxbox-api",
            run_id=12,
            job_name="Build exact credential-free release-control request",
            source_sha="a" * 40,
            token="",
            jobs_payload={"jobs": [], "total_count": 0},
        )

    acceptance = {
        "attestation_public_key_sha256": "",
        "network_attestation_sha256": "b" * 64,
        "registered_labels": [
            "ci-release-proxbox-api",
        ],
        "runner_id": 41,
        "runner_label": "ci-release-proxbox-api",
        "runner_name": "ci-release-proxbox-api-runner",
        "runner_scope_sha256": "e" * 64,
        "runtime_attestation_sha256": "a" * 64,
        "runtime_image_digest": "c" * 64,
        "schema": 1,
        "supervisor_policy_sha256": "d" * 64,
        "validation_runner_id": 42,
        "validation_runner_name": "ci-release-proxbox-api-validate",
        "validation_runner_scope_sha256": "f" * 64,
    }
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    acceptance["attestation_public_key_sha256"] = hashlib.sha256(
        public_key.read_bytes()
    ).hexdigest()
    assert gate.TRUSTED_EXTERNAL_UID == 0
    with pytest.raises(gate.RunnerGateError, match="metadata is unsafe"):
        gate._open_external_file(
            public_key,
            "attestation public key",
            16384,
            trusted_uid=os.geteuid() + 1,
        )
    public_key.chmod(0o666)
    with pytest.raises(gate.RunnerGateError, match="metadata is unsafe"):
        gate._open_external_file(
            public_key,
            "attestation public key",
            16384,
            trusted_uid=os.geteuid(),
        )
    public_key.chmod(0o644)
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_bytes(gate._canonical_json(acceptance))
    job = {
        "conclusion": None,
        "head_sha": "a" * 40,
        "id": 34,
        "labels": ["ci-release-proxbox-api"],
        "name": "Build exact credential-free release-control request",
        "run_attempt": 1,
        "run_id": 12,
        "runner_id": 41,
        "runner_name": "ci-release-proxbox-api-runner",
        "status": "in_progress",
    }
    attestation_root = tmp_path / "attestations"
    attestation_root.mkdir()
    attestation_path = attestation_root / "run-12-job-34.json"
    signature_path = attestation_root / "run-12-job-34.sig"
    attestation = {
        "expires_at": 1200,
        "issued_at": 1000,
        "job_id": 34,
        "network_attestation_sha256": acceptance["network_attestation_sha256"],
        "registered_labels": acceptance["registered_labels"],
        "repository": "emersonfelipesp/proxbox-api",
        "run_id": 12,
        "runner_id": 41,
        "runner_name": "ci-release-proxbox-api-runner",
        "runner_scope_sha256": acceptance["runner_scope_sha256"],
        "runtime_attestation_sha256": acceptance["runtime_attestation_sha256"],
        "runtime_image_digest": acceptance["runtime_image_digest"],
        "schema": 1,
        "source_sha": "a" * 40,
        "supervisor_policy_sha256": acceptance["supervisor_policy_sha256"],
    }

    def sign(value: dict[str, object]) -> None:
        attestation_path.write_bytes(gate._canonical_json(value))
        subprocess.run(
            [
                "/usr/bin/openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(private_key),
                "-out",
                str(signature_path),
                str(attestation_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    sign(attestation)
    assert (
        gate.validate_release_runner(
            acceptance_path=acceptance_path,
            owner="emersonfelipesp",
            repository="proxbox-api",
            run_id=12,
            job_name=job["name"],
            source_sha="a" * 40,
            token="",
            jobs_payload={"jobs": [job], "total_count": 1},
            attestation_root=attestation_root,
            public_key_path=public_key,
            now=1100,
            trusted_external_uid=os.geteuid(),
        )["runner_id"]
        == 41
    )
    with pytest.raises(gate.RunnerGateError, match="exact accepted"):
        gate.validate_release_runner(
            acceptance_path=acceptance_path,
            owner="emersonfelipesp",
            repository="proxbox-api",
            run_id=12,
            job_name=job["name"],
            source_sha="a" * 40,
            token="",
            jobs_payload={"jobs": [{**job, "runner_id": 42}], "total_count": 1},
            attestation_root=attestation_root,
            public_key_path=public_key,
            now=1100,
        )
    for label, changed in (
        ("stale", {"issued_at": 800, "expires_at": 1000}),
        ("runtime", {"runtime_image_digest": "e" * 64}),
        ("network", {"network_attestation_sha256": "f" * 64}),
        ("repository-scope", {"runner_scope_sha256": "f" * 64}),
        (
            "labels",
            {
                "registered_labels": [
                    *acceptance["registered_labels"],
                    "ci-untrusted-extra",
                ]
            },
        ),
    ):
        sign({**attestation, **changed})
        with pytest.raises(gate.RunnerGateError, match="differs"):
            gate.validate_release_runner(
                acceptance_path=acceptance_path,
                owner="emersonfelipesp",
                repository="proxbox-api",
                run_id=12,
                job_name=job["name"],
                source_sha="a" * 40,
                token="",
                jobs_payload={"jobs": [job], "total_count": 1},
                attestation_root=attestation_root,
                public_key_path=public_key,
                now=1100,
                trusted_external_uid=os.geteuid(),
            )


def test_release_jobs_require_distinct_job_bound_ephemeral_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _load_runner_gate()
    acceptance = {
        "attestation_public_key_sha256": "a" * 64,
        "network_attestation_sha256": "b" * 64,
        "registered_labels": ["ci-release-proxbox-api"],
        "runner_id": 41,
        "runner_label": "ci-release-proxbox-api",
        "runner_name": "ci-release-proxbox-api-build",
        "runner_scope_sha256": "c" * 64,
        "runtime_attestation_sha256": "d" * 64,
        "runtime_image_digest": "e" * 64,
        "schema": 1,
        "supervisor_policy_sha256": "f" * 64,
        "validation_runner_id": 42,
        "validation_runner_name": "ci-release-proxbox-api-validate",
        "validation_runner_scope_sha256": "a" * 64,
    }
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_bytes(gate._canonical_json(acceptance))
    observed_scopes: list[str] = []

    def verify_attestation(**kwargs: object) -> str:
        observed_scopes.append(str(kwargs["expected_runner_scope_sha256"]))
        return "0" * 64

    monkeypatch.setattr(gate, "_verify_live_attestation", verify_attestation)
    jobs = (
        (
            gate.VALIDATION_JOB_NAME,
            acceptance["validation_runner_id"],
            acceptance["validation_runner_name"],
            acceptance["validation_runner_scope_sha256"],
        ),
        (
            gate.BUILD_JOB_NAMES["proxbox-api"],
            acceptance["runner_id"],
            acceptance["runner_name"],
            acceptance["runner_scope_sha256"],
        ),
    )
    for index, (job_name, runner_id, runner_name, runner_scope) in enumerate(jobs, start=1):
        job = {
            "conclusion": None,
            "head_sha": "a" * 40,
            "id": 30 + index,
            "labels": [acceptance["runner_label"]],
            "name": job_name,
            "run_attempt": 1,
            "run_id": 12,
            "runner_id": runner_id,
            "runner_name": runner_name,
            "status": "in_progress",
        }
        evidence = gate.validate_release_runner(
            acceptance_path=acceptance_path,
            owner="emersonfelipesp",
            repository="proxbox-api",
            run_id=12,
            job_name=job_name,
            source_sha="a" * 40,
            token="",
            jobs_payload={"jobs": [job], "total_count": 1},
        )
        assert evidence["runner_id"] == runner_id
        assert observed_scopes[-1] == runner_scope
    acceptance["validation_runner_id"] = acceptance["runner_id"]
    acceptance_path.write_bytes(gate._canonical_json(acceptance))
    with pytest.raises(gate.RunnerGateError, match="not activated"):
        gate.validate_release_runner(
            acceptance_path=acceptance_path,
            owner="emersonfelipesp",
            repository="proxbox-api",
            run_id=12,
            job_name=gate.BUILD_JOB_NAMES["proxbox-api"],
            source_sha="a" * 40,
            token="",
            jobs_payload={"jobs": [], "total_count": 0},
        )


def test_authenticated_release_evidence_rejects_ambient_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci_gate = _load_ci_gate()
    runner_gate = _load_runner_gate()
    for name in tuple(os.environ):
        if name.casefold() in ci_gate.PROXY_ENVIRONMENT_NAMES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    with pytest.raises(ci_gate.CIGateError, match="ambient proxy"):
        ci_gate._request_json("/repos/owner/repository/actions/runs", token="token")
    with pytest.raises(ci_gate.CIGateError, match="ambient proxy"):
        ci_gate._request_github_json("/repos/emersonfelipesp/proxbox-api/actions/runs")
    with pytest.raises(runner_gate.RunnerGateError, match="ambient proxy"):
        runner_gate._request_jobs("owner", "repository", 1, "token")


def test_registry_fetch_rejects_rebinding_original_artifacts_to_moved_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_artifacts = _load_release_artifacts()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "proxbox_api-0.0.20-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "proxbox_api-0.0.20.tar.gz").write_bytes(b"sdist")
    original = release_artifacts.create_manifest(
        dist=dist,
        package="proxbox_api",
        version="0.0.20",
        source_sha="a" * 40,
    )
    monkeypatch.setattr(
        release_artifacts,
        "fetch_gitea_manifest",
        lambda **_kwargs: original,
    )

    with pytest.raises(
        release_artifacts.ReleaseArtifactError,
        match="does not match the protected tag",
    ):
        release_artifacts.fetch_gitea_artifacts(
            owner="emersonfelipesp",
            repository="proxbox-api",
            package="proxbox_api",
            version="0.0.20",
            source_sha="b" * 40,
            dist=tmp_path / "download",
        )


def test_final_release_requires_exact_nms_promotion_evidence(tmp_path: Path) -> None:
    release_artifacts = _load_release_artifacts()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "proxbox_api-0.0.20-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "proxbox_api-0.0.20.tar.gz").write_bytes(b"sdist")
    manifest = release_artifacts.create_manifest(
        dist=dist,
        package="proxbox_api",
        version="0.0.20",
        source_sha="b" * 40,
    )
    evidence = {
        "artifacts": manifest["artifacts"],
        "deploy_source": "latest_package",
        "deployment_run_id": 123,
        "deployment_status": "success",
        "environment": "production",
        "manifest_sha256": release_artifacts.manifest_sha256(manifest),
        "observed_runtime_identity": f"proxbox_api==0.0.20@sha256:{'c' * 64}",
        "package": "proxbox-api",
        "repository": "emersonfelipesp/proxbox-api",
        "schema": 2,
        "source_sha": "b" * 40,
        "target": "proxbox-api",
        "version": "0.0.20",
    }
    assert (
        release_artifacts.validate_release_attestation(
            evidence=evidence,
            manifest=manifest,
            repository="emersonfelipesp/proxbox-api",
        )
        == evidence
    )

    evidence["deploy_source"] = "main_branch"
    with pytest.raises(release_artifacts.ReleaseArtifactError):
        release_artifacts.validate_release_attestation(
            evidence=evidence,
            manifest=manifest,
            repository="emersonfelipesp/proxbox-api",
        )

    evidence["deploy_source"] = "latest_package"
    evidence["observed_runtime_identity"] = "proxbox_api==0.0.20@sha256:short"
    with pytest.raises(release_artifacts.ReleaseArtifactError):
        release_artifacts.validate_release_attestation(
            evidence=evidence,
            manifest=manifest,
            repository="emersonfelipesp/proxbox-api",
        )


def test_netbox_e2e_version_set_matches_supported_plugin_range():
    versions = json.loads(NETBOX_VERSIONS_PATH.read_text(encoding="utf-8"))

    assert versions == [
        "v4.5.8",
        "v4.5.9",
        "v4.6.0",
        "v4.6.1",
        "v4.6.2",
        "v4.6.3",
        "v4.6.4",
        "v4.6.5",
        "v4.6.6",
    ]


def test_pypi_package_validation_happens_before_docker_publish_and_e2e():
    workflow = _read(PUBLISH_WORKFLOW_PATH)

    _assert_order(
        workflow,
        "publish-pypi:",
        "validate-pypi:",
        "publish-docker:",
        "e2e-post-publish:",
    )
    assert "needs: [prepare-release, validate-pypi]" in workflow
    assert "needs: [publish-docker, prepare-release]" in workflow
