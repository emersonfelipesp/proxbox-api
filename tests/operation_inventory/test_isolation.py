"""Real child-process isolation boundaries, without socket or provider contact."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.mounted_operation_inventory import _inside, child_environment


@pytest.mark.parametrize(
    "action",
    ["socket", "database", "read", "write", "process", "mkdir", "rename", "allowed", "dotenv"],
)
def test_actual_offline_guard_refuses_external_effects(tmp_path, action):
    scratch = tmp_path / "owned"
    scratch.mkdir()
    (scratch / "owned").write_bytes(b"owned")
    outside = tmp_path / "private"
    outside.write_bytes(b"PRIVATE_TOKEN=CANARY_SECRET\n")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("guard_probe.py")),
            str(scratch),
            action,
            str(outside),
        ],
        env=child_environment(scratch),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "ALLOWED_SCRATCH_ONLY" if action == "allowed" else "DENIED_BEFORE_EFFECT"
    )
    assert outside.read_bytes() == b"PRIVATE_TOKEN=CANARY_SECRET\n"
    assert not (tmp_path / "forbidden-directory").exists()


def test_guard_path_and_environment_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("CANARY_SECRET", "PRIVATE")
    assert "CANARY_SECRET" in os.environ
    assert "CANARY_SECRET" not in child_environment(tmp_path)
    assert not _inside(1, (tmp_path,))
    assert not _inside(str(tmp_path / "../foreign"), (tmp_path,))
    assert _inside(bytes(tmp_path / "owned"), (tmp_path,))


def test_guard_callback_branch_matrix_without_installing_global_hook(tmp_path, monkeypatch):
    from scripts import mounted_operation_inventory as cli

    hooks = []
    monkeypatch.setattr(cli.sys, "addaudithook", hooks.append)
    root = Path(__file__).absolute().parents[2]
    cli.offline_guard(root, tmp_path)
    assert len(hooks) == 1
    audit = hooks[0]
    for event, arguments in [
        ("open", (str(tmp_path / "owned"), "w", os.O_WRONLY)),
        ("open", (str(root / "uv.lock"), "r", 0)),
        ("os.mkdir", (str(tmp_path / "owned"),)),
        ("unrelated.event", ()),
    ]:
        audit(event, arguments)
    for event, arguments in [
        ("socket.__new__", ()),
        ("subprocess.Popen", ()),
        ("sqlite3.connect", ()),
        ("open", (str(root / "uv.lock"), "w", os.O_WRONLY)),
        ("open", (str(root / "uv.lock"), None, os.O_RDONLY | os.O_TRUNC)),
        ("open", (str(root / "uv.lock"), None, os.O_RDONLY | os.O_CREAT)),
        ("open", (1, "r", 0)),
        ("os.remove", (str(root / "uv.lock"),)),
    ]:
        with pytest.raises(PermissionError):
            audit(event, arguments)
