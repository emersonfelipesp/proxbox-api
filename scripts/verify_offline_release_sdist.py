#!/usr/bin/env python3
"""Safely extract and verify the release-only offline Docker context."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_MEMBERS = 20_000
MAX_COMPRESSED_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_LOCK_BYTES = 1024 * 1024
PINNED_IMAGES = (
    "emersonfelipesp/proxbox-api:0.0.19.post5@sha256:"
    "f8b5decb8415867d2befb013f64f158d31650a974e9bc60bdf4f2e78c1808794",
    "ghcr.io/astral-sh/uv:0.11.28@sha256:"
    "0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa",
)


class OfflineSdistError(ValueError):
    """Raised when a release sdist is not one safe offline Docker context."""


def validate_dockerfile(dockerfile: str) -> list[str]:  # noqa: C901
    """Return exact immutable base images from one restricted Dockerfile."""
    stages: set[str] = set()
    images: list[str] = []
    current_stage = -1
    cache_copy_stage: int | None = None
    offline_sync_stage: int | None = None
    allowed_passive = {"CMD", "ENTRYPOINT", "ENV", "EXPOSE", "VOLUME", "WORKDIR"}
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if re.match(r"^#\s*(?:syntax|escape|check)\s*=", line, re.IGNORECASE):
                raise OfflineSdistError("Dockerfile parser directives are forbidden")
            continue
        if raw_line.rstrip().endswith("\\"):
            raise OfflineSdistError("Dockerfile continuations are forbidden")
        instruction, _, arguments = line.partition(" ")
        instruction = instruction.upper()
        arguments = arguments.strip()
        if instruction == "ARG":
            raise OfflineSdistError("Dockerfile build arguments are forbidden")
        if instruction == "ADD":
            raise OfflineSdistError("Dockerfile ADD is forbidden")
        if instruction == "FROM":
            fields = arguments.split()
            if (
                len(fields) != 3
                or fields[1].upper() != "AS"
                or "$" in fields[0]
                or fields[0] not in PINNED_IMAGES
                or re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", fields[2]) is None
            ):
                raise OfflineSdistError("Dockerfile FROM is not exact and immutable")
            alias = fields[2]
            if alias in stages or fields[0] in images:
                raise OfflineSdistError("Dockerfile stage identity is ambiguous")
            stages.add(alias)
            images.append(fields[0])
            current_stage += 1
            continue
        if instruction == "RUN":
            if arguments == (
                "uv sync --frozen --offline --no-index --find-links "
                "/root/.cache/uv --no-dev --no-editable"
            ):
                if (
                    current_stage < 0
                    or cache_copy_stage != current_stage
                    or offline_sync_stage is not None
                ):
                    raise OfflineSdistError(
                        "Dockerfile offline sync does not follow one cache copy"
                    )
                offline_sync_stage = current_stage
            elif arguments != "chmod 0555 /usr/local/bin/docker-entrypoint-raw.sh":
                raise OfflineSdistError("Dockerfile RUN is not allowlisted")
            continue
        if instruction != "COPY":
            if instruction not in allowed_passive:
                raise OfflineSdistError("Dockerfile instruction is not allowlisted")
            continue
        fields = arguments.split()
        if len(fields) < 2 or any("$" in field for field in fields):
            raise OfflineSdistError("Dockerfile COPY is not a literal local copy")
        flags = [field for field in fields if field.startswith("--")]
        if len(flags) > 1 or any(not field.startswith("--from=") for field in flags):
            raise OfflineSdistError("Dockerfile COPY flags are not allowlisted")
        if flags:
            source_stage = flags[0].split("=", 1)[1]
            if source_stage not in stages:
                raise OfflineSdistError("Dockerfile COPY uses an external source")
        sources = [field for field in fields if not field.startswith("--")][:-1]
        if not sources or any(
            source.startswith(("http://", "https://", "git@", "ssh://")) for source in sources
        ):
            raise OfflineSdistError("Dockerfile COPY source is not local")
        if fields == ["docker/build-cache", "/root/.cache/uv"]:
            if current_stage < 0 or cache_copy_stage is not None:
                raise OfflineSdistError("Dockerfile offline cache copy is ambiguous")
            cache_copy_stage = current_stage
    if sorted(images) != sorted(PINNED_IMAGES) or len(images) != len(PINNED_IMAGES):
        raise OfflineSdistError("release Dockerfile image pins are incomplete")
    if cache_copy_stage is None or offline_sync_stage != cache_copy_stage:
        raise OfflineSdistError("Dockerfile does not consume its offline cache")
    return sorted(images)


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
    pinned_images = validate_dockerfile(dockerfile_text)
    if (
        set(lock) != {"dockerfile_sha256", "files", "images", "schema", "uv_lock_sha256"}
        or lock.get("schema") != 2
        or lock.get("dockerfile_sha256") != _sha256(dockerfile)
        or lock.get("uv_lock_sha256") != _sha256(uv_lock)
        or not cache_rows
        or lock.get("files") != cache_rows
        or lock.get("images") != pinned_images
    ):
        raise OfflineSdistError("offline build inventory differs from extracted bytes")


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
    descriptor = -1
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        remaining = member.size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise OfflineSdistError("sdist member size changed")
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OfflineSdistError("sdist member write failed")
                view = view[written:]
            remaining -= len(chunk)
        if stream.read(1):
            raise OfflineSdistError("sdist member size changed")
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def extract_and_verify(sdist: Path, output: Path, version: str) -> Path:  # noqa: C901
    """Extract one bounded regular-file sdist and verify its offline inventory."""
    expected_root = f"proxbox_api-{version}"
    if output.exists() or output.is_symlink():
        raise OfflineSdistError("release context output already exists")
    try:
        metadata = sdist.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= MAX_COMPRESSED_BYTES
        ):
            raise OfflineSdistError("sdist compressed input is unsafe")
        with tarfile.open(sdist, mode="r|gz") as archive:
            names: set[str] = set()
            expanded = 0
            member_count = 0
            output.mkdir(mode=0o700)
            for member in archive:
                member_count += 1
                if member_count > MAX_MEMBERS:
                    raise OfflineSdistError("sdist member count exceeds its bound")
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
            if member_count == 0:
                raise OfflineSdistError("sdist member count exceeds its bound")
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
