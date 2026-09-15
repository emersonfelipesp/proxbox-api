"""Tests for the `proxbox-schema` argparse-based CLI."""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api import schema_cli


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["proxbox-schema", *argv])
    return schema_cli.main()


def test_no_command_prints_help_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(monkeypatch)
    captured = capsys.readouterr()
    assert rc == 0
    assert "usage: proxbox-schema" in captured.out


def test_list_command_prints_bundled_versions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(monkeypatch, "list")
    captured = capsys.readouterr()
    assert rc == 0
    assert "Available Proxmox OpenAPI schema versions" in captured.out


@pytest.mark.parametrize("command", ["list", "status"])
def test_default_discovery_never_reads_user_generated_directory(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    def unexpected_user_directory():
        raise AssertionError("default CLI discovery read the user-generated directory")

    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.get_user_generated_dir",
        unexpected_user_directory,
    )

    assert _run(monkeypatch, command) == 0


@pytest.mark.parametrize("command", ["list", "status"])
def test_include_user_requires_runtime_codegen_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.delenv("PROXBOX_RUNTIME_CODEGEN_ENABLED", raising=False)

    rc = _run(monkeypatch, command, "--include-user")

    assert rc == 2
    assert "--include-user requires PROXBOX_RUNTIME_CODEGEN_ENABLED=true" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "expected"),
    [("list", "user-generated"), ("status", "User-generated versions: 9.1-user")],
)
def test_include_user_labels_user_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
    command: str,
    expected: str,
) -> None:
    monkeypatch.setenv("PROXBOX_RUNTIME_CODEGEN_ENABLED", "true")
    paths = {}
    for version in ("8.3", "9.1-user"):
        path = tmp_path / version / "openapi.json"
        path.parent.mkdir()
        path.write_text("{}", encoding="utf-8")
        paths[version] = path
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.available_proxmox_sdk_versions",
        lambda **kwargs: ["8.3", "9.1-user"],
    )
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.has_bundled_proxmox_schema",
        lambda version: version == "8.3",
    )
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.proxmox_generated_openapi_path",
        lambda version_tag, **kwargs: paths[version_tag],
    )
    monkeypatch.setattr(
        "proxbox_api.schema_version_manager.get_all_generation_statuses",
        lambda: {},
    )

    assert _run(monkeypatch, command, "--include-user") == 0
    assert expected in capsys.readouterr().out


def test_list_command_returns_one_when_no_versions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.available_proxmox_sdk_versions",
        lambda **kwargs: [],
    )
    rc = _run(monkeypatch, "list")
    captured = capsys.readouterr()
    assert rc == 1
    assert "No bundled" in captured.out


def test_status_command_reports_versions_and_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.available_proxmox_sdk_versions",
        lambda **kwargs: ["8.3", "8.4"],
    )
    monkeypatch.setattr(
        "proxbox_api.schema_version_manager.get_all_generation_statuses",
        lambda: {"8.5": {"status": "running"}},
    )
    rc = _run(monkeypatch, "status")
    captured = capsys.readouterr()
    assert rc == 0
    assert "Bundled versions: 8.3, 8.4" in captured.out
    assert "8.5: running" in captured.out


def test_status_command_with_no_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.available_proxmox_sdk_versions",
        lambda **kwargs: ["8.3"],
    )
    monkeypatch.setattr(
        "proxbox_api.schema_version_manager.get_all_generation_statuses",
        lambda: {},
    )
    rc = _run(monkeypatch, "status")
    captured = capsys.readouterr()
    assert rc == 0
    assert "No active or recent generation tasks" in captured.out


def test_generate_skips_when_schema_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "proxbox_api.schema_version_manager.has_schema_for_release",
        lambda tag, **kwargs: True,
    )
    rc = _run(monkeypatch, "generate", "8.4")
    captured = capsys.readouterr()
    assert rc == 0
    assert "already exists" in captured.out


def test_generate_invokes_pipeline_and_reports_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "proxbox_api.schema_version_manager.has_schema_for_release",
        lambda tag, **kwargs: False,
    )

    class _FakeBundle:
        version_tag = "8.4"
        endpoint_count = 12
        operation_count = 34
        capture: dict[str, Any] = {
            "viewer": {"duration_seconds": 1.5},
            "completeness": {"fallback_method_count": 0},
        }

    captured_kwargs: dict[str, Any] = {}

    def fake_pipeline(**kwargs: Any) -> _FakeBundle:
        captured_kwargs.update(kwargs)
        return _FakeBundle()

    monkeypatch.setattr(
        "proxbox_api.proxmox_codegen.pipeline.generate_proxmox_codegen_bundle",
        fake_pipeline,
    )

    rc = _run(
        monkeypatch,
        "generate",
        "8.4",
        "--output-dir",
        str(tmp_path),
        "--workers",
        "2",
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "Generation completed for Proxmox 8.4" in captured.out
    assert captured_kwargs["version_tag"] == "8.4"
    assert captured_kwargs["worker_count"] == 2
    assert captured_kwargs["output_dir"] == tmp_path


def test_generate_returns_one_when_pipeline_raises(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "proxbox_api.schema_version_manager.has_schema_for_release",
        lambda tag, **kwargs: False,
    )

    def boom(**kwargs: Any) -> None:
        raise RuntimeError("crawler offline")

    monkeypatch.setattr(
        "proxbox_api.proxmox_codegen.pipeline.generate_proxmox_codegen_bundle", boom
    )

    rc = _run(monkeypatch, "generate", "8.4", "--output-dir", str(tmp_path))
    captured = capsys.readouterr()
    assert rc == 1
    assert "Generation failed: crawler offline" in captured.err


def test_generate_refuses_bundled_version_even_with_force(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.has_bundled_proxmox_schema",
        lambda tag: True,
    )

    rc = _run(monkeypatch, "generate", "8.3", "--force")
    captured = capsys.readouterr()

    assert rc == 1
    assert "is immutable" in captured.err


def test_quarantine_legacy_command_reports_renamed_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    quarantined = tmp_path / "pydantic_models.py.quarantined-20260914T000000Z"
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.quarantine_legacy_codegen_artifacts",
        lambda: [quarantined],
    )

    rc = _run(monkeypatch, "quarantine-legacy")
    captured = capsys.readouterr()

    assert rc == 0
    assert "Quarantined 1 legacy Proxmox codegen artifact" in captured.out
    assert str(quarantined) in captured.out
