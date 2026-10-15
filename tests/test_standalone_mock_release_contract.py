"""Static and executable contracts for standalone mock package-first releases."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import runpy
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MOCK_ROOT = ROOT / "proxmox-mock"
MANIFEST_PATH = MOCK_ROOT / "release/release-manifest.toml"
PACKAGE_WORKFLOW_PATH = ROOT / ".github/workflows/publish-proxmox-mock.yml"
IMAGE_WORKFLOW_PATH = ROOT / ".github/workflows/publish-proxmox-mock-image.yml"
CONTRACT = runpy.run_path(str(MOCK_ROOT / "release/release_contract.py"))
ReleaseContractError = CONTRACT["ReleaseContractError"]
VARIABLE_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
PINNED_ACTIONS = {
    "actions/checkout": "de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-python": "a309ff8b426b58ec0e2a45f0f869d46889d02405",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "astral-sh/setup-uv": "11f9893b081a58869d3b5fccaea48c9e9e46f990",
    "docker/setup-qemu-action": "99012661954931238ded8c8b007157a8430204e1",
    "docker/setup-buildx-action": "f87e5991a6d7451dcb8d9637bfbc97413f497069",
    "docker/login-action": "dbcb813823bdd20940b903addbd779551569679f",
    "docker/build-push-action": "c3c9e263c25d99ce0380d002d59b67737d91b0dc",
}


class FakeDistribution:
    """Minimal installed-distribution fixture for provenance checks."""

    def __init__(self, name: str, version: str, record: str, direct_url: str | None = None):
        self.metadata = {"Name": name}
        self.version = version
        self.record = record
        self.direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        """Return the metadata files used by the release validator."""
        if filename == "RECORD":
            return self.record
        if filename == "direct_url.json":
            return self.direct_url
        return None


def load_workflow(path: Path) -> dict:
    """Parse workflow YAML without YAML 1.1 coercion of the `on` key."""
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def load_manifest() -> dict:
    """Parse the checked-in release manifest."""
    with MANIFEST_PATH.open("rb") as stream:
        return tomllib.load(stream)


def complete_manifest() -> dict:
    """Return a syntactically complete manifest for mutation tests."""
    manifest = copy.deepcopy(load_manifest())
    manifest["requirements_sha256"] = "a" * 64
    manifest["wheel"]["sha256"] = "b" * 64
    replacements = {
        "linux/amd64": ("3.13.14", "c", "d", "e", "f"),
        "linux/arm64": ("3.13.14", "1", "2", "3", "4"),
    }
    for platform_name, (patch, base, uv, graph, provenance) in replacements.items():
        row = manifest["platforms"][platform_name]
        row["base_image"] = f"python:{patch}-slim-bookworm@sha256:{base * 64}"
        row["uv_image"] = f"ghcr.io/astral-sh/uv:0.12.5@sha256:{uv * 64}"
        row["wheel_graph_sha256"] = graph * 64
        row["installed_provenance_sha256"] = provenance * 64
    return manifest


def docker_instructions(body: str) -> list[str]:
    """Parse logical Dockerfile instructions while ignoring comments and blank lines."""
    instructions = []
    current = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        current.append(line[:-1].rstrip() if continued else line)
        if not continued:
            instructions.append(" ".join(current))
            current = []
    assert not current, "unterminated Dockerfile instruction"
    return instructions


def referenced_variables(instruction: str) -> set[str]:
    """Return shell-style variable names referenced by one Docker instruction."""
    return {left or right for left, right in VARIABLE_RE.findall(instruction)}


def assert_stage_args_scoped(body: str) -> None:
    """Require global build arguments to be redeclared before stage-local use."""
    global_args = set()
    stage_args: set[str] | None = None
    for instruction in docker_instructions(body):
        operation, _, value = instruction.partition(" ")
        if operation.upper() == "FROM":
            stage_args = set()
        elif operation.upper() == "ARG":
            name = value.split("=", 1)[0].strip()
            (global_args if stage_args is None else stage_args).add(name)
        elif stage_args is not None:
            missing = referenced_variables(instruction) & global_args - stage_args
            assert not missing, f"stage uses global ARG without redeclaration: {sorted(missing)}"


def expect_release_error(action: Callable[[], None]) -> None:
    """Require an executable release guard to reject a mutated input."""
    try:
        action()
    except ReleaseContractError:
        return
    raise AssertionError("release guard accepted a mutated input")


def context_fixture(tmp_path: Path) -> tuple[dict[str, Any], str, Path, Path, Path]:
    """Create a valid wheel, requirements export, and dependency wheel graph."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest = complete_manifest()
    platform_name = "linux/amd64"
    wheel = tmp_path / manifest["wheel"]["filename"]
    requirements = tmp_path / "requirements.lock"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel.write_bytes(b"qualified application wheel")
    requirements.write_bytes(b"dependency==1.0 --hash=sha256:" + b"1" * 64 + b"\n")
    (wheelhouse / "dependency-1.0-py3-none-any.whl").write_bytes(b"dependency wheel")
    manifest["wheel"]["sha256"] = CONTRACT["sha256_file"](wheel)
    manifest["requirements_sha256"] = CONTRACT["sha256_file"](requirements)
    manifest["platforms"][platform_name]["wheel_graph_sha256"] = CONTRACT["wheelhouse_digest"](
        wheelhouse
    )
    return manifest, platform_name, wheel, requirements, wheelhouse


def assert_verify_context_contract(verifier: Callable[..., None], tmp_path: Path) -> None:
    """Exercise valid context acceptance and every authenticated input boundary."""
    manifest, platform_name, wheel, requirements, wheelhouse = context_fixture(tmp_path)
    verifier(manifest, platform_name, wheel, requirements, wheelhouse)

    wheel.write_bytes(b"changed application wheel")
    expect_release_error(lambda: verifier(manifest, platform_name, wheel, requirements, wheelhouse))
    wheel.write_bytes(b"qualified application wheel")

    requirements.write_bytes(b"changed requirements")
    expect_release_error(lambda: verifier(manifest, platform_name, wheel, requirements, wheelhouse))
    requirements.write_bytes(b"dependency==1.0 --hash=sha256:" + b"1" * 64 + b"\n")

    (wheelhouse / "unexpected-1.0-py3-none-any.whl").write_bytes(b"unexpected wheel")
    expect_release_error(lambda: verifier(manifest, platform_name, wheel, requirements, wheelhouse))


def installed_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], str, FakeDistribution]:
    """Create deterministic installed metadata for provenance verification."""
    platform_name = "linux/amd64"
    application = FakeDistribution("proxmox-mock-api", "0.0.6.post5", "app.py,sha256=1,1")
    dependency = FakeDistribution("dependency", "1.0", "dependency.py,sha256=2,2")
    metadata = CONTRACT["verify_installed"].__globals__["importlib"].metadata
    monkeypatch.setattr(metadata, "version", lambda _name: application.version)
    monkeypatch.setattr(metadata, "distribution", lambda _name: application)
    monkeypatch.setattr(metadata, "distributions", lambda: [application, dependency])
    manifest = complete_manifest()
    manifest["platforms"][platform_name]["installed_provenance_sha256"] = CONTRACT[
        "installed_provenance"
    ](platform_name)
    return manifest, platform_name, application


def assert_verify_installed_contract(
    verifier: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise valid installed provenance, editable rejection, and graph drift."""
    with monkeypatch.context() as scoped:
        manifest, platform_name, application = installed_fixture(scoped)
        verifier(manifest, platform_name)
        application.direct_url = json.dumps({"dir_info": {"editable": True}})
        expect_release_error(lambda: verifier(manifest, platform_name))
        application.direct_url = None
        application.record = "changed.py,sha256=3,3"
        expect_release_error(lambda: verifier(manifest, platform_name))


def assert_uploads_reject_collisions(workflow: dict[str, Any]) -> None:
    """Require both package indexes to reject an already-used version."""
    upload_commands = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "twine upload" in step.get("run", "")
    ]
    assert len(upload_commands) == 2
    assert all("--skip-existing" not in command for command in upload_commands)


def action_references(value: object) -> list[str]:
    """Collect every action reference from parsed workflow data."""
    if isinstance(value, dict):
        references = [
            item for key, item in value.items() if key == "uses" and isinstance(item, str)
        ]
        for item in value.values():
            references.extend(action_references(item))
        return references
    if isinstance(value, list):
        return [reference for item in value for reference in action_references(item)]
    return []


def assert_remote_actions_pinned(workflow: dict[str, Any]) -> None:
    """Require every remote action to use its reviewed full commit SHA."""
    for uses in action_references(workflow):
        if uses.startswith("./"):
            continue
        action, separator, reference = uses.partition("@")
        assert separator and FULL_SHA_RE.fullmatch(reference), f"unpinned action: {uses}"
        if action in PINNED_ACTIONS:
            assert reference == PINNED_ACTIONS[action]


def assert_release_dockerfile(body: str) -> None:
    """Assert the minimum package-first Docker contract."""
    instructions = docker_instructions(body)
    parsed = "\n".join(instructions)
    required = (
        "FROM ${UV_IMAGE} AS uv-source",
        "FROM ${PYTHON_BASE_IMAGE} AS runtime",
        "COPY release/context/ /release/",
        '--platform "${TARGETPLATFORM}" --kind base)',
        '--platform "${TARGETPLATFORM}" --kind uv)',
        "wheel-filename",
        "verify-context",
        "--offline --no-index --require-hashes",
        "application-wheel.txt",
        "verify-installed",
    )
    for token in required:
        assert token in parsed
    for forbidden in ("pip install .", "COPY proxmox_mock", "COPY pyproject.toml", ":latest"):
        assert forbidden not in parsed
    assert re.search(r"proxmox_mock_api-[0-9]", parsed) is None


def test_checked_in_manifest_fails_closed_on_placeholders() -> None:
    with pytest.raises(ReleaseContractError, match="populated|immutable"):
        CONTRACT["validate_manifest"](load_manifest(), "0.0.6.post5")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("wheel", "sha256"), "A" * 64),
        (("platforms", "linux/amd64", "base_image"), "python:3.13-slim-bookworm"),
        (("platforms", "linux/arm64", "wheel_graph_sha256"), "1234"),
    ],
)
def test_manifest_rejects_malformed_digests(path: tuple[str, ...], value: str) -> None:
    manifest = complete_manifest()
    target = manifest
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ReleaseContractError):
        CONTRACT["validate_manifest"](manifest, "0.0.6.post5")


def test_manifest_rejects_missing_platform() -> None:
    manifest = complete_manifest()
    del manifest["platforms"]["linux/arm64"]
    with pytest.raises(ReleaseContractError, match="exactly linux/amd64 and linux/arm64"):
        CONTRACT["validate_manifest"](manifest, "0.0.6.post5")


def test_manifest_and_dispatch_reject_version_mismatch() -> None:
    manifest = complete_manifest()
    with pytest.raises(ReleaseContractError, match="manifest version"):
        CONTRACT["validate_manifest"](manifest, "0.0.6.post6")
    with pytest.raises(ReleaseContractError, match="requested version"):
        CONTRACT["validate_release"](MANIFEST_PATH, MOCK_ROOT / "pyproject.toml", "0.0.6.post6")


def test_complete_manifest_accepts_both_glibc_platforms() -> None:
    CONTRACT["validate_manifest"](complete_manifest(), "0.0.6.post5")


def test_release_event_never_builds_or_publishes_an_image() -> None:
    workflow = load_workflow(PACKAGE_WORKFLOW_PATH)
    assert workflow["on"] == {"release": {"types": ["published"]}}
    assert "publish-docker" not in workflow["jobs"]
    serialized = json.dumps(workflow)
    assert "docker/build-push-action" not in serialized
    assert "docker/login-action" not in serialized
    assert "platforms: linux/amd64,linux/arm64" not in serialized
    assert "docker" not in serialized.lower()


def test_manual_image_dispatch_is_version_bound_and_approval_gated() -> None:
    workflow = load_workflow(IMAGE_WORKFLOW_PATH)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    version = workflow["on"]["workflow_dispatch"]["inputs"]["version"]
    assert version["required"] == "true"
    assert version["type"] == "string"
    for job in workflow["jobs"].values():
        assert job["environment"] == "standalone-mock-production"
    validate_step = next(
        step
        for step in workflow["jobs"]["prepare-image-inputs"]["steps"]
        if step.get("name") == "Require an exact version and a fully populated manifest"
    )
    assert "release_contract.py validate" in validate_step["run"]
    assert '--version "${VERSION}"' in validate_step["run"]
    platforms = {
        row["platform"]
        for row in workflow["jobs"]["publish-platform"]["strategy"]["matrix"]["include"]
    }
    assert platforms == {"linux/amd64", "linux/arm64"}


def test_workflows_pin_uv_and_build_versions() -> None:
    manifest = load_manifest()
    workflows = [load_workflow(path) for path in (PACKAGE_WORKFLOW_PATH, IMAGE_WORKFLOW_PATH)]
    setup_uv_steps = [
        step
        for workflow in workflows
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]
    assert setup_uv_steps
    assert all(
        step["with"]["version"] == manifest["tools"]["uv_version"] for step in setup_uv_steps
    )
    build_token = f"build=={manifest['tools']['build_version']}"
    build_steps = [
        step["run"]
        for workflow in workflows
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if build_token in step.get("run", "")
    ]
    assert len(build_steps) == 1
    image_commands = json.dumps(workflows[1])
    assert "python -m build" not in image_commands
    assert "pip download --no-deps --only-binary=:all:" in image_commands
    assert "uv export --project proxmox-mock --frozen --no-dev --no-emit-project" in image_commands
    assert "--no-hashes" not in image_commands
    assert "pip download --require-hashes --only-binary=:all:" in image_commands
    assert "verify-context" in image_commands


def test_every_remote_release_action_uses_a_reviewed_commit_pin() -> None:
    for path in (PACKAGE_WORKFLOW_PATH, IMAGE_WORKFLOW_PATH):
        assert_remote_actions_pinned(load_workflow(path))


@pytest.mark.parametrize("reference", ["docker/login-action@v4", "unreviewed/action@main"])
def test_action_pin_guard_rejects_mutated_docker_login_reference(reference: str) -> None:
    workflow = load_workflow(IMAGE_WORKFLOW_PATH)
    login = next(
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("docker/login-action@")
    )
    login["uses"] = reference
    with pytest.raises(AssertionError, match="unpinned action"):
        assert_remote_actions_pinned(workflow)


def test_image_index_consumes_both_current_run_platform_digests() -> None:
    workflow = load_workflow(IMAGE_WORKFLOW_PATH)
    platform_job = workflow["jobs"]["publish-platform"]
    build = next(step for step in platform_job["steps"] if step.get("id") == "build")
    assert build["uses"] == ("docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc")
    platform_steps = json.dumps(platform_job["steps"])
    assert "steps.build.outputs.digest" in platform_steps
    assert "proxmox-mock-image-digest-${{ matrix.suffix }}" in platform_steps

    index_job = workflow["jobs"]["publish-index"]
    assert index_job["needs"] == "publish-platform"
    artifact_names = {
        step["with"]["name"]
        for step in index_job["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    }
    assert artifact_names == {
        "proxmox-mock-image-digest-amd64",
        "proxmox-mock-image-digest-arm64",
    }
    commands = "\n".join(step.get("run", "") for step in index_job["steps"])
    assert "emersonfelipesp/proxmox-mock-api@${AMD64_DIGEST}" in commands
    assert "emersonfelipesp/proxmox-mock-api@${ARM64_DIGEST}" in commands
    assert "${VERSION}-amd64" not in commands
    assert "${VERSION}-arm64" not in commands


def test_package_uploads_fail_on_existing_version_collisions() -> None:
    workflow = load_workflow(PACKAGE_WORKFLOW_PATH)
    assert_uploads_reject_collisions(workflow)
    mutated = copy.deepcopy(workflow)
    upload = next(
        step
        for step in mutated["jobs"]["publish-pypi"]["steps"]
        if "twine upload" in step.get("run", "")
    )
    upload["run"] += "\n--skip-existing"
    with pytest.raises(AssertionError):
        assert_uploads_reject_collisions(mutated)


def test_uploaded_wheel_validation_is_neutral_and_not_a_source_sync() -> None:
    workflow = load_workflow(PACKAGE_WORKFLOW_PATH)
    job = workflow["jobs"]["validate-testpypi"]
    assert not any(step.get("uses", "").startswith("actions/checkout@") for step in job["steps"])
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    artifacts = {
        step["with"]["name"]
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    }
    prepare = json.dumps(workflow["jobs"]["prepare-release"])
    assert "pip download --no-deps --only-binary=:all:" in commands
    assert '"uploaded-wheel/proxmox_mock_api-${VERSION}-py3-none-any.whl[test]"' in commands
    assert 'cd "${RUNNER_TEMP}"' in commands
    assert '"proxmox_mock.routes"' in commands
    assert "-I -m pytest" in commands
    assert artifacts == {"proxmox-mock-dist", "proxmox-mock-tests"}
    assert "proxmox-mock/tests/" in prepare
    assert "uv sync" not in commands


def test_release_dockerfile_consumes_only_qualified_wheels() -> None:
    assert_release_dockerfile((MOCK_ROOT / "Dockerfile").read_text())


def test_release_dockerfile_guard_ignores_commented_instructions() -> None:
    body = (MOCK_ROOT / "Dockerfile").read_text()
    commented = "\n".join(f"# {instruction}" for instruction in docker_instructions(body))
    with pytest.raises(AssertionError):
        assert_release_dockerfile(commented)


@pytest.mark.parametrize(
    "token",
    [
        "COPY release/context/ /release/",
        '--platform "${TARGETPLATFORM}" --kind uv)',
        "wheel-filename",
        "--offline --no-index --require-hashes",
        "application-wheel.txt",
        "verify-installed",
    ],
)
def test_dockerfile_guard_detects_key_mutations(token: str) -> None:
    body = (MOCK_ROOT / "Dockerfile").read_text()
    with pytest.raises(AssertionError):
        assert_release_dockerfile(body.replace(token, "", 1))


def test_release_dockerfile_is_version_neutral(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = complete_manifest()
    manifest["version"] = "0.0.6.post6"
    manifest["wheel"]["filename"] = "proxmox_mock_api-0.0.6.post6-py3-none-any.whl"
    CONTRACT["validate_manifest"](manifest, "0.0.6.post6")
    assert CONTRACT["wheel_filename"](manifest) == manifest["wheel"]["filename"]
    contract_globals = CONTRACT["main"].__globals__
    monkeypatch.setitem(contract_globals, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        contract_globals["sys"],
        "argv",
        ["release_contract.py", "wheel-filename", "--manifest", "release-manifest.toml"],
    )
    CONTRACT["main"]()
    assert capsys.readouterr().out.strip() == manifest["wheel"]["filename"]
    body = (MOCK_ROOT / "Dockerfile").read_text()
    assert "0.0.6.post5" not in "\n".join(docker_instructions(body))
    assert_release_dockerfile(body)


def test_dockerfiles_redeclare_global_args_before_stage_use() -> None:
    for filename in ("Dockerfile", "Dockerfile.dev"):
        assert_stage_args_scoped((MOCK_ROOT / filename).read_text())


def test_dockerfile_scope_guard_detects_missing_redeclaration() -> None:
    body = (MOCK_ROOT / "Dockerfile.dev").read_text()
    mutated = body.replace(
        "FROM ${PYTHON_BASE_IMAGE}\n\nARG UV_VERSION", "FROM ${PYTHON_BASE_IMAGE}", 1
    )
    with pytest.raises(AssertionError, match="UV_VERSION"):
        assert_stage_args_scoped(mutated)


def test_release_context_verification_authenticates_every_input(tmp_path: Path) -> None:
    assert_verify_context_contract(CONTRACT["verify_context"], tmp_path)


def test_installed_verification_rejects_editable_and_provenance_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_verify_installed_contract(CONTRACT["verify_installed"], monkeypatch)


def test_executable_release_guards_detect_noop_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AssertionError, match="mutated input"):
        assert_verify_context_contract(lambda *_args: None, tmp_path / "context")
    with pytest.raises(AssertionError, match="mutated input"):
        assert_verify_installed_contract(lambda *_args: None, monkeypatch)


def test_ci_uses_the_lock_backed_development_dockerfile() -> None:
    workflow = load_workflow(ROOT / ".github/workflows/ci.yml")
    commands = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["prepare-proxmox-image"]["steps"]
    )
    assert "docker build -f proxmox-mock/Dockerfile.dev" in commands


def test_wheelhouse_digest_binds_names_sizes_and_bytes(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "dependency-1.0-py3-none-any.whl"
    wheel.write_bytes(b"first")
    first = CONTRACT["wheelhouse_digest"](wheelhouse)
    wheel.write_bytes(b"second")
    second = CONTRACT["wheelhouse_digest"](wheelhouse)
    assert first != second
    assert (
        first
        == hashlib.sha256(
            json.dumps(
                [
                    {
                        "filename": wheel.name,
                        "sha256": hashlib.sha256(b"first").hexdigest(),
                        "size": 5,
                    }
                ],
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
