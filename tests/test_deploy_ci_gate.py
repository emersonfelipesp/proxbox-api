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
import pathlib

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


def test_deploy_depends_on_the_ci_gate(workflow):
    """Deploy must not be able to start before the gate passes."""
    jobs = workflow["jobs"]
    assert "verify-ci" in jobs, "the deploy workflow must define a verify-ci gate job"

    needs = jobs["deploy"].get("needs")
    needs = [needs] if isinstance(needs, str) else (needs or [])
    assert "verify-ci" in needs, (
        "the deploy job must declare `needs: verify-ci`; without it the gate and "
        "the rollout race, which is the exact defect #204 requirement 6 describes"
    )


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
    step = next(
        s
        for s in workflow["jobs"]["verify-ci"]["steps"]
        if "require_ci_status" in str(s.get("run", ""))
    )
    assert step["env"]["DEPLOY_SHA"] == "${{ github.sha }}", (
        "the gate must verify the exact commit being deployed; a branch name "
        "could move between verification and rollout"
    )


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
