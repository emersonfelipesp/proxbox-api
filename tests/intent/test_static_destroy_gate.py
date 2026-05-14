"""Static guard: Proxmox destroy dispatch may only live in deletion dispatchers."""

from __future__ import annotations

import re
from pathlib import Path

PROXBOX_ROOT = Path(__file__).parents[2] / "proxbox_api"
ALLOWLIST = {
    Path("routes/intent/dispatchers/qemu_destroy.py"),
    Path("routes/intent/dispatchers/lxc_destroy.py"),
}
DESTROY_PATTERNS = (
    re.compile(r"\.delete\s*\(.*nodes"),
    re.compile(r"\.qemu\.delete\("),
    re.compile(r"\.lxc\.delete\("),
    re.compile(r"requests\.delete\(.*nodes/"),
    re.compile(r"httpx\.delete\(.*nodes/"),
)


def _source_files() -> list[Path]:
    return [
        path
        for path in PROXBOX_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
        and "generated" not in path.relative_to(PROXBOX_ROOT).parts
    ]


def test_destroy_calls_are_only_in_delete_dispatchers():
    failures: list[str] = []
    for path in _source_files():
        relative = path.relative_to(PROXBOX_ROOT)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not any(pattern.search(line) for pattern in DESTROY_PATTERNS):
                continue
            if relative not in ALLOWLIST:
                failures.append(f"{relative}:{line_number}: {line.strip()}")

    assert failures == []
