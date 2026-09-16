"""Reject promotion changes that restore an older base-history blob."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class PromotionBlobGuardError(RuntimeError):
    """The promotion history or repository state cannot be validated safely."""


@dataclass(frozen=True)
class AncestorBlobRegression:
    """A proposed path resolves to a blob superseded on the base branch."""

    path: str
    proposed_blob: str
    base_blob: str | None
    older_commit: str


def _git(
    repository: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PromotionBlobGuardError(
            f"git {' '.join(arguments[:2])} failed with status {result.returncode}: {detail}"
        )
    return result


def _resolve_commit(repository: Path, revision: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) is None:
        raise PromotionBlobGuardError("promotion revisions must be exact hexadecimal object IDs")
    result = _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return result.stdout.decode("ascii").strip()


def _require_complete_history(repository: Path) -> None:
    result = _git(repository, "rev-parse", "--is-shallow-repository")
    if result.stdout.strip() != b"false":
        raise PromotionBlobGuardError("promotion ancestor-blob validation requires full history")


def _require_base_ancestor(repository: Path, base: str, head: str) -> None:
    result = _git(repository, "merge-base", "--is-ancestor", base, head, check=False)
    if result.returncode == 1:
        raise PromotionBlobGuardError("promotion head does not contain the exact base commit")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PromotionBlobGuardError(f"cannot validate promotion ancestry: {detail}")


def _changed_paths(repository: Path, base: str, head: str) -> list[str]:
    result = _git(
        repository,
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=ACMRTUXB",
        "--no-renames",
        base,
        head,
    )
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def _blob_oid(repository: Path, commit: str, path: str) -> str | None:
    result = _git(
        repository,
        "ls-tree",
        "-z",
        commit,
        "--",
        f":(literal){path}",
    )
    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    if not entries:
        return None
    if len(entries) != 1:
        raise PromotionBlobGuardError(f"path lookup was not exact: {json.dumps(path)}")
    metadata, listed_path = entries[0].split(b"\t", 1)
    if os.fsdecode(listed_path) != path:
        raise PromotionBlobGuardError(f"path lookup returned a different path: {json.dumps(path)}")
    fields = metadata.split()
    if len(fields) != 3 or fields[1] != b"blob":
        raise PromotionBlobGuardError(f"path is not a blob: {json.dumps(path)}")
    return fields[2].decode("ascii")


def _path_history(repository: Path, base: str, path: str) -> list[str]:
    result = _git(
        repository,
        "log",
        "--full-history",
        "--format=%H",
        base,
        "--",
        f":(literal){path}",
    )
    return [commit.decode("ascii") for commit in result.stdout.splitlines() if commit]


def find_ancestor_blob_regressions(
    repository: Path,
    *,
    base: str,
    head: str,
) -> tuple[list[AncestorBlobRegression], int]:
    base_commit = _resolve_commit(repository, base)
    head_commit = _resolve_commit(repository, head)
    _require_complete_history(repository)
    _require_base_ancestor(repository, base_commit, head_commit)
    regressions: list[AncestorBlobRegression] = []
    paths = _changed_paths(repository, base_commit, head_commit)
    for path in paths:
        proposed_blob = _blob_oid(repository, head_commit, path)
        base_blob = _blob_oid(repository, base_commit, path)
        if proposed_blob is None or proposed_blob == base_blob:
            continue
        for older_commit in _path_history(repository, base_commit, path):
            older_blob = _blob_oid(repository, older_commit, path)
            if older_blob == base_blob:
                continue
            if older_blob == proposed_blob:
                regressions.append(
                    AncestorBlobRegression(
                        path=path,
                        proposed_blob=proposed_blob,
                        base_blob=base_blob,
                        older_commit=older_commit,
                    )
                )
                break
    return regressions, len(paths)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Exact promotion base revision")
    parser.add_argument("--head", required=True, help="Exact promotion head revision")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        regressions, checked_paths = find_ancestor_blob_regressions(
            arguments.repository,
            base=arguments.base,
            head=arguments.head,
        )
    except PromotionBlobGuardError as error:
        print(
            f"Promotion ancestor-blob guard could not validate the repository: {error}",
            file=sys.stderr,
        )
        return 2
    for regression in regressions:
        print(
            "Promotion ancestor-blob regression: "
            f"path={json.dumps(regression.path)} "
            f"proposed_blob={regression.proposed_blob} "
            f"base_blob={regression.base_blob} "
            f"older_commit={regression.older_commit}",
            file=sys.stderr,
        )
    if regressions:
        return 1
    print(f"Promotion ancestor-blob guard passed: {checked_paths} changed paths checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
