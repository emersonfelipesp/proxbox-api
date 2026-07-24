"""Static release workflow contracts.

These checks keep the package publication pipeline aligned with the staged
TestPyPI -> PyPI release process without running a publishing workflow.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GITEA_CI_WORKFLOW_PATH = REPO_ROOT / ".gitea" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-testpypi.yml"
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

    coverage_command = steps["Core tests with coverage"]["run"]
    assert "--ignore=tests/e2e" in coverage_command
    assert "--ignore=tests/test_generated_proxmox_routes.py" in coverage_command
    assert "--cov=proxbox_api" in coverage_command
    assert "--cov-branch" in coverage_command
    assert "--cov-report=term-missing" in coverage_command
    assert "--cov-report=xml:coverage.xml" in coverage_command

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


def test_publish_workflow_routes_tags_to_testpypi_and_rcs_to_pypi():
    workflow = _read(PUBLISH_WORKFLOW_PATH)

    assert "publish_target = 'testpypi'" in workflow
    assert "elif event == 'release':" in workflow
    assert "re.search(r'rc\\d+$', version)" in workflow
    assert "--repository-url https://test.pypi.org/legacy/" in workflow
    assert "--repository-url https://upload.pypi.org/legacy/" in workflow


def test_publish_workflow_never_reuses_consumed_package_versions():
    workflow = _read(PUBLISH_WORKFLOW_PATH)

    assert "--skip-existing" not in workflow


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
