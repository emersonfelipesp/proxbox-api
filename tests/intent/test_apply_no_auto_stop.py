"""Static guard: UPDATE dispatchers must never auto-stop guests."""

from __future__ import annotations

from pathlib import Path

DISPATCHER_ROOT = Path(__file__).parents[2] / "proxbox_api" / "routes" / "intent" / "dispatchers"
FORBIDDEN = (".stop(", "qemu_stop", "lxc_stop")


def test_update_dispatchers_do_not_call_stop():
    failures: list[str] = []
    for name in ("qemu_update.py", "lxc_update.py"):
        source = (DISPATCHER_ROOT / name).read_text(encoding="utf-8")
        lowered = source.lower()
        for needle in FORBIDDEN:
            if needle in lowered:
                failures.append(f"{name} contains {needle!r}")

    assert failures == []
