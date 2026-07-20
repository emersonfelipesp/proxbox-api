"""Static coverage-gate contract for the required CI job."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_required_ci_job_invokes_coverage() -> None:
    workflow = yaml.safe_load(_read(CI_WORKFLOW_PATH))
    test_steps = {
        step["name"]: step for step in workflow["jobs"]["test"]["steps"] if "name" in step
    }
    coverage_command = test_steps["Core tests with coverage"]["run"]

    assert "--cov=proxbox_api" in coverage_command
    assert "--cov-branch" in coverage_command
    assert "--cov-report=term-missing" in coverage_command
    assert "--cov-report=xml:coverage.xml" in coverage_command


def test_coverage_fail_under_is_repository_owned() -> None:
    config = tomllib.loads(_read(PYPROJECT_PATH))
    fail_under = config["tool"]["coverage"]["report"].get("fail_under")

    assert isinstance(fail_under, (int, float))
    assert fail_under > 0
