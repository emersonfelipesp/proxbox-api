#!/usr/bin/env python3
"""Copy exact release bytes out of the untrusted build root without following links."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024


class HandoffError(ValueError):
    """The candidate build output is not an exact safe release transfer."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _copy_regular(  # noqa: C901
    source_directory_fd: int,
    destination_directory_fd: int,
    name: str,
    maximum: int,
) -> tuple[str, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | nofollow,
        dir_fd=source_directory_fd,
    )
    destination_fd = -1
    try:
        metadata = os.fstat(source_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= maximum
        ):
            raise HandoffError(f"Release handoff source is unsafe: {name}")
        destination_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | nofollow,
            0o400,
            dir_fd=destination_directory_fd,
        )
        digest = hashlib.sha256()
        written = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            written += len(chunk)
            pending = memoryview(chunk)
            while pending:
                count = os.write(destination_fd, pending)
                if count <= 0:
                    raise HandoffError("Release handoff write failed")
                pending = pending[count:]
        after = os.fstat(source_fd)
        if written != metadata.st_size or (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise HandoffError("Release handoff source changed during copy")
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    copied_fd = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | nofollow,
        dir_fd=destination_directory_fd,
    )
    copied_digest = hashlib.sha256()
    copied_size = 0
    try:
        while True:
            chunk = os.read(copied_fd, 1024 * 1024)
            if not chunk:
                break
            copied_digest.update(chunk)
            copied_size += len(chunk)
    finally:
        os.close(copied_fd)
    if copied_size != written or copied_digest.digest() != digest.digest():
        raise HandoffError("Release handoff copy digest changed")
    return digest.hexdigest(), written


def _load_manifest(path: Path) -> tuple[bytes, dict[str, Any]]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandoffError("Release manifest is invalid JSON") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value):
        raise HandoffError("Release manifest is not canonical JSON")
    return raw, value


def create_handoff(  # noqa: C901
    *,
    build_root: Path,
    transfer_root: Path,
    source_sha: str,
    tag: str,
    version: str,
    run_id: int,
    run_attempt: int,
    workflow_path: Path,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise HandoffError("Release handoff requires the trusted root parent")
    if run_id <= 0 or run_attempt != 1:
        raise HandoffError("Release run identity is invalid")
    if transfer_root.exists() or transfer_root.is_symlink():
        raise HandoffError("Release transfer already exists")
    source_root = build_root / "source"
    dist_root = source_root / "dist"
    manifest_path = source_root / "release-manifest.json"
    expected_wheel = f"proxbox_api-{version}-py3-none-any.whl"
    expected_sdist = f"proxbox_api-{version}.tar.gz"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    os.mkdir(transfer_root, mode=0o700)
    dist_fd = manifest_parent_fd = transfer_fd = -1
    try:
        dist_fd = os.open(dist_root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
        manifest_parent_fd = os.open(
            manifest_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | nofollow,
        )
        transfer_fd = os.open(
            transfer_root,
            os.O_RDONLY | os.O_DIRECTORY | nofollow,
        )
        if sorted(os.listdir(dist_fd)) != sorted([expected_wheel, expected_sdist]):
            raise HandoffError("Release artifact inventory is invalid")
        _copy_regular(dist_fd, transfer_fd, expected_wheel, MAX_ARTIFACT_BYTES)
        _copy_regular(dist_fd, transfer_fd, expected_sdist, MAX_ARTIFACT_BYTES)
        _copy_regular(
            manifest_parent_fd,
            transfer_fd,
            "release-manifest.json",
            MAX_MANIFEST_BYTES,
        )
        if sorted(os.listdir(transfer_fd)) != sorted(
            [expected_wheel, expected_sdist, "release-manifest.json"]
        ):
            raise HandoffError("Release transfer inventory changed")
    finally:
        for descriptor in (dist_fd, manifest_parent_fd, transfer_fd):
            if descriptor >= 0:
                os.close(descriptor)

    manifest_raw, manifest = _load_manifest(transfer_root / "release-manifest.json")
    artifacts = [
        {"filename": row["name"], "sha256": row["sha256"], "size": row["size"]}
        for row in manifest.get("artifacts", [])
        if isinstance(row, dict)
    ]
    if (
        artifacts != sorted(artifacts, key=lambda row: row["filename"])
        or {row["filename"] for row in artifacts} != {expected_wheel, expected_sdist}
        or manifest.get("package") != "proxbox_api"
        or manifest.get("source_sha") != source_sha
        or manifest.get("version") != version
    ):
        raise HandoffError("Release manifest identity is invalid")
    workflow_sha256 = hashlib.sha256(workflow_path.read_bytes()).hexdigest()
    request = {
        "artifacts": artifacts,
        "initiating_run_attempt": run_attempt,
        "initiating_run_id": run_id,
        "owner": "emersonfelipesp",
        "package": manifest["package"],
        "release_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "repository": "proxbox-api",
        "repository_id": 37,
        "schema": 1,
        "source_sha": source_sha,
        "tag": tag,
        "version": version,
        "workflow_sha256": workflow_sha256,
    }
    request_path = transfer_root / "release-request.json"
    request_fd = os.open(
        request_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | nofollow,
        0o400,
    )
    try:
        pending = memoryview(_canonical_json(request))
        while pending:
            count = os.write(request_fd, pending)
            if count <= 0:
                raise HandoffError("Release request write failed")
            pending = pending[count:]
        os.fsync(request_fd)
    finally:
        os.close(request_fd)
    if sorted(path.name for path in transfer_root.iterdir()) != sorted(
        [expected_wheel, expected_sdist, "release-manifest.json", "release-request.json"]
    ):
        raise HandoffError("Release request inventory changed")
    return request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--transfer-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--workflow", type=Path, required=True)
    args = parser.parse_args()
    create_handoff(
        build_root=args.build_root.resolve(),
        transfer_root=args.transfer_root.resolve(),
        source_sha=args.source_sha,
        tag=args.tag,
        version=args.version,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        workflow_path=args.workflow.resolve(strict=True),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
