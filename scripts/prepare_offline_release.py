#!/usr/bin/env python3
"""Prepare the exact offline Docker context embedded in a release sdist."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_SOURCE = REPOSITORY_ROOT / "Dockerfile.release"
DOCKERFILE_OUTPUT = REPOSITORY_ROOT / "Dockerfile"
UV_LOCK = REPOSITORY_ROOT / "uv.lock"
CACHE_ROOT = REPOSITORY_ROOT / "docker/build-cache"
LOCK_OUTPUT = REPOSITORY_ROOT / "docker/offline-build-inputs.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PINNED_IMAGES = (
    "emersonfelipesp/proxbox-api:0.0.19.post5@sha256:"
    "f8b5decb8415867d2befb013f64f158d31650a974e9bc60bdf4f2e78c1808794",
    "ghcr.io/astral-sh/uv:0.11.28@sha256:"
    "0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa",
)


class OfflineReleaseError(ValueError):
    """Raised when release build inputs are incomplete or mutable."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise OfflineReleaseError(f"unsafe offline release input: {path}")


def _cache_inventory() -> list[dict[str, object]]:
    if not CACHE_ROOT.is_dir() or CACHE_ROOT.is_symlink():
        raise OfflineReleaseError("offline wheel cache is unavailable")
    rows: list[dict[str, object]] = []
    for path in sorted(CACHE_ROOT.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise OfflineReleaseError("offline wheel cache contains an unsafe entry")
        if path.is_file():
            _regular_file(path)
            if path.suffix != ".whl":
                raise OfflineReleaseError("offline cache must contain wheels only")
            rows.append(
                {
                    "path": path.relative_to(REPOSITORY_ROOT).as_posix(),
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )
    if not rows:
        raise OfflineReleaseError("offline wheel cache is empty")
    return rows


def prepare() -> dict[str, object]:
    _regular_file(DOCKERFILE_SOURCE)
    _regular_file(UV_LOCK)
    dockerfile = DOCKERFILE_SOURCE.read_text(encoding="utf-8")
    for image in PINNED_IMAGES:
        digest = image.rsplit("@sha256:", 1)[-1]
        if SHA256_RE.fullmatch(digest) is None or image not in dockerfile:
            raise OfflineReleaseError("release Dockerfile image pins are incomplete")
    if dockerfile.count("@sha256:") != len(PINNED_IMAGES):
        raise OfflineReleaseError("release Dockerfile has an unreviewed image pin")

    shutil.copyfile(DOCKERFILE_SOURCE, DOCKERFILE_OUTPUT)
    DOCKERFILE_OUTPUT.chmod(0o644)
    lock = {
        "dockerfile_sha256": _sha256(DOCKERFILE_OUTPUT),
        "files": _cache_inventory(),
        "images": sorted(PINNED_IMAGES),
        "schema": 2,
        "uv_lock_sha256": _sha256(UV_LOCK),
    }
    raw = json.dumps(lock, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    LOCK_OUTPUT.write_bytes(raw)
    LOCK_OUTPUT.chmod(0o644)
    return lock


def main() -> int:
    lock = prepare()
    print(f"prepared {len(lock['files'])} immutable offline release files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
