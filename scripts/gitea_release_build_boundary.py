#!/usr/bin/env python3
"""Run release preparation behind a token-free, bounded UID boundary."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import os
import resource
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

BUILD_UID = 65532
BUILD_GID = 65532
CGROUP_MEMORY_MAX = 2 * 1024 * 1024 * 1024
CGROUP_PIDS_MAX = 64
TMPFS_BYTES_MAX = 1024 * 1024 * 1024
TMPFS_INODES_MAX = 50000


def _positive_limit(path: Path, label: str) -> int:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Hard {label} cgroup limit is unavailable") from exc
    if value <= 0:
        raise RuntimeError(f"Hard {label} cgroup limit is invalid")
    return value


def _nonnegative_limit(path: Path, label: str) -> int:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"{label} cgroup value is unavailable") from exc
    if value < 0:
        raise RuntimeError(f"{label} cgroup value is invalid")
    return value


def _verify_kernel_quotas(build_root: Path) -> None:  # noqa: C901
    quota_root = Path("/nmc-build")
    if build_root.parent != quota_root or not quota_root.is_dir():
        raise RuntimeError("Build root is outside the hard quota mount")
    mount_rows: list[str] = []
    for row in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = row.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if len(fields) > separator + 1 and fields[4] == str(quota_root):
            mount_rows.append(fields[separator + 1])
    if mount_rows != ["tmpfs"]:
        raise RuntimeError("Build root lacks an exact tmpfs quota mount")
    filesystem = os.statvfs(quota_root)
    total_bytes = filesystem.f_blocks * filesystem.f_frsize
    if not 0 < total_bytes <= TMPFS_BYTES_MAX:
        raise RuntimeError("Build tmpfs byte quota is unavailable")
    if not 0 < filesystem.f_files <= TMPFS_INODES_MAX:
        raise RuntimeError("Build tmpfs inode quota is unavailable")

    cgroup_root = Path("/sys/fs/cgroup")
    if not (cgroup_root / "cgroup.controllers").is_file():
        raise RuntimeError("Unified cgroup v2 limits are unavailable")
    cpu_fields = (cgroup_root / "cpu.max").read_text(encoding="ascii").split()
    if len(cpu_fields) != 2 or cpu_fields[0] == "max":
        raise RuntimeError("Hard CPU cgroup limit is unavailable")
    try:
        cpu_quota, cpu_period = map(int, cpu_fields)
    except ValueError as exc:
        raise RuntimeError("Hard CPU cgroup limit is invalid") from exc
    if cpu_quota <= 0 or cpu_period <= 0 or cpu_quota > cpu_period:
        raise RuntimeError("Hard CPU cgroup limit exceeds one CPU")
    if _positive_limit(cgroup_root / "memory.max", "memory") > CGROUP_MEMORY_MAX:
        raise RuntimeError("Hard memory cgroup limit exceeds policy")
    if _nonnegative_limit(cgroup_root / "memory.swap.max", "swap") != 0:
        raise RuntimeError("Hard cgroup policy must disable swap")
    if _positive_limit(cgroup_root / "pids.max", "PID") > CGROUP_PIDS_MAX:
        raise RuntimeError("Hard PID cgroup limit exceeds policy")


def _processes_for_uid(uid: int) -> list[int]:
    matches: list[int] = []
    for status_path in Path("/proc").glob("[0-9]*/status"):
        try:
            rows = status_path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise RuntimeError("Cannot inspect reserved build UID") from exc
        uid_row = next((row for row in rows if row.startswith("Uid:")), "")
        fields = uid_row.split()
        if len(fields) >= 2 and fields[1] == str(uid):
            matches.append(int(status_path.parent.name))
    return matches


def _checked_tree(root: Path) -> tuple[Path, list[Path]]:
    resolved_root = root.resolve(strict=True)
    candidates = [resolved_root, *resolved_root.rglob("*")]
    for candidate in candidates:
        if candidate.is_symlink():
            target = candidate.resolve(strict=True)
            if not target.is_relative_to(resolved_root):
                raise RuntimeError("External tree symlinks are forbidden")
    return resolved_root, candidates


def _hand_tree_to_build_user(root: Path) -> None:
    _, candidates = _checked_tree(root)
    for candidate in candidates:
        os.chown(candidate, BUILD_UID, BUILD_GID, follow_symlinks=False)


def _expose_read_only_tree(root: Path) -> None:
    _, candidates = _checked_tree(root)
    for candidate in candidates:
        if candidate.is_symlink():
            continue
        mode = candidate.stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            os.chmod(candidate, 0o555, follow_symlinks=False)  # nosec B103
        elif stat.S_ISREG(mode):
            os.chmod(candidate, 0o555 if mode & 0o111 else 0o444, follow_symlinks=False)
        else:
            raise RuntimeError("Python runtime contains a special file")


def _restrict_writes_to_build_root(root: Path) -> None:
    if os.uname().machine != "x86_64":
        raise OSError(errno.ENOTSUP, "Landlock syscall mapping requires x86-64")
    create_ruleset = 444
    add_rule = 445
    restrict_self = 446
    create_ruleset_version = 1
    rule_path_beneath = 1
    write_access = (
        (1 << 1)
        | (1 << 4)
        | (1 << 5)
        | (1 << 6)
        | (1 << 7)
        | (1 << 8)
        | (1 << 9)
        | (1 << 10)
        | (1 << 11)
        | (1 << 12)
        | (1 << 13)
        | (1 << 14)
    )

    class RulesetAttr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def checked_syscall(number: int, *args: object) -> int:
        result = libc.syscall(number, *args)
        if result < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        return int(result)

    abi = checked_syscall(
        create_ruleset,
        ctypes.c_void_p(),
        0,
        create_ruleset_version,
    )
    if abi < 3:
        raise OSError(errno.ENOTSUP, "Landlock ABI 3 or newer is required")
    ruleset_attr = RulesetAttr(handled_access_fs=write_access)
    ruleset_fd = checked_syscall(
        create_ruleset,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    root_fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
    try:
        path_rule_raw = struct.pack("=Qi", write_access, root_fd)
        path_rule = ctypes.create_string_buffer(path_rule_raw, len(path_rule_raw))
        checked_syscall(add_rule, ruleset_fd, rule_path_beneath, path_rule, 0)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        checked_syscall(restrict_self, ruleset_fd, 0)
    finally:
        os.close(root_fd)
        os.close(ruleset_fd)


def _drop_privileges(build_root: Path) -> None:
    _restrict_writes_to_build_root(build_root)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (CGROUP_MEMORY_MAX,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (CGROUP_PIDS_MAX, CGROUP_PIDS_MAX))
    os.setgroups([])
    os.setgid(BUILD_GID)
    os.setuid(BUILD_UID)
    os.umask(0o077)


def _kill_remaining_build_processes() -> None:
    for _ in range(4):
        pids = _processes_for_uid(BUILD_UID)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    raise
        time.sleep(0.05)
    if _processes_for_uid(BUILD_UID):
        raise RuntimeError("Untrusted build process survived cleanup")


def _build_tree_usage(build_root: Path) -> tuple[int, int]:
    total_size = 0
    total_entries = 0
    for candidate in build_root.rglob("*"):
        metadata = candidate.lstat()
        total_entries += 1
        if stat.S_ISREG(metadata.st_mode):
            total_size += metadata.st_size
    return total_size, total_entries


def process_cpu_ticks(stat_row: str) -> int:
    """Return live plus reaped-descendant CPU ticks from a proc stat row."""
    _, delimiter, remaining = stat_row.rpartition(") ")
    if not delimiter:
        raise ValueError("Malformed process stat record")
    fields = remaining.split()
    if len(fields) <= 14:
        raise ValueError("Incomplete process stat record")
    return sum(int(field) for field in fields[11:15])


def _available_filesystem_bytes(root: Path) -> int:
    usage = os.statvfs(root)
    return usage.f_bavail * usage.f_frsize


def _build_process_usage() -> tuple[int, int, float]:
    total_rss_kib = 0
    total_cpu_ticks = 0
    pids = _processes_for_uid(BUILD_UID)
    for pid in pids:
        try:
            status_rows = Path(f"/proc/{pid}/status").read_text().splitlines()
            stat_row = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        rss_row = next((row for row in status_rows if row.startswith("VmRSS:")), "")
        rss_fields = rss_row.split()
        if len(rss_fields) >= 2:
            total_rss_kib += int(rss_fields[1])
        total_cpu_ticks += process_cpu_ticks(stat_row)
    return len(pids), total_rss_kib, total_cpu_ticks / os.sysconf("SC_CLK_TCK")


def _cgroup_memory_usage() -> int:
    cgroup_root = Path("/sys/fs/cgroup")
    return _nonnegative_limit(
        cgroup_root / "memory.current", "memory current"
    ) + _nonnegative_limit(cgroup_root / "memory.swap.current", "swap current")


def _candidate_command() -> str:
    return r"""
set -eu
printf '%s\n' '::set-env name=NMC_RELEASE_BOUNDARY_INJECTED::yes'
printf '%s\n' '::add-path::/tmp/nmc-release-boundary-injected'
test "$RUN_ATTEMPT" = 1
test -z "${GITHUB_TOKEN:-}"
test -z "${GITEA_TOKEN:-}"
test -z "${ACTIONS_RUNTIME_TOKEN:-}"
test -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}"
test -z "${GITHUB_ENV:-}"
test -z "${GITHUB_OUTPUT:-}"
test ! -r "/proc/$BOUNDARY_PARENT_PID/environ"
cd "$BUILD_ROOT/source"
UV_PROJECT_ENVIRONMENT="$BUILD_ROOT/venv" \
  "$UV_BIN" sync --no-config --cache-dir "$BUILD_ROOT/uv-cache" \
  --managed-python --no-python-downloads --python 3.12.13 \
  --locked --only-group publish --no-install-project
"$UV_BIN" export --no-config --frozen --no-dev --group publish \
  --no-emit-project --format requirements-txt \
  --output-file "$BUILD_ROOT/runtime-requirements.txt"
test ! -e docker/build-cache
test ! -e docker/offline-build-inputs.json
mkdir -m 0700 -p docker/build-cache
"$BUILD_ROOT/venv/bin/python" -m ensurepip --default-pip
"$BUILD_ROOT/venv/bin/python" -m pip download \
  --disable-pip-version-check --require-hashes --only-binary=:all: \
  --dest docker/build-cache --requirement "$BUILD_ROOT/runtime-requirements.txt"
find docker/build-cache -mindepth 1 -maxdepth 1 -type f -name '*.whl' \
  -exec chmod 0444 {} +
test "$(find docker/build-cache -mindepth 1 -maxdepth 1 -type f | wc -l)" -gt 0
test "$(find docker/build-cache -mindepth 1 -maxdepth 1 ! -type f | wc -l)" -eq 0
"$BUILD_ROOT/venv/bin/python" scripts/prepare_offline_release.py
"$BUILD_ROOT/venv/bin/python" -m build --no-isolation --outdir dist
"$BUILD_ROOT/venv/bin/python" -m twine check dist/*
"$BUILD_ROOT/venv/bin/python" scripts/release_artifacts.py manifest \
  --dist dist --package proxbox_api --version "$VERSION" \
  --source-sha "$SOURCE_SHA" --manifest release-manifest.json
"""


def run_boundary(  # noqa: C901
    *,
    build_root: Path,
    uv_bin: Path,
    python_root: Path,
    source_sha: str,
    tag: str,
    version: str,
    run_attempt: str,
) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Token boundary requires a root job container")
    if run_attempt != "1":
        raise RuntimeError("Release builds must be first-attempt jobs")
    if build_root.exists() or build_root.is_symlink():
        raise RuntimeError("Build root already exists")
    _verify_kernel_quotas(build_root)
    if _processes_for_uid(BUILD_UID):
        raise RuntimeError("Reserved build UID is already active")

    source_root = build_root / "source"
    home_root = build_root / "home"
    temp_root = build_root / "tmp"
    ignored = shutil.ignore_patterns(
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "dist",
        "release-transfer",
    )
    for source_path in Path.cwd().rglob("*"):
        if source_path.is_symlink():
            raise RuntimeError("Source symlinks are forbidden")
    shutil.copytree(Path.cwd(), source_root, ignore=ignored)
    home_root.mkdir(mode=0o700)
    temp_root.mkdir(mode=0o700)
    _hand_tree_to_build_user(build_root)
    _expose_read_only_tree(python_root)
    os.chmod(uv_bin.parent, 0o555)  # nosec B103
    os.chmod(uv_bin, 0o555)  # nosec B103

    safe_env = {
        "BOUNDARY_PARENT_PID": str(os.getpid()),
        "BUILD_ROOT": str(build_root),
        "HOME": str(home_root),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "RUN_ATTEMPT": run_attempt,
        "SOURCE_SHA": source_sha,
        "TAG": tag,
        "TMPDIR": str(temp_root),
        "UV_BIN": str(uv_bin),
        "UV_PYTHON_INSTALL_DIR": str(python_root),
        "VERSION": version,
    }
    process = subprocess.Popen(  # noqa: S603
        ["/bin/sh", "-c", _candidate_command()],
        cwd=source_root,
        env=safe_env,
        preexec_fn=lambda: _drop_privileges(build_root),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = bytearray()
    output_limit = 1024 * 1024
    disk_limit = 1024 * 1024 * 1024
    baseline_available_bytes = _available_filesystem_bytes(build_root)
    deadline = time.monotonic() + 900
    boundary_error: str | None = None
    selector = selectors.DefaultSelector()
    if process.stdout is None:
        raise RuntimeError("Token-free build output pipe is unavailable")
    os.set_blocking(process.stdout.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                boundary_error = "Token-free build exceeded wall-clock limit"
                break
            events = selector.select(timeout=0.25)
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(output) + len(chunk) > output_limit:
                    boundary_error = "Token-free build output exceeded limit"
                    break
                output.extend(chunk)
            if boundary_error is not None:
                break
            try:
                disk_bytes, entries = _build_tree_usage(build_root)
                available_bytes = _available_filesystem_bytes(build_root)
                pids, rss_kib, cpu_seconds = _build_process_usage()
                memory_bytes = _cgroup_memory_usage()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                boundary_error = "Token-free build filesystem changed"
                break
            consumed_bytes = max(0, baseline_available_bytes - available_bytes)
            if disk_bytes > disk_limit or consumed_bytes > disk_limit or entries > TMPFS_INODES_MAX:
                boundary_error = "Token-free build exceeded disk quota"
                break
            if (
                pids > CGROUP_PIDS_MAX
                or rss_kib > 2 * 1024 * 1024
                or memory_bytes > CGROUP_MEMORY_MAX
                or cpu_seconds > 900
            ):
                boundary_error = "Token-free build exceeded process quota"
                break
            if process.poll() is not None and not events:
                break
        if boundary_error is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    raise
        return_code = process.wait(timeout=10)
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise
        _kill_remaining_build_processes()
    if boundary_error is not None or return_code != 0:
        encoded_output = base64.b64encode(bytes(output)).decode("ascii")
        if encoded_output:
            print(f"candidate-output-base64:{encoded_output}", file=sys.stderr)
        raise RuntimeError(boundary_error or "Token-free build failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--uv-bin", type=Path, required=True)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-attempt", required=True)
    args = parser.parse_args()
    run_boundary(
        build_root=args.build_root.resolve(),
        uv_bin=args.uv_bin.resolve(strict=True),
        python_root=args.python_root.resolve(strict=True),
        source_sha=args.source_sha,
        tag=args.tag,
        version=args.version,
        run_attempt=args.run_attempt,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
