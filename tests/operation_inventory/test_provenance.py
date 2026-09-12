"""Editable-source, stale-input, and restricted documentation evidence tests."""

from __future__ import annotations

import json
import sysconfig
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxbox_api.operation_inventory.provenance import editable_origin, regular_file
from proxbox_api.operation_inventory.schema import InventoryError
from scripts.operation_inventory_docs import _regular


def metadata_fixture(tmp_path, monkeypatch):
    from proxbox_api.operation_inventory import provenance

    names = ["fastapi", "starlette", "pydantic", "proxmox-sdk", "netbox-sdk", "proxbox-api"]
    lock = "\n".join(f'[[package]]\nname = "{name}"\nversion = "1"' for name in names)
    (tmp_path / "uv.lock").write_text(lock)
    (tmp_path / "proxbox_api.egg-info").mkdir()
    (tmp_path / "proxbox_api.egg-info/PKG-INFO").write_text("Name: proxbox-api\nVersion: 1\n")
    rows = [SimpleNamespace(metadata={"Name": name}, version="1") for name in names[:-1]]
    rows += [
        SimpleNamespace(
            metadata={"Name": "proxbox-api"},
            version="1",
            locate_file=lambda _: tmp_path,
            read_text=lambda _: None,
        ),
        SimpleNamespace(
            metadata={"Name": "proxbox-api"},
            version="1",
            locate_file=lambda _: Path(sysconfig.get_path("purelib")),
            read_text=lambda _: json.dumps(
                {"dir_info": {"editable": True}, "url": tmp_path.as_uri()}
            ),
        ),
    ]
    monkeypatch.setattr(provenance.importlib.metadata, "distributions", lambda **_: rows)
    return rows


def test_dependency_discovery_deduplicates_paths_not_distribution_rows(tmp_path, monkeypatch):
    from proxbox_api.operation_inventory import provenance

    rows = metadata_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(provenance.sys, "path", [str(tmp_path), str(tmp_path / ".")])

    def discover(*, path):
        assert path == [str(tmp_path)]
        return rows

    monkeypatch.setattr(provenance.importlib.metadata, "distributions", discover)
    assert len(provenance.dependencies(tmp_path)) == 6
    rows.append(rows[-1])
    with pytest.raises(InventoryError, match="Repeated editable"):
        provenance.dependencies(tmp_path)


@pytest.mark.parametrize("mutation", ["version", "origin", "missing", "duplicate"])
def test_conflicting_or_missing_distribution_identity_fails(tmp_path, monkeypatch, mutation):
    from proxbox_api.operation_inventory import provenance

    rows = metadata_fixture(tmp_path, monkeypatch)
    if mutation == "version":
        rows[-1].version = "2"
    elif mutation == "origin":
        rows[-1].locate_file = lambda _: tmp_path / "foreign"
    elif mutation == "missing":
        rows.pop()
    else:
        rows.append(rows[0])
    with pytest.raises(InventoryError):
        provenance.dependencies(tmp_path)


def test_conflicting_editable_versions_fail_even_when_both_versions_are_locked(
    tmp_path, monkeypatch
):
    from proxbox_api.operation_inventory import provenance

    rows = metadata_fixture(tmp_path, monkeypatch)
    lock = tmp_path / "uv.lock"
    lock.write_text(lock.read_text() + '\n[[package]]\nname = "proxbox-api"\nversion = "2"\n')
    rows[-1].version = "2"
    with pytest.raises(InventoryError, match="Conflicting editable versions"):
        provenance.dependencies(tmp_path)


def test_same_source_editable_metadata_is_supported(tmp_path):
    directory = tmp_path / "proxbox_api.egg-info"
    directory.mkdir()
    (directory / "PKG-INFO").write_text("Name: proxbox_api\nVersion: 0.0.22rc5\n")
    source = SimpleNamespace(locate_file=lambda _: tmp_path, read_text=lambda _: None)
    installed = SimpleNamespace(
        locate_file=lambda _: Path(sysconfig.get_path("purelib")),
        read_text=lambda _: json.dumps({"dir_info": {"editable": True}, "url": tmp_path.as_uri()}),
    )
    assert editable_origin(source, tmp_path) == "source"
    assert editable_origin(installed, tmp_path) == "installed"


@pytest.mark.parametrize(
    "wire",
    [
        {"dir_info": {"editable": True}, "url": "file:///foreign/source"},
        {"dir_info": {"editable": False}, "url": "file:///foreign/source"},
        {"dir_info": {"editable": 1}, "url": "file:///foreign/source"},
        {"url": "file:///foreign/source"},
    ],
)
def test_foreign_editable_metadata_fails(tmp_path, wire):
    installed = SimpleNamespace(
        locate_file=lambda _: Path(sysconfig.get_path("purelib")),
        read_text=lambda _: json.dumps(wire),
    )
    with pytest.raises(InventoryError):
        editable_origin(installed, tmp_path)


def test_foreign_metadata_origin_fails(tmp_path):
    distribution = SimpleNamespace(
        locate_file=lambda _: tmp_path / "foreign", read_text=lambda _: None
    )
    with pytest.raises(InventoryError):
        editable_origin(distribution, tmp_path)


def test_editable_boolean_alias_is_not_accepted(tmp_path):
    distribution = SimpleNamespace(
        locate_file=lambda _: Path(sysconfig.get_path("purelib")),
        read_text=lambda _: json.dumps({"dir_info": {"editable": 1}, "url": tmp_path.as_uri()}),
    )
    with pytest.raises(InventoryError):
        editable_origin(distribution, tmp_path)


def test_actual_locked_environment_matches_selected_source():
    from proxbox_api.operation_inventory.provenance import dependencies

    records = dependencies(Path(__file__).absolute().parents[2])
    actual = {row.name: row.version for row in records}
    project = Path(__file__).absolute().parents[2] / "pyproject.toml"
    assert actual["proxbox-api"] == tomllib.loads(project.read_text())["project"]["version"]
    assert actual["fastapi"] == "0.137.2"
    assert actual["starlette"] == "1.3.1"
    assert actual["netbox-sdk"] == "0.0.13"


def test_framework_callable_identity_never_includes_nested_environment_path():
    from starlette.staticfiles import StaticFiles

    from proxbox_api.operation_inventory.provenance import callable_identity

    identity = callable_identity(StaticFiles.__call__, Path(__file__).absolute().parents[2])
    assert identity.source.owner == "starlette"
    assert identity.source.path == "starlette/staticfiles.py"
    assert ".venv" not in identity.source.path


def test_noncallable_or_source_less_package_is_not_accepted(monkeypatch):
    import starlette
    from starlette.staticfiles import StaticFiles

    from proxbox_api.operation_inventory.provenance import callable_identity

    root = Path(__file__).absolute().parents[2]
    with pytest.raises(InventoryError, match="not callable"):
        callable_identity(object(), root)
    monkeypatch.setattr(starlette, "__file__", None)
    with pytest.raises(InventoryError, match="no inspectable file"):
        callable_identity(StaticFiles.__call__, root)


def test_uninspectable_callable_and_foreign_first_party_imports_fail(monkeypatch):
    import sys

    from proxbox_api.operation_inventory.provenance import callable_identity, imported_sources

    root = Path(__file__).absolute().parents[2]
    with pytest.raises(InventoryError, match="inspectable"):
        callable_identity(len, root)
    monkeypatch.setitem(
        sys.modules, "proxbox_api.foreign_fixture", SimpleNamespace(__file__="/foreign/source.py")
    )
    with pytest.raises(InventoryError, match="escaped"):
        imported_sources(root)


def test_regular_file_and_parent_symlink_rejection(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    target = real / "input.json"
    target.write_text("{}")
    assert regular_file(target) == target
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    for checker in (regular_file, _regular):
        with pytest.raises(ValueError):
            checker(link / "input.json")
        with pytest.raises(ValueError):
            checker(tmp_path / "missing")


def test_snippets_missing_traversal_and_sibling_paths_fail(tmp_path):
    from markdown import Markdown
    from pymdownx.snippets import SnippetMissingError

    allowed = tmp_path / "contracts"
    allowed.mkdir()
    table = allowed / "table.md"
    table.write_text("| Protocol | Path |\n|---|---|\n| websocket | /fixed |\n")
    (tmp_path / "private.md").write_text("CANARY_DO_NOT_RENDER")
    markdown = Markdown(
        extensions=["tables", "pymdownx.snippets"],
        extension_configs={
            "pymdownx.snippets": {
                "base_path": [str(table)],
                "restrict_base_path": True,
                "check_paths": True,
                "url_download": False,
            }
        },
    )
    html = markdown.convert('--8<-- "table.md"')
    assert "<td>websocket</td>" in html
    assert "<td>/fixed</td>" in html
    for name in ("missing.md", "../private.md", "https://example.invalid/private.md"):
        markdown.reset()
        with pytest.raises(SnippetMissingError):
            markdown.convert(f'--8<-- "{name}"')
