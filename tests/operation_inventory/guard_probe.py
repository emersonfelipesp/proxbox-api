"""Fixed subprocess-only isolation probe; it never contacts a managed service."""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parents[2]))

from scripts.mounted_operation_inventory import child_environment, offline_guard


def main() -> int:
    """Exercise an explicit prohibited action after installing the real guard."""
    root = Path(__file__).absolute().parents[2]
    scratch = Path(sys.argv[1])
    action = sys.argv[2]
    outside = Path(sys.argv[3])
    offline_guard(root, scratch)
    if action == "dotenv":
        from dotenv import load_dotenv

        assert load_dotenv(outside) is False
        assert "PRIVATE_TOKEN" not in os.environ
        print("DENIED_BEFORE_EFFECT")
        return 0
    actions = {
        "socket": socket.socket,
        "database": lambda: sqlite3.connect(":memory:"),
        "read": outside.read_bytes,
        "write": lambda: outside.write_bytes(b"MUST_NOT_WRITE"),
        "process": lambda: subprocess.run([sys.executable, "--version"], check=True),
        "mkdir": lambda: (outside.parent / "forbidden-directory").mkdir(),
        "rename": lambda: (scratch / "owned").rename(outside),
    }
    if action == "allowed":
        (scratch / "owned").write_bytes(b"owned")
        assert (scratch / "owned").read_bytes() == b"owned"
        assert "CANARY_SECRET" not in child_environment(scratch)
        print("ALLOWED_SCRATCH_ONLY")
        return 0
    try:
        actions[action]()
    except PermissionError:
        print("DENIED_BEFORE_EFFECT")
        return 0
    raise AssertionError("The actual isolation guard accepted a forbidden effect")


if __name__ == "__main__":
    raise SystemExit(main())
