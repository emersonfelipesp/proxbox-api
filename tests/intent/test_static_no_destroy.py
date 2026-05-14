"""Static guard: intent package must not issue Proxmox delete/destroy calls."""

from __future__ import annotations

import ast
from pathlib import Path

INTENT_ROOT = Path(__file__).parents[2] / "proxbox_api" / "routes" / "intent"
DESTROY_DISPATCHERS = {
    INTENT_ROOT / "dispatchers" / "qemu_destroy.py",
    INTENT_ROOT / "dispatchers" / "lxc_destroy.py",
}
TEXT_FORBIDDEN = (
    "qemu/vmid",
    ".delete(",
    "requests.delete",
    "vzdelete",
)
CALL_FORBIDDEN = {
    "delete",
    "destroy",
    "destroy_vm",
    "vzdelete",
}


def _source_files() -> list[Path]:
    return [
        path
        for path in INTENT_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
        and not path.name.startswith("test_")
        and path not in DESTROY_DISPATCHERS
    ]


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def test_intent_package_contains_no_proxmox_destroy_calls():
    failures: list[str] = []
    for path in _source_files():
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        for needle in TEXT_FORBIDDEN:
            if needle in lowered:
                failures.append(f"{path.relative_to(INTENT_ROOT)} contains {needle!r}")

        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name and name.lower() in CALL_FORBIDDEN:
                failures.append(f"{path.relative_to(INTENT_ROOT)} calls forbidden method {name!r}")
            if name == "request" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and str(first.value).upper() == "DELETE":
                    failures.append(f"{path.relative_to(INTENT_ROOT)} issues a DELETE request call")

    assert failures == []
