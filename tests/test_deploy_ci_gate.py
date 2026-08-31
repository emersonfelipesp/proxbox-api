"""Contracts for the deploy-time CI gate.

N-MultiCloud/nmulticloud-context#204 requirement 6: *"Gate production deployment
on successful verification of the exact deployed SHA. A parallel workflow on the
same push is insufficient."*

`CI` and `Deploy proxbox-api` used to be sibling workflows on the same `push`
event — they raced, and deploy never consulted CI, so neither a red nor a
still-running CI could stop a production rollout. These tests pin both halves of
the fix: the workflow wiring, and the checker's fail-closed behaviour.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".gitea" / "workflows" / "deploy-production.yml"
CHECKER = REPO_ROOT / ".gitea" / "scripts" / "require_ci_status.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("require_ci_status", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def checker():
    return _load_checker()


# --------------------------------------------------------------------------- #
# Workflow wiring
# --------------------------------------------------------------------------- #


def _step_names(job: dict) -> list[str]:
    return [step.get("name", "") for step in job["steps"]]


def test_staging_deploy_depends_on_the_ci_gate(workflow):
    """Staging must not be able to start before the gate passes."""
    jobs = workflow["jobs"]
    assert "verify-ci" in jobs, "the deploy workflow must define a verify-ci gate job"

    needs = jobs["staging"].get("needs")
    needs = [needs] if isinstance(needs, str) else (needs or [])
    assert "verify-ci" in needs, (
        "the staging job must declare `needs: verify-ci`; without it the gate "
        "and the rollout race"
    )


def test_production_gates_itself_between_source_resolution_and_deployment(workflow):
    """Production runs the same gate, in-job, rather than waiting on it.

    It used to declare `needs: verify-ci`. That job is allowed to poll for a CI
    run far longer than a deployment authorization lives, and the authorization's
    clock starts when the deployment is dispatched -- so a queued dispatch, or one
    whose CI turned green late, reached the deploy step with an expired
    authorization and could not deploy at all. The gate itself is not weakened:
    it still runs before either mutation, on the exact resolved SHA.
    """
    production = workflow["jobs"]["production"]

    needs = production.get("needs")
    needs = [needs] if isinstance(needs, str) else (needs or [])
    assert "verify-ci" not in needs, (
        "production must not wait on the polling gate job; it verifies in-job so "
        "the authorization is not spent waiting"
    )

    names = _step_names(production)
    gate = names.index("Require a green CI status for the deployed SHA")
    assert gate > names.index("Resolve the deployed source SHA"), (
        "the gate must run on the resolved SHA, not before it exists"
    )
    for deploy_step in (
        "Deploy exact Gitea package",
        "Deploy the canonical main commit the request authorizes",
    ):
        assert gate < names.index(deploy_step), (
            f"the gate must run before {deploy_step!r}"
        )

    gate_step = production["steps"][gate]
    assert "require_ci_status" in gate_step["run"]
    # Zero wait: the authorization is already ticking by the time this runs.
    assert gate_step["env"]["CI_GATE_TIMEOUT_SECONDS"] == "0"
    # And it must gate the SHA that was resolved, not the workflow's own commit.
    # Gating `github.sha` while deploying a package built from another commit
    # would verify one thing and ship another, and every ordering assertion
    # above would still pass.
    assert gate_step["env"]["DEPLOY_SHA"] == "${{ steps.source.outputs.deploy_sha }}"


def test_production_resolves_the_package_sha_it_gates(workflow):
    """The resolved SHA must come from the tag, not from the workflow commit.

    Exercised rather than pattern-matched: the resolution step is run with a
    stub `git` whose `rev-parse` answers with a package SHA distinct from the
    workflow SHA, and the value it exports is the one the gate consumes.
    """
    steps = workflow["jobs"]["production"]["steps"]
    resolve = next(s for s in steps if s.get("name") == "Resolve the deployed source SHA")

    workflow_sha = "a" * 40
    package_sha = "b" * 40

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        stub_bin = tmp / "bin"
        stub_bin.mkdir()
        git_stub = stub_bin / "git"
        git_stub.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            f'  rev-parse) echo "{package_sha}" ;;\n'
            "  *) : ;;\n"
            "esac\n"
            "exit 0\n"
        )
        git_stub.chmod(0o755)

        github_output = tmp / "output"
        github_env = tmp / "env"
        github_output.touch()
        github_env.touch()

        result = subprocess.run(
            ["bash", "-c", resolve["run"]],
            cwd=tmp,
            env={
                "PATH": f"{stub_bin}:{os.environ['PATH']}",
                "RESOLVED_SOURCE": "latest_package",
                "PACKAGE_VERSION": "1.2.3",
                "WORKFLOW_SHA": workflow_sha,
                "GITHUB_OUTPUT": str(github_output),
                "GITHUB_ENV": str(github_env),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert f"deploy_sha={package_sha}" in github_output.read_text()
        assert workflow_sha not in github_output.read_text()
        assert f"VERIFIED_DEPLOY_SHA={package_sha}" in github_env.read_text()


def test_gate_runs_on_a_trusted_runner(workflow):
    """The gate reads an API token — it must not run on the untrusted CI runner."""
    runs_on = workflow["jobs"]["verify-ci"]["runs-on"]
    assert runs_on == "prod-deploy", (
        "verify-ci handles a token and gates production; it must stay on the "
        f"trusted deploy runner, not {runs_on!r}"
    )
    assert runs_on != "ci-untrusted-python312"


def test_gate_verifies_an_exact_sha(workflow):
    """The gate must be pinned to github.sha, not a branch name."""
    resolve_step = next(
        s
        for s in workflow["jobs"]["verify-ci"]["steps"]
        if "release-policy/candidate" in str(s.get("run", ""))
    )
    gate_step = next(
        s
        for s in workflow["jobs"]["verify-ci"]["steps"]
        if "require_ci_status" in str(s.get("run", ""))
    )
    assert "git rev-parse refs/release-policy/candidate^{commit}" in resolve_step["run"]
    assert gate_step["env"]["DEPLOY_SHA"] == "${{ steps.source.outputs.deploy_sha }}"
    # Production resolves and pins its own SHA now that it no longer inherits
    # one from this job.
    assert "VERIFIED_DEPLOY_SHA" in str(workflow["jobs"]["production"])


def test_emergency_bypass_exists_and_defaults_off(workflow):
    """An incident rollback must not be locked out — but never silently."""
    triggers = workflow.get("on") or workflow.get(True)
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "skip_ci_gate" in inputs, (
        "keep an explicit escape hatch so a rollback to a known-good older SHA "
        "is possible during an incident"
    )
    assert inputs["skip_ci_gate"]["default"] is False


# --------------------------------------------------------------------------- #
# Checker behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([{"context": "CI", "status": "success"}], {"CI": "success"}),
        ({"statuses": [{"context": "CI", "status": "pending"}]}, {"CI": "pending"}),
        # Gitea returns newest-first, so the first entry per context wins.
        (
            [
                {"context": "C", "status": "success"},
                {"context": "C", "status": "failure"},
            ],
            {"C": "success"},
        ),
        # Anything unexpected degrades to "nothing known", which fails closed.
        ("nonsense", {}),
        (None, {}),
        ([1, 2, 3], {}),
    ],
)
def test_latest_status_by_context(checker, payload, expected):
    assert checker.latest_status_by_context(payload) == expected


def test_skip_flag_allows_deploy(checker, monkeypatch, capsys):
    monkeypatch.setenv("SKIP_CI_GATE", "true")
    monkeypatch.setenv("DEPLOY_SHA", "a" * 40)

    assert checker.main() == 0
    assert "skipped" in capsys.readouterr().out


def test_unreadable_api_fails_closed(checker, monkeypatch):
    """No status must never mean "go ahead"."""
    monkeypatch.delenv("SKIP_CI_GATE", raising=False)
    monkeypatch.setenv("REQUIRED_CI_CONTEXT", "CI / whatever")
    # Port 9 (discard) refuses connections immediately.
    monkeypatch.setenv("API_BASE", "http://127.0.0.1:9/api/v1/repos/x/y")
    monkeypatch.setenv("DEPLOY_SHA", "a" * 40)
    monkeypatch.setenv("CI_GATE_TIMEOUT_SECONDS", "0")

    assert checker.main() == 1


def test_branch_ref_is_refused(checker):
    """A dispatch ref that is not an exact SHA cannot be verified."""
    with pytest.raises(SystemExit):
        checker.resolve_sha("a" * 40, "main")


def test_full_sha_ref_is_accepted(checker):
    assert checker.resolve_sha("a" * 40, "b" * 40) == "b" * 40
