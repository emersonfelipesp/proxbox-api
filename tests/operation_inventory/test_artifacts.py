"""Independent artifact, rendering, readiness and stale-source boundaries."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from proxbox_api.operation_inventory.coverage import check_coverage, unresolved
from proxbox_api.operation_inventory.provenance import source
from proxbox_api.operation_inventory.rendering import cell, render
from proxbox_api.operation_inventory.schema import canonical, digest, load_coverage, load_inventory
from scripts import operation_inventory_docs as docs
from scripts.mounted_operation_inventory import prepare, publish


@pytest.fixture
def artifact_tree(tmp_path, minimal):
    root = tmp_path / "repository"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    (root / "contracts").mkdir()
    (root / "proxbox_api").mkdir()
    (root / "scripts").mkdir()
    (root / "fixture.py").write_text("# Synthetic callable source.\n")
    operation = next(iter(minimal["operations"].values()))
    operation["handler"]["source"] = source(root / "fixture.py", root).model_dump()
    key = digest(operation)
    minimal["operations"] = {key: operation}
    for row in minimal["modes"][0]["registrations"]:
        row["operation"] = key
    for name in ("pyproject.toml", "uv.lock", "contracts/operation-inventory-inputs.json"):
        (root / name).write_text("{}")
    minimal["provenance"]["sources"] = [
        source(root / name, root).model_dump() for name in sorted(docs._source_paths(root))
    ]
    for name in ("fastapi", "starlette"):
        path = root / name / "routing.py"
        path.parent.mkdir()
        path.write_text("# Synthetic framework routing evidence.\n")
        minimal["provenance"]["framework_sources"].append(source(path, root, name).model_dump())
    inventory = load_inventory(canonical(minimal))
    prepare(scratch, inventory)
    publish(root, scratch, "generate")
    return root, scratch, inventory


def fake_framework(monkeypatch, root):
    monkeypatch.setattr(
        docs.importlib.metadata,
        "distribution",
        lambda _: SimpleNamespace(locate_file=lambda path: root / path),
    )


def test_artifact_publish_verify_and_existing_coverage_preservation(artifact_tree, monkeypatch):
    root, scratch, _ = artifact_tree
    fake_framework(monkeypatch, root)
    docs.verify_artifacts(root)
    docs.on_pre_build(SimpleNamespace(config_file_path=str(root / "mkdocs.yml")))
    publish(root, scratch, "verify")
    coverage = root / "contracts/operation-coverage.json"
    coverage.write_bytes(b"HUMAN_OWNED_COVERAGE")
    publish(root, scratch, "generate")
    assert coverage.read_bytes() == b"HUMAN_OWNED_COVERAGE"


@pytest.mark.parametrize("exists", [True, False])
def test_coverage_symlink_is_never_followed(artifact_tree, exists):
    root, scratch, _ = artifact_tree
    coverage = root / "contracts/operation-coverage.json"
    coverage.unlink()
    outside = root / "human-owned.json"
    if exists:
        outside.write_bytes(b"HUMAN_OWNED")
    coverage.symlink_to(outside)
    with pytest.raises(ValueError, match="Coverage artifact"):
        publish(root, scratch, "generate")
    assert outside.exists() is exists
    if exists:
        assert outside.read_bytes() == b"HUMAN_OWNED"


@pytest.mark.parametrize(
    "mutation", ["table", "missing", "source", "new-source", "framework", "symlink", "callable"]
)
def test_docs_rejects_actual_stale_or_missing_artifact(artifact_tree, monkeypatch, mutation):
    root, _, _ = artifact_tree
    fake_framework(monkeypatch, root)
    table = root / "contracts/mounted-operations.en.md"
    targets = {
        "table": table,
        "source": root / "uv.lock",
        "new-source": root / "proxbox_api/new.py",
        "framework": root / "fastapi/routing.py",
        "callable": root / "fixture.py",
    }
    if mutation in targets:
        targets[mutation].write_bytes(b"STALE_CANARY")
    elif mutation == "missing":
        table.unlink()
    else:
        table.rename(root / "table.md")
        table.symlink_to(root / "table.md")
    with pytest.raises(ValueError):
        docs.verify_artifacts(root)


@pytest.mark.parametrize("mutation", ["missing", "stale", "symlink"])
def test_cli_drift_verification_refuses_invalid_output(artifact_tree, mutation):
    root, scratch, _ = artifact_tree
    path = root / "contracts/mounted-operations.json"
    if mutation == "missing":
        path.unlink()
    elif mutation == "stale":
        path.write_bytes(b"{}")
    else:
        path.rename(root / "saved.json")
        path.symlink_to(root / "saved.json")
    with pytest.raises(ValueError):
        publish(root, scratch, "verify")


def test_rendered_bilingual_rows_and_untrusted_markup(minimal):
    from markdown import markdown

    inventory = load_inventory(canonical(minimal))
    for language, heading in (("en", "Effective path"), ("pt-BR", "Caminho efetivo")):
        html = markdown(render(inventory, language).decode(), extensions=["tables"])
        assert f"<th>{heading}</th>" in html
        assert html.count("<td>/fixed/{item}</td>") == 2
        assert "fixture.py:1 fixed" in html
    with pytest.raises(ValueError):
        render(inventory, "unsupported")
    html = markdown(
        "| Input |\n|---|\n| " + cell("<script>[x](https://invalid)|`*_\\\n") + " |",
        extensions=["tables"],
    )
    assert "<script>" not in html
    assert "<a " not in html
    assert "<code>" not in html
    assert "<em>" not in html


def test_readiness_requires_complete_evidenced_columns_and_exact_hash(minimal):
    inventory = load_inventory(canonical(minimal))
    wire = unresolved(inventory).model_dump()
    record = wire["records"][0]
    for name, value in record.items():
        if type(value) is dict:
            value.update(
                status="evidenced",
                rationale="Synthetic boundary proof only.",
                citations=["tests/fixture.py:1"],
            )
    record["effects"] = ["no_external_effect"]
    assert check_coverage(inventory, load_coverage(canonical(wire))) == []
    wire["inventory_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        check_coverage(inventory, load_coverage(canonical(wire)))


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b"[]", b"{"])
def test_build_hook_strict_json_input(tmp_path, raw):
    path = tmp_path / "input.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        docs._json(path)


def test_integrity_manifest_is_exact_and_independently_hashed(artifact_tree, monkeypatch):
    root, _, _ = artifact_tree
    fake_framework(monkeypatch, root)
    manifest = root / "contracts/operation-inventory-integrity.json"
    data = docs._json(manifest)
    assert set(data) == {
        "mounted-operations.json",
        "mounted-operations.en.md",
        "mounted-operations.pt-BR.md",
        "mounted-operations.schema.json",
        "operation-coverage.schema.json",
    }
    for name, expected in data.items():
        assert hashlib.sha256((root / "contracts" / name).read_bytes()).hexdigest() == expected
    data["unexpected"] = "0" * 64
    manifest.write_bytes(canonical(data))
    with pytest.raises(ValueError):
        docs.verify_artifacts(root)


@pytest.mark.parametrize("mutation", ["none", "source", "dependency", "framework", "callable"])
def test_readiness_current_source_and_dependency_checks(artifact_tree, monkeypatch, mutation):
    from proxbox_api.operation_inventory import verification
    from proxbox_api.operation_inventory.schema import digest

    root, _, inventory = artifact_tree
    (root / "fixture.py").write_text("# Callable source evidence.\n")
    wire = inventory.model_dump()
    operation = next(iter(wire["operations"].values()))
    operation["handler"]["source"] = source(root / "fixture.py", root).model_dump()
    key = digest(operation)
    wire["operations"] = {key: operation}
    for row in wire["modes"][0]["registrations"]:
        row["operation"] = key
    wire["provenance"]["sources"] = [
        source(root / name, root).model_dump()
        for name in (
            "pyproject.toml",
            "uv.lock",
            "contracts/operation-inventory-inputs.json",
        )
    ]
    current = load_inventory(canonical(wire))
    monkeypatch.setattr(
        verification, "dependencies", lambda _: ["foreign"] if mutation == "dependency" else []
    )
    monkeypatch.setattr(
        verification, "distribution_source", lambda owner, path: source(root / path, root, owner)
    )
    changed = {"source": "uv.lock", "framework": "fastapi/routing.py", "callable": "fixture.py"}
    if mutation in changed:
        (root / changed[mutation]).write_text("# Changed evidence.\n")
    if mutation == "none":
        verification.verify_sources(current, root)
    else:
        with pytest.raises(ValueError):
            verification.verify_sources(current, root)


@pytest.mark.parametrize(
    "mutation",
    ["version", "source-shape", "source-owner", "source-escape", "framework-set", "framework-path"],
)
def test_forged_inventory_manifest_does_not_bypass_build_checks(
    artifact_tree, monkeypatch, mutation
):
    root, _, _ = artifact_tree
    fake_framework(monkeypatch, root)
    path = root / "contracts/mounted-operations.json"
    value = docs._json(path)
    if mutation == "version":
        value["schema_version"] = True
    elif mutation == "source-shape":
        value["provenance"]["sources"] = None
    elif mutation == "source-owner":
        value["provenance"]["sources"][0]["owner"] = "foreign"
    elif mutation == "source-escape":
        value["provenance"]["sources"][0]["path"] = "../outside.py"
    elif mutation == "framework-set":
        value["provenance"]["framework_sources"] = []
    else:
        value["provenance"]["framework_sources"][0]["path"] = "../outside.py"
    path.write_bytes(canonical(value))
    manifest = root / "contracts/operation-inventory-integrity.json"
    integrity = docs._json(manifest)
    integrity["mounted-operations.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.write_bytes(canonical(integrity))
    with pytest.raises(ValueError):
        docs.verify_artifacts(root)


def test_build_input_byte_bound(tmp_path):
    path = tmp_path / "oversized.json"
    with path.open("wb") as stream:
        stream.truncate(96 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="byte limit"):
        docs._regular(path)


@pytest.mark.parametrize("path", ["/absolute", "../escape", "a//b", "a\\b"])
def test_callable_source_path_cannot_escape_build_owner(tmp_path, path):
    with pytest.raises(ValueError):
        docs._callable_file(tmp_path, {"owner": "repository", "path": path, "sha256": "1" * 64})


def test_build_distribution_callable_uses_exact_metadata_owner(artifact_tree, monkeypatch):
    root, _, _ = artifact_tree
    fake_framework(monkeypatch, root)
    row = source(root / "starlette/routing.py", root, "starlette").model_dump()
    assert docs._callable_file(root, row) == root / "starlette/routing.py"
    row["unknown"] = True
    with pytest.raises(ValueError):
        docs._callable_file(root, row)


def test_build_callable_symlink_escape_and_cached_repeated_source(artifact_tree):
    root, _, inventory = artifact_tree
    external = root.parent / "outside.py"
    external.write_text("# Outside the claimed source owner.\n")
    (root / "linked.py").symlink_to(external)
    with pytest.raises(ValueError, match="escaped"):
        docs._callable_file(root, {"owner": "repository", "path": "linked.py", "sha256": "1" * 64})
    operation = next(iter(inventory.model_dump()["operations"].values()))
    docs._check_callables(root, {"operations": {"first": operation, "second": operation}})
