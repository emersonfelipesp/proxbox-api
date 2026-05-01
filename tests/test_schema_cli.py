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


def test_list_command_returns_one_when_no_versions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.available_proxmox_sdk_versions",
        lambda: [],
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
        lambda: ["8.3", "8.4"],
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
        lambda: ["8.3"],
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
        "proxbox_api.schema_version_manager.has_schema_for_release", lambda tag: True
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
        "proxbox_api.schema_version_manager.has_schema_for_release", lambda tag: False
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
        "proxbox_api.schema_version_manager.has_schema_for_release", lambda tag: False
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
