"""Static release workflow contracts.

These checks keep the package publication pipeline aligned with the staged
TestPyPI -> PyPI release process without running a publishing workflow.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-testpypi.yml"


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
    assert publish_workflow.count("for i in $(seq 1 600); do") >= 2
    assert publish_workflow.count("NetBox API did not become ready") >= 2


def test_ci_downloads_netbox_image_artifact_only_when_registry_pull_fails():
    workflow = _read(CI_WORKFLOW_PATH)
    e2e_image_block = workflow.split("Resolve NetBox image source", 1)[1]

    _assert_order(
        e2e_image_block,
        'if docker pull "${NETBOX_IMAGE}" 2>/dev/null; then',
        "Download NetBox image artifact (if built from source)",
        "docker load < /tmp/netbox-image.tar.gz",
        "Start E2E stack",
    )
    assert "if: steps.netbox_image.outputs.source == 'artifact'" in workflow
    download_step = workflow.split("Download NetBox image artifact (if built from source)", 1)[1]
    download_step = download_step.split("Load NetBox image from artifact", 1)[0]
    assert "continue-on-error: true" not in download_step


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
