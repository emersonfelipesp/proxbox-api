"""Static guards for UPDATE TOCTOU rechecks."""

from __future__ import annotations

from pathlib import Path

DISPATCHER_ROOT = Path(__file__).parents[2] / "proxbox_api" / "routes" / "intent" / "dispatchers"


def _source(name: str) -> str:
    return (DISPATCHER_ROOT / name).read_text(encoding="utf-8")


def test_qemu_update_contains_toctou_reread_before_put():
    source = _source("qemu_update.py")

    assert "TOCTOU" in source
    reread_index = source.index(".qemu.get()")
    put_index = source.index(".config.put(")
    assert reread_index < put_index


def test_lxc_update_contains_toctou_reread_before_put():
    source = _source("lxc_update.py")

    assert "TOCTOU" in source
    reread_index = source.index(".lxc.get()")
    put_index = source.index(".config.put(")
    assert reread_index < put_index
