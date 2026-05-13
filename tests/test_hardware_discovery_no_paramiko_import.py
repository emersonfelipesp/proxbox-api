"""Static guard: proxbox_api must not import paramiko or any SSH transport.

The SSH boundary lives in ``proxmox-sdk``. proxbox-api is a pure consumer that
imports :class:`proxmox_sdk.ssh.RemoteSSHClient`; the moment a file under
``proxbox_api/`` imports paramiko directly the "SSH lives in proxmox-sdk"
invariant breaks. This test walks the package AST and fails on any forbidden
import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FORBIDDEN = frozenset({"paramiko", "asyncssh", "fabric", "spurplus", "scrapli"})

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "proxbox_api"


def _iter_python_files() -> list[Path]:
    return [p for p in PACKAGE_ROOT.rglob("*.py") if p.is_file()]


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".", 1)[0])
    return names


@pytest.mark.parametrize(
    "path", _iter_python_files(), ids=lambda p: str(p.relative_to(PACKAGE_ROOT))
)
def test_no_ssh_transport_import(path: Path) -> None:
    found = _imports_in(path) & FORBIDDEN
    assert not found, (
        f"{path.relative_to(PACKAGE_ROOT)} imports forbidden SSH transport(s) {sorted(found)}; "
        "SSH must live in proxmox-sdk and be reached through proxmox_sdk.ssh."
    )
