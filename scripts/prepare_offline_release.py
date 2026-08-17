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
                raise OfflineReleaseError("Dockerfile parser directives are forbidden")
            continue
        if raw_line.rstrip().endswith("\\"):
            raise OfflineReleaseError("Dockerfile continuations are forbidden")
        instruction, _, arguments = line.partition(" ")
        instruction = instruction.upper()
        arguments = arguments.strip()
        if instruction == "ARG":
            raise OfflineReleaseError("Dockerfile build arguments are forbidden")
        if instruction == "ADD":
            raise OfflineReleaseError("Dockerfile ADD is forbidden")
        if instruction == "FROM":
            fields = arguments.split()
            if (
                len(fields) != 3
                or fields[1].upper() != "AS"
                or "$" in fields[0]
                or fields[0] not in PINNED_IMAGES
                or re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", fields[2]) is None
            ):
                raise OfflineReleaseError("Dockerfile FROM is not exact and immutable")
            alias = fields[2]
            if alias in stages or fields[0] in images:
                raise OfflineReleaseError("Dockerfile stage identity is ambiguous")
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
                    raise OfflineReleaseError(
                        "Dockerfile offline sync does not follow one cache copy"
                    )
                offline_sync_stage = current_stage
            elif arguments != "chmod 0555 /usr/local/bin/docker-entrypoint-raw.sh":
                raise OfflineReleaseError("Dockerfile RUN is not allowlisted")
            continue
        if instruction != "COPY":
            if instruction not in allowed_passive:
                raise OfflineReleaseError("Dockerfile instruction is not allowlisted")
            continue
        fields = arguments.split()
        if len(fields) < 2 or any("$" in field for field in fields):
            raise OfflineReleaseError("Dockerfile COPY is not a literal local copy")
        flags = [field for field in fields if field.startswith("--")]
        if len(flags) > 1 or any(not field.startswith("--from=") for field in flags):
            raise OfflineReleaseError("Dockerfile COPY flags are not allowlisted")
        if flags:
            source_stage = flags[0].split("=", 1)[1]
            if source_stage not in stages:
                raise OfflineReleaseError("Dockerfile COPY uses an external source")
        sources = [field for field in fields if not field.startswith("--")][:-1]
        if not sources or any(
            source.startswith(("http://", "https://", "git@", "ssh://")) for source in sources
        ):
            raise OfflineReleaseError("Dockerfile COPY source is not local")
        if fields == ["docker/build-cache", "/root/.cache/uv"]:
            if current_stage < 0 or cache_copy_stage is not None:
                raise OfflineReleaseError("Dockerfile offline cache copy is ambiguous")
            cache_copy_stage = current_stage
    if sorted(images) != sorted(PINNED_IMAGES) or len(images) != len(PINNED_IMAGES):
        raise OfflineReleaseError("release Dockerfile image pins are incomplete")
    if cache_copy_stage is None or offline_sync_stage != cache_copy_stage:
        raise OfflineReleaseError("Dockerfile does not consume its offline cache")
    return sorted(images)


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
        if SHA256_RE.fullmatch(digest) is None:
            raise OfflineReleaseError("release Dockerfile image pins are incomplete")
    images = validate_dockerfile(dockerfile)

    shutil.copyfile(DOCKERFILE_SOURCE, DOCKERFILE_OUTPUT)
    DOCKERFILE_OUTPUT.chmod(0o644)
    lock = {
        "dockerfile_sha256": _sha256(DOCKERFILE_OUTPUT),
        "files": _cache_inventory(),
        "images": images,
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
