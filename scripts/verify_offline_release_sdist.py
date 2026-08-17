#!/usr/bin/env python3
"""Safely extract and verify the release-only offline Docker context."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_MEMBERS = 20_000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_LOCK_BYTES = 1024 * 1024
VARIABLE_COPY_FROM_RE = re.compile(
    r"^\s*COPY\b[^\n]*--from\s*=\s*\$(?:[A-Z_][A-Z0-9_]*|\{[A-Z_][A-Z0-9_]*\})",
    re.IGNORECASE | re.MULTILINE,
)
PINNED_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}")


class OfflineSdistError(ValueError):
    """Raised when a release sdist is not one safe offline Docker context."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(name: str, root: str) -> PurePosixPath:
    if "\\" in name or "\0" in name:
        raise OfflineSdistError("sdist contains a non-canonical member name")
    path = PurePosixPath(name)
    if (
        name != path.as_posix()
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise OfflineSdistError("sdist member escapes its canonical root")
    if path.parts[0] != root:
        raise OfflineSdistError("sdist member differs from its canonical root")
    return PurePosixPath(*path.parts[1:])


def _canonical_lock(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_LOCK_BYTES:
        raise OfflineSdistError("offline build lock size is invalid")
    try:
        lock = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfflineSdistError("offline build lock is not valid JSON") from exc
    canonical = json.dumps(lock, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if raw != canonical or not isinstance(lock, dict):
        raise OfflineSdistError("offline build lock is not canonical")
    return lock


def _cache_inventory(output: Path) -> list[dict[str, object]]:
    cache_root = output / "docker/build-cache"
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise OfflineSdistError("offline wheel cache is unavailable")
    cache_rows: list[dict[str, object]] = []
    for path in sorted(cache_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise OfflineSdistError("offline wheel cache contains an unsafe entry")
        if path.is_file():
            if path.suffix != ".whl" or path.stat().st_size <= 0:
                raise OfflineSdistError("offline wheel cache contains a non-wheel")
            cache_rows.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )
    return cache_rows


def _validate_context(output: Path) -> None:
    dockerfile = output / "Dockerfile"
    uv_lock = output / "uv.lock"
    lock_path = output / "docker/offline-build-inputs.json"
    for required in (dockerfile, uv_lock, lock_path, output / "pyproject.toml"):
        if required.is_symlink() or not required.is_file():
            raise OfflineSdistError(f"release context is missing {required.name}")
    lock = _canonical_lock(lock_path)
    cache_rows = _cache_inventory(output)
    try:
        dockerfile_text = dockerfile.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise OfflineSdistError("release Dockerfile is not UTF-8") from exc
    pinned_images = sorted(set(PINNED_IMAGE_RE.findall(dockerfile_text)))
    if (
        set(lock) != {"dockerfile_sha256", "files", "images", "schema", "uv_lock_sha256"}
        or lock.get("schema") != 2
        or lock.get("dockerfile_sha256") != _sha256(dockerfile)
        or lock.get("uv_lock_sha256") != _sha256(uv_lock)
        or not cache_rows
        or lock.get("files") != cache_rows
        or len(pinned_images) != 2
        or dockerfile_text.count("@sha256:") != len(pinned_images)
        or lock.get("images") != pinned_images
    ):
        raise OfflineSdistError("offline build inventory differs from extracted bytes")
    if VARIABLE_COPY_FROM_RE.search(dockerfile_text):
        raise OfflineSdistError("Dockerfile COPY --from cannot use a variable")


def _write_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
) -> None:
    if member.isdir():
        destination.mkdir(parents=True, exist_ok=True, mode=0o755)
        return
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    stream = archive.extractfile(member)
    if stream is None:
        raise OfflineSdistError("sdist member cannot be read")
    payload = stream.read(member.size + 1)
    if len(payload) != member.size:
        raise OfflineSdistError("sdist member size changed")
    destination.write_bytes(payload)
    destination.chmod(0o644)


def extract_and_verify(sdist: Path, output: Path, version: str) -> Path:
    """Extract one bounded regular-file sdist and verify its offline inventory."""
    expected_root = f"proxbox_api-{version}"
    if output.exists() or output.is_symlink():
        raise OfflineSdistError("release context output already exists")
    try:
        with tarfile.open(sdist, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_MEMBERS:
                raise OfflineSdistError("sdist member count exceeds its bound")
            names: set[str] = set()
            expanded = 0
            output.mkdir(mode=0o700)
            for member in members:
                name = member.name.rstrip("/")
                if name in names:
                    raise OfflineSdistError("sdist contains duplicate members")
                names.add(name)
                relative = _safe_relative(name, expected_root)
                if not (member.isfile() or member.isdir()):
                    raise OfflineSdistError("sdist contains a link or special member")
                expanded += member.size
                if expanded > MAX_EXPANDED_BYTES:
                    raise OfflineSdistError("sdist expanded size exceeds its bound")
                if not relative.parts:
                    continue
                destination = output.joinpath(*relative.parts)
                _write_member(archive, member, destination)
    except (OSError, tarfile.TarError) as exc:
        raise OfflineSdistError("sdist is not a readable gzip tar archive") from exc
    _validate_context(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    extract_and_verify(args.sdist, args.output, args.version)
    print("verified extracted offline release context")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
