"""Explicit developer commands for offline mounted registration evidence."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proxbox_api.operation_inventory.schema import Inventory


def _inside(path: object, roots: tuple[Path, ...]) -> bool:
    if not isinstance(path, (str, bytes)):
        return False
    candidate = Path(os.fsdecode(path)).resolve()
    return any(candidate.is_relative_to(root) for root in roots)


def offline_guard(root: Path, scratch: Path) -> None:
    """Refuse sockets, process creation and writes outside this child's scratch."""

    readable = (
        scratch,
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        root / "proxbox_api",
        root / "scripts",
        root / "contracts",
        root / "proxbox_api.egg-info",
        root / "pyproject.toml",
        root / "uv.lock",
    )
    mutations = {
        "os.mkdir": (0,),
        "os.remove": (0,),
        "os.rmdir": (0,),
        "os.rename": (0, 1),
        "os.link": (0, 1),
        "os.symlink": (0, 1),
        "os.chmod": (0,),
        "os.truncate": (0,),
    }

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event.startswith("socket.") or event in {"subprocess.Popen", "os.posix_spawn"}:
            raise PermissionError("Offline inventory forbids network and process creation")
        if event == "open":
            path, mode, flags = arguments
            writing = isinstance(mode, str) and any(letter in mode for letter in "wax+")
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            writing = writing or isinstance(flags, int) and bool(flags & write_flags)
            allowed = (scratch,) if writing else readable
            if not _inside(path, allowed):
                raise PermissionError("Offline inventory file access escaped its allowed roots")
        if event in mutations:
            if not all(_inside(arguments[index], (scratch,)) for index in mutations[event]):
                raise PermissionError("Offline inventory mutation escaped scratch")
        if event == "sqlite3.connect":
            raise PermissionError("Offline inventory forbids database access")

    sys.addaudithook(audit)


def worker(root: Path, scratch: Path, action: str) -> int:
    """Install isolation before the package's existing import-time logger setup."""
    sys.dont_write_bytecode = True
    os.environ.clear()
    os.environ.update(child_environment(scratch))
    os.environ["PROXBOX_GENERATED_DIR"] = str(scratch / "generated")
    os.environ["PROXBOX_DATABASE_PATH"] = str(scratch / "forbidden.sqlite3")
    offline_guard(root, scratch)
    sys.path.insert(0, str(root))
    from proxbox_api.operation_inventory.collection import collect
    from proxbox_api.operation_inventory.coverage import check_coverage
    from proxbox_api.operation_inventory.provenance import regular_file
    from proxbox_api.operation_inventory.schema import canonical, load_coverage, load_inventory

    if action == "readiness":
        from proxbox_api.operation_inventory.verification import verify_sources

        inventory = load_inventory(
            regular_file(root / "contracts/mounted-operations.json").read_bytes()
        )
        verify_sources(inventory, root)
        coverage = load_coverage(
            regular_file(root / "contracts/operation-coverage.json").read_bytes()
        )
        missing = check_coverage(inventory, coverage)
        print(f"Unresolved required operation columns: {len(missing)}")
        (scratch / "unresolved.json").write_bytes(canonical(missing))
        return 2 if missing else 0
    inventory = collect(root)
    prepare(scratch, inventory)
    return 0


def child_environment(scratch: Path) -> dict[str, str]:
    """Do not inherit credentials, provider configuration, or cache selectors."""
    return {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(scratch),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
    }


def prepare(scratch: Path, inventory: Inventory) -> None:
    """Render validated artifacts inside the already isolated child."""
    from proxbox_api.operation_inventory.coverage import unresolved
    from proxbox_api.operation_inventory.rendering import render
    from proxbox_api.operation_inventory.schema import (
        CoverageDocument,
        Inventory,
        canonical,
    )

    files = {
        "mounted-operations.json": canonical(inventory.model_dump()),
        "mounted-operations.en.md": render(inventory, "en"),
        "mounted-operations.pt-BR.md": render(inventory, "pt-BR"),
        "mounted-operations.schema.json": canonical(Inventory.model_json_schema()),
        "operation-coverage.schema.json": canonical(CoverageDocument.model_json_schema()),
    }
    integrity = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files["operation-inventory-integrity.json"] = canonical(integrity)
    files["operation-coverage.json"] = canonical(unresolved(inventory).model_dump())
    for name, data in files.items():
        (scratch / name).write_bytes(data)


def publish(root: Path, scratch: Path, action: str) -> None:
    """Copy explicit outputs without importing application code in the parent."""
    names = (
        "mounted-operations.json",
        "mounted-operations.en.md",
        "mounted-operations.pt-BR.md",
        "mounted-operations.schema.json",
        "operation-coverage.schema.json",
        "operation-inventory-integrity.json",
    )
    for name in names:
        path = root / "contracts" / name
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("Artifact is a symlink")
        data = (scratch / name).read_bytes()
        if action == "verify":
            if not path.is_file() or path.read_bytes() != data:
                raise ValueError(f"Stale or missing artifact: {name}")
        else:
            path.write_bytes(data)
    coverage = root / "contracts/operation-coverage.json"
    if any(part.is_symlink() for part in (coverage, *coverage.parents)):
        raise ValueError("Coverage artifact is a symlink")
    if action == "generate" and not coverage.exists():
        coverage.write_bytes((scratch / "operation-coverage.json").read_bytes())


def main() -> int:
    """Run explicit offline generation, drift verification, or coverage readiness."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "verify", "readiness", "_worker"))
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--worker-action", choices=("generate", "verify", "readiness"))
    arguments = parser.parse_args()
    root = arguments.root.absolute()
    if arguments.action == "_worker":
        if arguments.scratch is None or arguments.worker_action is None:
            parser.error("Internal worker arguments are required")
        return worker(root, arguments.scratch, arguments.worker_action)
    with tempfile.TemporaryDirectory(prefix="proxbox-operation-inventory-") as directory:
        scratch = Path(directory)
        command = [
            sys.executable,
            "-I",
            str(Path(__file__).absolute()),
            "_worker",
            "--root",
            str(root),
            "--scratch",
            str(scratch),
            "--worker-action",
            arguments.action,
        ]
        result = subprocess.run(command, env=child_environment(scratch), check=False, timeout=1200)
        if result.returncode or arguments.action == "readiness":
            return result.returncode
        publish(root, scratch, arguments.action)
    print("MOUNTED_OPERATION_INVENTORY_" + arguments.action.upper() + "_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
