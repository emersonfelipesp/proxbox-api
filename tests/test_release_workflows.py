"""Static release workflow contracts.

These checks keep the package publication pipeline aligned with the staged
TestPyPI -> PyPI release process without running a publishing workflow.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
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
CI_MATRIX_PATH = REPO_ROOT / "scripts" / "ci_e2e_matrix.py"
CI_GATE_PATH = REPO_ROOT / "scripts" / "gitea_ci_gate.py"


def _load_release_artifacts():
    spec = importlib.util.spec_from_file_location("release_artifacts", RELEASE_ARTIFACTS_PATH)
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

    parsed = yaml.load(workflow, Loader=yaml.BaseLoader)
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


def test_gitea_publish_is_single_triggered_and_promotes_only_rc_tags():
    workflow = _read(GITEA_PUBLISH_WORKFLOW_PATH)

    assert "  create:" not in workflow
    assert "needs.validate-source.outputs.is_rc == 'true'" in workflow
    assert "Create or publish GitHub Release" not in workflow
    assert "gh release create" not in workflow
    assert "refs/heads/develop:refs/remotes/gitea/release-develop" in workflow
    assert "release-manifest.json" in workflow
    assert "fetch-gitea" in workflow
    assert "scripts/gitea_ci_gate.py" in workflow
    assert "/commits/${SOURCE_SHA}/statuses" not in workflow
    assert "scripts/release_artifacts.py release-transfer/" in workflow
    assert "release-transfer/release_artifacts.py" in workflow
    assert "/-/link/proxbox-api" in workflow
    _assert_order(workflow, "publish-manifest", "fetch-gitea")
    assert "actions/upload-artifact@c6a3b2bd78b3985e4b2f15397fec357f0fd808de" in workflow
    assert "actions/download-artifact@ad191675b41f6a5b46da9a048cb6893812da158b" in workflow
    parsed = yaml.safe_load(workflow)
    assert all(job["runs-on"] == "ci-untrusted-python312" for job in parsed["jobs"].values())
    assert parsed["jobs"]["publish-gitea"]["permissions"] == {
        "contents": "read",
        "packages": "read",
    }
    assert parsed["jobs"]["verify-candidate"]["needs"] == [
        "validate-source",
        "build-candidate",
    ]
    assert parsed["jobs"]["publish-gitea"]["needs"] == [
        "validate-source",
        "verify-candidate",
    ]
    assert parsed["jobs"]["verify-registry"]["needs"] == [
        "validate-source",
        "publish-gitea",
    ]
    assert parsed["jobs"]["push-to-github"]["needs"] == [
        "validate-source",
        "verify-registry",
    ]
    assert "astral-sh/setup-uv@" not in workflow
    assert "RUNNER_TOOL_CACHE" not in workflow
    assert "UV_MANAGED_PYTHON" not in workflow
    assert "mirror-host" not in workflow
    assert "packages: write" not in workflow
    assert (
        workflow.count(
            "https://github.com/astral-sh/uv/releases/download/0.11.28/"
            "uv-x86_64-unknown-linux-gnu.tar.gz"
        )
        == 3
    )
    assert workflow.count("e490a6464492183c5d4534a5527fb4440f7f2bb2f228162ad7e4afe076dc0224") == 3
    assert workflow.count("UV_PYTHON_INSTALL_DIR=%s") == 3
    assert workflow.count("while IFS='=' read -r VARIABLE _; do") == 6
    assert workflow.count('case "$VARIABLE" in UV_*) unset "$VARIABLE" ;; esac') == 6
    assert workflow.count("--no-config") == 6
    assert workflow.count("--managed-python") == 6
    assert workflow.count("--no-python-downloads") == 3
    assert workflow.count('"$MANAGED_PYTHON_ROOT"/*) ;;') == 3
    assert "GITEA_PACKAGE_TOKEN: ${{ secrets.PKG_TOKEN }}" in workflow
    assert "TWINE_PASSWORD: ${{ secrets.PKG_TOKEN }}" in workflow
    assert "GITEA_TOKEN: ${{ github.token }}" not in workflow
    assert "Fetch validated publisher tool anonymously" in workflow
    assert '"$PUBLISHER_UV" sync' in workflow
    assert '"$PUBLISHER_PYTHON" -m twine upload' in workflow
    assert '"$BUILD_ROOT/venv/bin/python" -m build --no-isolation' in workflow
    assert "uvx --from twine" not in workflow
    assert "--password" not in workflow
    assert "--username" not in workflow
    assert "Authorization: token" not in workflow
    assert "http.https://github.com/.extraheader" not in workflow
    assert '--netrc-file "$SECRET_ROOT/netrc"' in workflow
    assert 'GIT_ASKPASS="$SECRET_ROOT/askpass"' in workflow
    assert "sealed-private-release-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "sha256sum -- *.whl *.tar.gz" in workflow
    assert workflow.count("sha256sum --check --strict SHA256SUMS") == 3

    pkg_token_steps = [
        (job_name, step)
        for job_name, job in parsed["jobs"].items()
        for step in job.get("steps", [])
        if "${{ secrets.PKG_TOKEN }}" in yaml.safe_dump(step)
    ]
    assert len(pkg_token_steps) == 1
    job_name, pkg_token_step = pkg_token_steps[0]
    assert job_name == "publish-gitea"
    assert pkg_token_step["name"] == "Publish exact files with package-only authority"
    assert pkg_token_step["env"] == {
        "GITEA_PACKAGE_TOKEN": "${{ secrets.PKG_TOKEN }}",
        "TWINE_PASSWORD": "${{ secrets.PKG_TOKEN }}",
        "TWINE_USERNAME": "emersonfelipesp",
    }


def test_gitea_artifact_v3_compatibility_probe_is_bounded_and_disposable():
    workflow = _read(GITEA_ARTIFACT_WORKFLOW_PATH)
    parsed = yaml.load(workflow, Loader=yaml.BaseLoader)

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


def test_ci_gate_binds_status_to_authenticated_run_and_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _load_ci_gate()
    sha = "a" * 40
    context = "CI / Lint, smoke, and core coverage (push)"
    responses = {
        f"/repos/emersonfelipesp/proxbox-api/commits/{sha}/statuses?limit=100": [
            {
                "id": 8,
                "context": context,
                "status": "success",
                "target_url": "/emersonfelipesp/proxbox-api/actions/runs/12/jobs/34",
            }
        ],
        "/repos/emersonfelipesp/proxbox-api/actions/runs/12": {
            "id": 12,
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_sha": sha,
            "head_branch": "develop",
            "path": "ci.yml@refs/heads/develop",
            "actor": {"login": "emersonfelipesp"},
        },
        "/repos/emersonfelipesp/proxbox-api/actions/jobs/34": {
            "id": 34,
            "run_id": 12,
            "name": "Lint, smoke, and core coverage",
            "status": "completed",
            "conclusion": "success",
            "head_sha": sha,
            "runner_name": "ci-untrusted-proxbox-api",
            "labels": ["ci-untrusted-python312"],
            "html_url": "https://git.nmulti.cloud/emersonfelipesp/proxbox-api/actions/runs/12/jobs/34",
        },
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
    assert evidence == {context: {"job_id": 34, "run_id": 12}}

    responses["/repos/emersonfelipesp/proxbox-api/actions/runs/12"]["event"] = "pull_request"
    with pytest.raises(gate.CIGateError, match="run does not match"):
        gate.validate_ci_gate(
            owner="emersonfelipesp",
            repository="proxbox-api",
            source_sha=sha,
            required_contexts=[context],
            trusted_actor="emersonfelipesp",
            token="test-token",
        )


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
