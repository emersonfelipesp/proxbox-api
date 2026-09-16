"""Regression tests for promotion ancestor-blob validation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPOSITORY_ROOT / "scripts" / "check_promotion_ancestor_blobs.py"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _commit_file(repository: Path, path: str, content: str, message: str) -> str:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repository, "add", "--", path)
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def history_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Promotion Guard Test")
    _git(repository, "config", "user.email", "promotion-guard@example.invalid")
    return repository


def _run_guard(repository: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(GUARD),
            "--repository",
            str(repository),
            "--base",
            base,
            "--head",
            head,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_guard_rejects_blob_superseded_on_base(history_repository: Path) -> None:
    _commit_file(history_repository, "factory.py", "first\n", "first version")
    base = _commit_file(history_repository, "factory.py", "current\n", "current version")
    head = _commit_file(history_repository, "factory.py", "first\n", "stale promotion")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 1
    assert 'path="factory.py"' in result.stderr
    assert "Promotion ancestor-blob regression" in result.stderr


def test_guard_accepts_new_blob_and_ignores_deletion(history_repository: Path) -> None:
    _commit_file(history_repository, "changed.py", "first\n", "first version")
    base = _commit_file(history_repository, "deleted.py", "remove me\n", "base version")
    (history_repository / "deleted.py").unlink()
    (history_repository / "changed.py").write_text("new promotion\n", encoding="utf-8")
    _git(history_repository, "add", "--all")
    _git(history_repository, "commit", "-m", "safe promotion")
    head = _git(history_repository, "rev-parse", "HEAD")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 0
    assert result.stdout.strip() == "Promotion ancestor-blob guard passed: 1 changed paths checked."


def test_guard_rejects_restoring_blob_for_path_deleted_at_base(
    history_repository: Path,
) -> None:
    _commit_file(history_repository, "retired.py", "stale\n", "add retired path")
    (history_repository / "retired.py").unlink()
    _git(history_repository, "add", "--all")
    _git(history_repository, "commit", "-m", "delete retired path")
    base = _git(history_repository, "rev-parse", "HEAD")
    head = _commit_file(history_repository, "retired.py", "stale\n", "restore stale path")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 1
    assert 'path="retired.py"' in result.stderr


def test_guard_handles_literal_control_character_path(history_repository: Path) -> None:
    path = "odd:\nname[1].py"
    _commit_file(history_repository, path, "first\n", "first odd version")
    base = _commit_file(history_repository, path, "current\n", "current odd version")
    head = _commit_file(history_repository, path, "first\n", "stale odd promotion")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 1
    assert 'path="odd:\\nname[1].py"' in result.stderr


def test_guard_rejects_head_that_does_not_contain_base(history_repository: Path) -> None:
    root = _commit_file(history_repository, "factory.py", "root\n", "root")
    base = _commit_file(history_repository, "factory.py", "base\n", "base")
    _git(history_repository, "switch", "--detach", root)
    head = _commit_file(history_repository, "factory.py", "other\n", "unrelated head")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 2
    assert "promotion head does not contain the exact base commit" in result.stderr


def test_guard_rejects_non_object_revision_without_passing_it_to_git(
    history_repository: Path,
) -> None:
    head = _commit_file(history_repository, "factory.py", "head\n", "head")

    result = _run_guard(history_repository, "not-a-sha", head)

    assert result.returncode == 2
    assert "promotion revisions must be exact hexadecimal object IDs" in result.stderr


def test_guard_searches_merge_side_parent_history(history_repository: Path) -> None:
    root = _commit_file(history_repository, "factory.py", "root\n", "root")
    _git(history_repository, "switch", "-c", "side", root)
    side = _commit_file(history_repository, "factory.py", "side\n", "side version")
    _git(history_repository, "switch", "main")
    _commit_file(history_repository, "factory.py", "current\n", "current main version")
    _git(history_repository, "merge", "--no-ff", "-s", "ours", "side", "-m", "merge side")
    base = _git(history_repository, "rev-parse", "HEAD")
    head = _commit_file(history_repository, "factory.py", "side\n", "stale promotion")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 1
    assert f"older_commit={side}" in result.stderr


def test_guard_requires_advanced_base_before_approval(history_repository: Path) -> None:
    root = _commit_file(history_repository, "factory.py", "root\n", "root")
    _git(history_repository, "switch", "-c", "develop", root)
    _commit_file(history_repository, "feature.py", "feature\n", "feature")
    stale_head = _git(history_repository, "rev-parse", "HEAD")
    _git(history_repository, "switch", "main")
    advanced_base = _commit_file(history_repository, "base.py", "advanced\n", "advance main")

    stale_result = _run_guard(history_repository, advanced_base, stale_head)
    assert stale_result.returncode == 2

    _git(history_repository, "switch", "develop")
    _git(history_repository, "merge", "--no-edit", "main")
    updated_head = _git(history_repository, "rev-parse", "HEAD")
    updated_result = _run_guard(history_repository, advanced_base, updated_head)
    assert updated_result.returncode == 0


def test_guard_accepts_rename_to_new_literal_path(history_repository: Path) -> None:
    base = _commit_file(history_repository, "old.py", "current\n", "current path")
    _git(history_repository, "mv", "old.py", "new[1].py")
    _git(history_repository, "commit", "-m", "rename path")
    head = _git(history_repository, "rev-parse", "HEAD")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 0
    assert "1 changed paths checked" in result.stdout


def test_guard_fails_closed_for_shallow_history(history_repository: Path, tmp_path: Path) -> None:
    _commit_file(history_repository, "factory.py", "first\n", "first")
    base = _commit_file(history_repository, "factory.py", "base\n", "base")
    head = _commit_file(history_repository, "factory.py", "head\n", "head")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth=2", f"file://{history_repository}", str(shallow)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    result = _run_guard(shallow, base, head)

    assert result.returncode == 2
    assert "requires full history" in result.stderr


def test_guard_rejects_stale_blob_across_symlink_type_change(
    history_repository: Path,
) -> None:
    _commit_file(history_repository, "factory.py", "first\n", "first")
    base = _commit_file(history_repository, "factory.py", "current\n", "current")
    (history_repository / "factory.py").unlink()
    (history_repository / "factory.py").symlink_to("first\n")
    _git(history_repository, "add", "--", "factory.py")
    _git(history_repository, "commit", "-m", "stale symlink promotion")
    head = _git(history_repository, "rev-parse", "HEAD")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 1


def test_guard_fails_closed_for_gitlink(history_repository: Path) -> None:
    base = _commit_file(history_repository, "factory.py", "current\n", "base")
    _git(
        history_repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{base},vendor",
    )
    _git(history_repository, "commit", "-m", "add gitlink")
    head = _git(history_repository, "rev-parse", "HEAD")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 2
    assert 'path is not a blob: "vendor"' in result.stderr


def test_guard_handles_non_utf8_path(history_repository: Path) -> None:
    path = os.fsdecode(b"nonutf8-\xff.py")
    _commit_file(history_repository, path, "first\n", "first")
    base = _commit_file(history_repository, path, "current\n", "current")
    head = _commit_file(history_repository, path, "first\n", "stale promotion")

    result = _run_guard(history_repository, base, head)

    assert result.returncode == 1
    assert "nonutf8-\\udcff.py" in result.stderr
