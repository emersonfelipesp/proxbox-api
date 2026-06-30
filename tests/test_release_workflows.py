"""Static release workflow contracts.

These checks keep the package publication pipeline aligned with the staged
TestPyPI -> PyPI release process without running a publishing workflow.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-testpypi.yml"
NETBOX_VERSIONS_PATH = REPO_ROOT / ".github" / "netbox-versions.json"


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
    assert ci_workflow.count('--build-arg "FROM=ubuntu:26.04"') == 1
    assert publish_workflow.count('--build-arg "FROM=ubuntu:26.04"') == 2


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
