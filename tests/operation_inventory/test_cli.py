"""CLI orchestration branches; real isolation is separately subprocess-tested."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from proxbox_api.operation_inventory.schema import canonical, load_inventory
from scripts import mounted_operation_inventory as cli


@pytest.mark.parametrize(
    "action,code", [("generate", 0), ("verify", 0), ("readiness", 2), ("generate", 7)]
)
def test_parent_command_and_failure_propagation(tmp_path, monkeypatch, action, code):
    published = []

    def child(command, *, env, check, timeout):
        assert command[1:4] == ["-I", cli.__file__, "_worker"]
        assert command[-2:] == ["--worker-action", action]
        assert set(env) == {
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHON_DOTENV_DISABLED",
        }
        assert env["PYTHON_DOTENV_DISABLED"] == "1"
        assert check is False and timeout == 1200
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(cli.sys, "argv", ["inventory", action, "--root", str(tmp_path)])
    monkeypatch.setattr(cli.subprocess, "run", child)
    monkeypatch.setattr(cli, "publish", lambda root, scratch, mode: published.append((root, mode)))
    assert cli.main() == code
    assert published == ([(tmp_path, action)] if code == 0 and action != "readiness" else [])


def test_internal_worker_requires_explicit_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["inventory", "_worker", "--root", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_internal_worker_explicit_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "inventory",
            "_worker",
            "--root",
            str(tmp_path),
            "--scratch",
            str(tmp_path),
            "--worker-action",
            "readiness",
        ],
    )
    monkeypatch.setattr(cli, "worker", lambda root, scratch, action: 2)
    assert cli.main() == 2


def test_worker_cleans_environment_before_collection(tmp_path, monkeypatch, minimal):
    from proxbox_api.operation_inventory import collection, verification

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (tmp_path / "contracts").mkdir()
    inventory = load_inventory(canonical(minimal))
    events = []
    monkeypatch.setattr(cli.os, "environ", dict(os.environ, CANARY_SECRET="PRIVATE"))
    monkeypatch.setattr(cli.sys, "path", list(cli.sys.path))
    monkeypatch.setattr(cli.sys, "dont_write_bytecode", False)

    def guard(root, owned):
        assert "CANARY_SECRET" not in cli.os.environ
        assert cli.sys.dont_write_bytecode is True
        events.append("guard")

    def collect(root):
        assert events == ["guard"]
        return inventory

    monkeypatch.setattr(cli, "offline_guard", guard)
    monkeypatch.setattr(collection, "collect", collect)
    assert cli.worker(tmp_path, scratch, "generate") == 0
    cli.publish(tmp_path, scratch, "generate")
    monkeypatch.setattr(verification, "verify_sources", lambda current, root: None)
    assert cli.worker(tmp_path, scratch, "readiness") == 2
    assert (scratch / "unresolved.json").is_file()
