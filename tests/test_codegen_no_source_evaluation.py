"""Security regressions for generated Proxmox models and artifact paths."""

from __future__ import annotations

import ast
import builtins
import json
import multiprocessing
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import CodeType

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from proxbox_api.app import factory
from proxbox_api.proxmox_codegen import pipeline, pydantic_generator, security
from proxbox_api.proxmox_codegen.pydantic_generator import (
    build_pydantic_models_from_openapi,
    generate_pydantic_models_from_openapi,
)
from proxbox_api.proxmox_codegen.security import (
    MAX_DOCUMENT_BYTES,
    MAX_PROPERTIES_PER_SCHEMA,
    SchemaLimitError,
    SchemaShapeError,
    resolve_contained,
)
from proxbox_api.proxmox_to_netbox import proxmox_schema
from proxbox_api.routes.proxmox import runtime_generated, viewer_codegen

_UNUSUAL_PROPERTY_NAME = 'field")\ncodegen_security_sentinel()\n#'


def _runtime_viewer_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(viewer_codegen.common_router, prefix="/proxmox/viewer")
    app.include_router(viewer_codegen.runtime_codegen_router, prefix="/proxmox/viewer")
    return app


def _bundled_viewer_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(viewer_codegen.common_router, prefix="/proxmox/viewer")
    app.include_router(viewer_codegen.bundled_only_router, prefix="/proxmox/viewer")
    return app


def test_exported_viewer_router_never_mounts_runtime_codegen_routes() -> None:
    app = FastAPI()
    app.include_router(viewer_codegen.router, prefix="/proxmox/viewer")

    mounted = {
        (method, route.path)
        for context in iter_route_contexts(app.routes)
        for route in [
            getattr(getattr(context, "_route_context", None), "starlette_route", None) or context
        ]
        for method in getattr(route, "methods", set())
    }

    assert ("POST", "/proxmox/viewer/generate") not in mounted
    assert ("POST", "/proxmox/viewer/routes/refresh") not in mounted
    assert ("GET", "/proxmox/viewer/openapi") in mounted
    assert ("GET", "/proxmox/viewer/pydantic") in mounted


def _openapi_with_unusual_property_name() -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "test"},
        "paths": {
            "/security-test": {
                "get": {
                    "operationId": "get_security_test",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            _UNUSUAL_PROPERTY_NAME: {
                                                "type": "string",
                                                "description": 'Literal "quote", newline\n# marker.',
                                            }
                                        },
                                        "required": [_UNUSUAL_PROPERTY_NAME],
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


def test_builder_treats_property_names_as_data(monkeypatch):
    sentinel = {"called": False}

    def _mark_called() -> None:
        sentinel["called"] = True

    monkeypatch.setattr(builtins, "codegen_security_sentinel", _mark_called, raising=False)

    models = build_pydantic_models_from_openapi(_openapi_with_unusual_property_name())
    response_model = models["GetSecurityTestResponse"]
    payload = response_model.model_validate({_UNUSUAL_PROPERTY_NAME: "literal value"})

    assert sentinel == {"called": False}
    assert payload.model_dump(by_alias=True) == {_UNUSUAL_PROPERTY_NAME: "literal value"}
    assert {_field.alias for _field in response_model.model_fields.values()} == {
        _UNUSUAL_PROPERTY_NAME
    }


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    value: ast.expr = node.func
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"
    return aliases


def _resolved_call_name(node: ast.Call, aliases: dict[str, str]) -> str:
    name = _call_name(node)
    root, separator, remainder = name.partition(".")
    resolved_root = aliases.get(root, root)
    return resolved_root + (separator + remainder if separator else "")


def _dynamic_source_violations(source: str, path: str = "fixture.py") -> list[str]:
    tree = ast.parse(source, filename=path)
    aliases = _import_aliases(tree)
    violations: list[str] = []
    prohibited = {
        "__import__",
        "builtins.__import__",
        "builtins.compile",
        "builtins.eval",
        "builtins.exec",
        "compile",
        "eval",
        "exec",
        "importlib.import_module",
        "pickle.loads",
        "runpy.run_module",
        "runpy.run_path",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _resolved_call_name(node, aliases)
        getattr_target = _prohibited_getattr_target(node, aliases)
        unsafe_yaml = name == "yaml.load" and not _uses_safe_yaml_loader(node)
        if name in prohibited or name.endswith(".exec_module") or getattr_target or unsafe_yaml:
            violations.append(f"{path}:{node.lineno}:{getattr_target or name}")
    return violations


def _prohibited_getattr_target(node: ast.Call, aliases: dict[str, str]) -> str | None:
    name = _resolved_call_name(node, aliases)
    if name not in {"getattr", "builtins.getattr"} or len(node.args) < 2:
        return None
    owner = node.args[0]
    attribute = node.args[1]
    if not isinstance(owner, ast.Name) or not isinstance(attribute, ast.Constant):
        return None
    if aliases.get(owner.id, owner.id) != "builtins" or not isinstance(attribute.value, str):
        return None
    target = f"builtins.{attribute.value}"
    return target if target in {"builtins.compile", "builtins.eval", "builtins.exec"} else None


def _uses_safe_yaml_loader(node: ast.Call) -> bool:
    for keyword_arg in node.keywords:
        if keyword_arg.arg != "Loader":
            continue
        loader = keyword_arg.value
        if isinstance(loader, ast.Name):
            return loader.id == "SafeLoader"
        if isinstance(loader, ast.Attribute):
            return loader.attr == "SafeLoader"
    return False


def test_codegen_runtime_has_no_dynamic_source_or_unsafe_deserialization_calls():
    roots = [Path("proxbox_api/proxmox_codegen")]
    paths = [
        Path("proxbox_api/routes/proxmox/runtime_generated.py"),
        Path("proxbox_api/routes/proxmox/viewer_codegen.py"),
        Path("proxbox_api/proxmox_to_netbox/proxmox_schema.py"),
        Path("proxbox_api/schema_version_manager.py"),
        Path("proxbox_api/app/factory.py"),
        Path("proxbox_api/schema_cli.py"),
    ]
    paths.extend(path for root in roots for path in root.rglob("*.py"))
    violations: list[str] = []

    for path in sorted(set(paths)):
        violations.extend(_dynamic_source_violations(path.read_text(encoding="utf-8"), str(path)))

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        "import builtins\ngetattr(builtins, 'eval')('value')",
        "import builtins as safe_name\nsafe_name.exec('value')",
        "from builtins import compile as safe_name\nsafe_name('value', 'x', 'exec')",
        "__import__('module_name')",
        "import runpy\nrunpy.run_path('module.py')",
        "from runpy import run_module as safe_name\nsafe_name('module_name')",
        "import importlib as safe_name\nsafe_name.import_module('module_name')",
        "loader.exec_module(module)",
    ],
)
def test_dynamic_source_ast_guard_detects_indirect_forms(source: str):
    assert _dynamic_source_violations(source)


def test_runtime_loader_treats_hostile_alias_as_data_and_preserves_environment(monkeypatch):
    sentinel_name = "PROXBOX_CODEGEN_SECURITY_SENTINEL"
    monkeypatch.setenv(sentinel_name, "unchanged")

    module = runtime_generated._load_model_module(
        _openapi_with_unusual_property_name(),
        version_tag="security-oracle",
    )
    response_model = module.GetSecurityTestResponse
    payload = response_model.model_validate({_UNUSUAL_PROPERTY_NAME: "literal value"})

    assert payload.model_dump(by_alias=True) == {_UNUSUAL_PROPERTY_NAME: "literal value"}
    assert os.environ[sentinel_name] == "unchanged"


def test_text_renderer_quotes_unusual_property_name_as_data():
    rendered = generate_pydantic_models_from_openapi(_openapi_with_unusual_property_name())
    compiled = compile(rendered, "<generated-proxmox-models>", "exec")
    parsed = ast.parse(rendered)
    aliases = {
        keyword.value.value
        for keyword in ast.walk(parsed)
        if isinstance(keyword, ast.keyword)
        and keyword.arg == "alias"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    }

    assert isinstance(compiled, CodeType)
    assert _UNUSUAL_PROPERTY_NAME in aliases


@pytest.mark.asyncio
async def test_codegen_routes_reject_invalid_version_tags_before_pipeline_or_filesystem(
    monkeypatch,
):
    async def _unexpected_pipeline(**kwargs):
        raise AssertionError("The pipeline must not run for an invalid version tag.")

    def _unexpected_mkdir(*args, **kwargs):
        raise AssertionError("The filesystem must not be touched for an invalid version tag.")

    monkeypatch.setattr(
        viewer_codegen, "generate_proxmox_codegen_bundle_async", _unexpected_pipeline
    )
    routes = ("/proxmox/viewer/generate", "/proxmox/viewer/routes/refresh")
    version_tags = ("../outside", "nested/tag", "nested\\tag", ".", "..")

    transport = httpx.ASGITransport(app=_runtime_viewer_test_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with monkeypatch.context() as path_monkeypatch:
            path_monkeypatch.setattr(Path, "mkdir", _unexpected_mkdir)
            for route in routes:
                for version_tag in version_tags:
                    response = await client.post(route, params={"version_tag": version_tag})

                    assert response.status_code == 422


@pytest.mark.asyncio
async def test_default_openapi_route_has_no_regeneration_or_unknown_tag_fallback(monkeypatch):
    async def _unexpected_pipeline(**kwargs):
        raise AssertionError("The default route must not reach the generation pipeline.")

    monkeypatch.setattr(
        viewer_codegen,
        "generate_proxmox_codegen_bundle_async",
        _unexpected_pipeline,
    )
    transport = httpx.ASGITransport(app=_bundled_viewer_test_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        regenerate = await client.get(
            "/proxmox/viewer/openapi",
            params={"regenerate": "true"},
        )
        unknown = await client.get(
            "/proxmox/viewer/openapi",
            params={"version_tag": "unknown-tag"},
        )

    assert regenerate.status_code == 404
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_generate_route_refuses_immutable_bundled_tag_before_pipeline_or_filesystem(
    monkeypatch,
):
    async def _unexpected_pipeline(**kwargs):
        raise AssertionError("The pipeline must not overwrite a bundled version tag.")

    def _unexpected_mkdir(*args, **kwargs):
        raise AssertionError("The filesystem must not be touched for a bundled version tag.")

    monkeypatch.setattr(
        viewer_codegen, "generate_proxmox_codegen_bundle_async", _unexpected_pipeline
    )

    transport = httpx.ASGITransport(app=_runtime_viewer_test_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with monkeypatch.context() as path_monkeypatch:
            path_monkeypatch.setattr(Path, "mkdir", _unexpected_mkdir)
            response = await client.post(
                "/proxmox/viewer/generate",
                params={"version_tag": "latest", "persist": "true"},
            )

    assert response.status_code == 409
    assert "bundled and immutable" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version_tag",
    ["../outside", "nested/tag", "nested\\tag", ".", ".."],
)
async def test_pipeline_rejects_invalid_version_tag_before_crawl(
    monkeypatch,
    tmp_path: Path,
    version_tag: str,
):
    def _unexpected_playwright_check() -> bool:
        raise AssertionError("Playwright availability must not be checked for an invalid tag.")

    monkeypatch.setattr(pipeline, "_check_playwright_available", _unexpected_playwright_check)

    with pytest.raises(ValueError, match="version_tag"):
        await pipeline.generate_proxmox_codegen_bundle_async(
            output_dir=tmp_path,
            version_tag=version_tag,
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_pipeline_namespaces_custom_source_and_writes_provenance(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(pipeline, "_check_playwright_available", lambda: False)
    monkeypatch.setattr(
        pipeline,
        "fetch_apidoc_js",
        lambda url, allow_insecure_ssl=False: (
            'const apiSchema = [{"path":"/version","text":"version","leaf":1,'
            '"info":{"GET":{"name":"version","parameters":{"additionalProperties":0},'
            '"returns":{"type":"object"}}}}];'
        ),
    )

    bundle = await pipeline.generate_proxmox_codegen_bundle_async(
        output_dir=tmp_path,
        source_url="https://schemas.example.test/api-viewer/",
        version_tag="review-1",
    )
    artifact_dir = tmp_path / "custom" / "review-1"
    openapi_path = artifact_dir / "openapi.json"
    provenance = json.loads((artifact_dir / "provenance.json").read_text(encoding="utf-8"))

    assert bundle.version_tag == "review-1"
    assert openapi_path.is_file()
    assert not (tmp_path / "review-1").exists()
    assert provenance == {
        "source_url": "https://schemas.example.test/api-viewer/",
        "generated_at": bundle.generated_at,
        "sha256": sha256(openapi_path.read_bytes()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_pydantic_route_renders_from_openapi_without_user_source_read(
    monkeypatch,
):
    def _unexpected_user_directory() -> Path:
        raise AssertionError("The Pydantic read route must not inspect persisted Python source.")

    monkeypatch.setattr(viewer_codegen, "get_user_generated_dir", _unexpected_user_directory)
    monkeypatch.setattr(
        viewer_codegen.asyncio,
        "to_thread",
        lambda function, *args: _completed_awaitable(function(*args)),
    )
    monkeypatch.setattr(
        viewer_codegen,
        "load_proxmox_generated_openapi",
        lambda version_tag: _openapi_with_unusual_property_name(),
    )

    transport = httpx.ASGITransport(app=_runtime_viewer_test_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/proxmox/viewer/pydantic",
            params={"version_tag": "security-reader"},
        )

    assert response.status_code == 200
    assert repr(_UNUSUAL_PROPERTY_NAME) in response.text


async def _completed_awaitable(value: object) -> object:
    return value


def test_resolve_contained_accepts_descendants_and_rejects_parent_escapes(tmp_path: Path):
    base = tmp_path / "generated"
    expected = (base / "8.4" / "openapi.json").resolve()

    assert resolve_contained(base, "8.4", "openapi.json") == expected

    with pytest.raises(ValueError, match="escapes"):
        resolve_contained(base, "safe", "..", "..", "outside.json")

    with pytest.raises(ValueError, match="escapes"):
        resolve_contained(base, "..", "outside.json")


def test_resolve_contained_rejects_existing_symlink_escape(tmp_path: Path):
    base = tmp_path / "generated"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (base / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        resolve_contained(base, "linked", "openapi.json")


def _document_with_response_schema(
    schema: dict[str, object],
    *,
    operation_id: str = "get_bounded_resource",
) -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "test"},
        "paths": {
            "/bounded": {
                "get": {
                    "operationId": operation_id,
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": schema}},
                        }
                    },
                }
            }
        },
    }


def _assert_rejected_before_model_build(
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, object],
    error_type: type[ValueError],
) -> None:
    def _unexpected_create_model(*args, **kwargs):
        raise AssertionError("No Pydantic model may be built for a rejected document.")

    monkeypatch.setattr(pydantic_generator, "create_model", _unexpected_create_model)
    with pytest.raises(error_type):
        build_pydantic_models_from_openapi(document)


def test_deep_array_schema_raises_typed_limit_before_model_build(monkeypatch):
    schema: dict[str, object] = {"type": "string"}
    for _ in range(1200):
        schema = {"type": "array", "items": schema}

    _assert_rejected_before_model_build(
        monkeypatch,
        _document_with_response_schema(schema),
        SchemaLimitError,
    )


def test_excessive_property_count_raises_typed_limit_before_model_build(monkeypatch):
    properties = {
        f"property-{index}": {"type": "string"} for index in range(MAX_PROPERTIES_PER_SCHEMA + 1)
    }
    document = _document_with_response_schema({"type": "object", "properties": properties})

    _assert_rejected_before_model_build(monkeypatch, document, SchemaLimitError)


def test_oversized_document_raises_typed_limit_before_model_build(monkeypatch):
    document = _document_with_response_schema({"type": "string"})
    document["x-padding"] = "x" * MAX_DOCUMENT_BYTES

    _assert_rejected_before_model_build(monkeypatch, document, SchemaLimitError)


@pytest.mark.parametrize("property_name", ["__private", "model_dump", "model_validate"])
def test_reserved_pydantic_field_names_are_rejected_before_model_build(
    monkeypatch,
    property_name: str,
):
    document = _document_with_response_schema(
        {"type": "object", "properties": {property_name: {"type": "string"}}}
    )

    _assert_rejected_before_model_build(monkeypatch, document, SchemaShapeError)


def test_normalized_field_name_collision_is_rejected_before_model_build(monkeypatch):
    document = _document_with_response_schema(
        {
            "type": "object",
            "properties": {"a-b": {"type": "string"}, "a_b": {"type": "integer"}},
        }
    )

    _assert_rejected_before_model_build(monkeypatch, document, SchemaShapeError)


def test_duplicate_operation_model_name_is_rejected_before_model_build(monkeypatch):
    document = _document_with_response_schema({"type": "string"}, operation_id="duplicate")
    document["paths"]["/second"] = document["paths"]["/bounded"]

    _assert_rejected_before_model_build(monkeypatch, document, SchemaShapeError)


def _write_openapi_with_provenance(directory: Path, document: dict[str, object]) -> None:
    directory.mkdir(parents=True)
    openapi_path = directory / "openapi.json"
    openapi_path.write_text(json.dumps(document), encoding="utf-8")
    (directory / "provenance.json").write_text(
        json.dumps(
            {
                "source_url": "https://pve.proxmox.com/pve-docs/api-viewer/",
                "generated_at": "2026-09-14T00:00:00+00:00",
                "sha256": sha256(openapi_path.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )


def test_bundled_schema_wins_over_valid_user_artifact(tmp_path: Path, monkeypatch):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled_document = _document_with_response_schema({"type": "string"})
    user_document = _document_with_response_schema({"type": "integer"})
    (bundled / "8.3").mkdir(parents=True)
    (bundled / "8.3" / "openapi.json").write_text(json.dumps(bundled_document), encoding="utf-8")
    _write_openapi_with_provenance(user / "8.3", user_document)
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(proxmox_schema, "get_user_generated_dir", lambda: user)

    assert (
        proxmox_schema.proxmox_generated_openapi_path("8.3")
        == (bundled / "8.3" / "openapi.json").resolve()
    )
    assert proxmox_schema.load_proxmox_generated_openapi("8.3") == bundled_document


def test_user_schema_requires_matching_provenance_digest(tmp_path: Path, monkeypatch):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    document = _document_with_response_schema({"type": "string"})
    _write_openapi_with_provenance(user / "8.4", document)
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(proxmox_schema, "get_user_generated_dir", lambda: user)
    monkeypatch.setenv("PROXBOX_RUNTIME_CODEGEN_ENABLED", "true")

    assert proxmox_schema.load_proxmox_generated_openapi("8.4") == document

    tampered = _document_with_response_schema({"type": "integer"})
    (user / "8.4" / "openapi.json").write_text(json.dumps(tampered), encoding="utf-8")
    warnings: list[str] = []
    monkeypatch.setattr(
        proxmox_schema.logger,
        "warning",
        lambda message, *args: warnings.append(message % args),
    )
    assert proxmox_schema.load_proxmox_generated_openapi("8.4") == {}
    assert any("missing or invalid provenance" in warning for warning in warnings)

    authoritative = _document_with_response_schema({"type": "boolean"})
    (bundled / "latest").mkdir(parents=True)
    (bundled / "latest" / "openapi.json").write_text(json.dumps(authoritative), encoding="utf-8")
    assert proxmox_schema.available_proxmox_sdk_versions() == ["latest"]
    assert proxmox_schema.load_proxmox_generated_openapi("latest") == authoritative


def test_quarantine_legacy_artifacts_renames_python_and_unprovenanced_cache(
    tmp_path: Path,
    monkeypatch,
):
    user = tmp_path / "user"
    version_dir = user / "8.4"
    version_dir.mkdir(parents=True)
    models_path = version_dir / "pydantic_models.py"
    cache_path = user / "runtime_generated_routes_cache.json"
    models_path.write_text("legacy source", encoding="utf-8")
    cache_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(proxmox_schema, "get_user_generated_dir", lambda: user)

    quarantined = proxmox_schema.quarantine_legacy_codegen_artifacts()

    assert not models_path.exists()
    assert not cache_path.exists()
    assert len(quarantined) == 2
    assert all(".quarantined-" in path.name for path in quarantined)
    assert {path.read_text(encoding="utf-8") for path in quarantined} == {
        "legacy source",
        "{}",
    }


def test_default_schema_discovery_never_reads_user_directory(monkeypatch, tmp_path: Path):
    bundled = tmp_path / "bundled"
    (bundled / "latest").mkdir(parents=True)
    document = _document_with_response_schema({"type": "string"})
    (bundled / "latest" / "openapi.json").write_text(json.dumps(document), encoding="utf-8")

    def _unexpected_user_directory() -> Path:
        raise AssertionError("Default schema discovery must not inspect the user directory.")

    monkeypatch.delenv("PROXBOX_RUNTIME_CODEGEN_ENABLED", raising=False)
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(proxmox_schema, "get_user_generated_dir", _unexpected_user_directory)

    assert proxmox_schema.available_proxmox_sdk_versions() == ["latest"]
    assert proxmox_schema.load_proxmox_generated_openapi("latest") == document
    assert proxmox_schema.load_proxmox_generated_openapi("unknown") == {}


def test_bundled_tag_skips_same_named_user_artifact_before_read(monkeypatch, tmp_path: Path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    (bundled / "8.3").mkdir(parents=True)
    (user / "8.3").mkdir(parents=True)
    document = _document_with_response_schema({"type": "string"})
    (bundled / "8.3" / "openapi.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(proxmox_schema, "get_user_generated_dir", lambda: user)
    monkeypatch.setattr(
        proxmox_schema,
        "_user_openapi_artifact",
        lambda version_tag: (_ for _ in ()).throw(
            AssertionError(f"User artifact was read for bundled tag {version_tag}.")
        ),
    )

    assert proxmox_schema.available_proxmox_sdk_versions(include_user=True) == ["8.3"]


def test_oversized_openapi_is_rejected_from_stat_before_read(monkeypatch, tmp_path: Path):
    bundled = tmp_path / "bundled"
    path = bundled / "oversized" / "openapi.json"
    path.parent.mkdir(parents=True)
    with path.open("wb") as stream:
        stream.truncate(MAX_DOCUMENT_BYTES + 1)
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(
        proxmox_schema.os,
        "read",
        lambda *args: (_ for _ in ()).throw(AssertionError("Oversized file was read.")),
    )

    assert proxmox_schema.load_proxmox_generated_openapi("oversized") == {}


@pytest.mark.parametrize("parser_error", [RecursionError(), ValueError("parser sentinel")])
def test_openapi_parser_errors_become_typed_rejections(
    monkeypatch,
    tmp_path: Path,
    parser_error: Exception,
):
    bundled = tmp_path / "bundled"
    path = bundled / "parser" / "openapi.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"paths": {}}', encoding="utf-8")
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(
        proxmox_schema.json,
        "loads",
        lambda raw: (_ for _ in ()).throw(parser_error),
    )

    assert proxmox_schema.load_proxmox_generated_openapi("parser") == {}


def _malformed_container_document(container: str) -> dict[str, object]:
    document = _document_with_response_schema({"type": "array", "items": {"type": "string"}})
    operation = document["paths"]["/bounded"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    if container == "requestBody":
        operation["requestBody"] = []
    elif container == "responses":
        operation["responses"] = []
    elif container == "parameters":
        operation["parameters"] = {}
    elif container == "required":
        schema["required"] = "field"
    elif container == "properties":
        schema["properties"] = []
    elif container == "items":
        schema["items"] = []
    elif container == "enum":
        schema["enum"] = {}
    elif container == "type":
        schema["type"] = {}
    return document


@pytest.mark.parametrize(
    "container",
    ["requestBody", "responses", "parameters", "required", "properties", "items", "enum", "type"],
)
@pytest.mark.parametrize("artifact_source", ["bundled", "user"])
def test_malformed_consumed_shapes_are_rejected_before_model_construction(
    monkeypatch,
    tmp_path: Path,
    container: str,
    artifact_source: str,
):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    document = _malformed_container_document(container)
    if artifact_source == "bundled":
        path = bundled / "shape" / "openapi.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    else:
        _write_openapi_with_provenance(user / "shape", document)
        monkeypatch.setenv("PROXBOX_RUNTIME_CODEGEN_ENABLED", "true")
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(proxmox_schema, "get_user_generated_dir", lambda: user)
    loaded = proxmox_schema.load_proxmox_generated_openapi("shape")

    assert loaded == document
    with pytest.raises(SchemaShapeError):
        runtime_generated.register_generated_proxmox_routes(
            FastAPI(),
            version_tag="shape",
            openapi_document=loaded,
        )


@pytest.mark.asyncio
async def test_lifespan_catches_only_typed_schema_validation_failure(monkeypatch):
    document = _malformed_container_document("responses")
    application = FastAPI()

    async def _skip_bootstrap(app: FastAPI) -> None:
        return None

    async def _skip_dispose() -> None:
        return None

    real_register = runtime_generated.register_generated_proxmox_routes
    monkeypatch.setattr(factory.bootstrap, "init_database_and_netbox", lambda _owner: None)
    monkeypatch.setattr(factory, "validate_auth_lockout_identity_key", lambda: None)
    monkeypatch.setattr(factory, "quarantine_legacy_codegen_artifacts", lambda: [])
    monkeypatch.setattr(factory, "_run_bootstrap_pass", _skip_bootstrap)
    monkeypatch.setattr(factory.database, "dispose_database", _skip_dispose)
    monkeypatch.setattr(
        factory,
        "register_generated_proxmox_routes",
        lambda app: real_register(app, openapi_documents={"shape": document}),
    )

    async with factory._lifespan(application):
        assert not any(
            str(getattr(route, "path", "")).startswith("/proxmox/api2/")
            for route in application.routes
        )


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [
        ("MAX_ELIGIBLE_VERSIONS", 1),
        ("MAX_AGGREGATE_DOCUMENT_BYTES", 1),
        ("MAX_AGGREGATE_MODELS", 1),
        ("MAX_AGGREGATE_ROUTES", 1),
    ],
)
def test_aggregate_limits_precede_model_and_cache_construction(
    monkeypatch,
    limit_name: str,
    limit_value: int,
):
    documents = {
        "one": _document_with_response_schema({"type": "string"}),
        "two": _document_with_response_schema({"type": "integer"}),
    }
    monkeypatch.setattr(runtime_generated, limit_name, limit_value)
    monkeypatch.setattr(
        runtime_generated,
        "_load_model_module",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Model construction ran.")),
    )
    monkeypatch.setattr(
        runtime_generated,
        "_write_generated_route_cache",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Cache construction ran.")),
    )

    with pytest.raises(SchemaLimitError):
        runtime_generated.register_generated_proxmox_routes(
            FastAPI(),
            openapi_documents=documents,
        )


def test_more_than_eight_individually_valid_documents_exceeds_aggregate_limit(
    monkeypatch,
):
    documents = {
        f"version-{index}": _document_with_response_schema({"type": "string"}) for index in range(9)
    }
    monkeypatch.setattr(
        runtime_generated,
        "_load_model_module",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Model construction ran.")),
    )
    monkeypatch.setattr(
        runtime_generated,
        "_write_generated_route_cache",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Cache construction ran.")),
    )

    with pytest.raises(SchemaLimitError, match="MAX_ELIGIBLE_VERSIONS"):
        runtime_generated.register_generated_proxmox_routes(
            FastAPI(),
            openapi_documents=documents,
        )


def test_process_caches_validation_models_and_routes_by_version_digest(
    monkeypatch,
    tmp_path: Path,
):
    bundled = tmp_path / "bundled"
    documents = {
        "latest": _document_with_response_schema(
            {"type": "string"}, operation_id="get_latest_cached"
        ),
        "8.4": _document_with_response_schema({"type": "integer"}, operation_id="get_84_cached"),
    }
    documents["latest"]["info"]["version"] = "latest-first"
    documents["8.4"]["info"]["version"] = "8.4-first"
    for version_tag, document in documents.items():
        path = bundled / version_tag / "openapi.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    validation_calls: list[str] = []
    builder_calls: list[str] = []
    real_validate = security._validate_openapi_document_uncached
    real_builder = runtime_generated.build_pydantic_models_from_openapi

    def _count_validation(document):
        validation_calls.append(document["info"]["version"])
        return real_validate(document)

    def _count_builder(document, **kwargs):
        builder_calls.append(document["info"]["version"])
        return real_builder(document, **kwargs)

    monkeypatch.setattr(security, "_validate_openapi_document_uncached", _count_validation)
    monkeypatch.setattr(
        runtime_generated,
        "build_pydantic_models_from_openapi",
        _count_builder,
    )
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(
        runtime_generated, "available_proxmox_sdk_versions", lambda: list(documents)
    )
    monkeypatch.setattr(
        runtime_generated,
        "proxmox_generated_route_cache_path",
        lambda: tmp_path / "routes.json",
    )
    proxmox_schema._BUNDLED_OPENAPI_CACHE.clear()
    runtime_generated._MODEL_MODULE_CACHE.clear()
    runtime_generated._REGISTRATION_PLAN_CACHE.clear()

    first_app = FastAPI()
    runtime_generated.register_generated_proxmox_routes(first_app)
    first_routes = {
        route.name: route
        for route in first_app.routes
        if str(getattr(route, "name", "")).startswith("generated_proxmox_route__")
    }
    second_app = FastAPI()
    runtime_generated.register_generated_proxmox_routes(second_app)
    second_routes = {
        route.name: route
        for route in second_app.routes
        if str(getattr(route, "name", "")).startswith("generated_proxmox_route__")
    }

    assert validation_calls == ["latest-first", "8.4-first"]
    assert builder_calls == ["latest-first", "8.4-first"]
    assert second_routes.keys() == first_routes.keys()
    assert all(second_routes[name] is not route for name, route in first_routes.items())
    assert all(
        second_routes[name].dependant is route.dependant for name, route in first_routes.items()
    )
    assert all(
        route.dependency_overrides_provider is second_app for route in second_routes.values()
    )

    changed = deepcopy(documents["latest"])
    changed["info"]["version"] = "latest-changed"
    documents["latest"] = changed
    (bundled / "latest" / "openapi.json").write_text(json.dumps(changed), encoding="utf-8")
    third_app = FastAPI()
    runtime_generated.register_generated_proxmox_routes(third_app)

    assert validation_calls == ["latest-first", "8.4-first", "latest-changed"]
    assert builder_calls == ["latest-first", "8.4-first", "latest-changed"]


def test_bundled_document_loader_caches_by_path_and_digest(monkeypatch, tmp_path: Path):
    bundled = tmp_path / "bundled"
    path = bundled / "latest" / "openapi.json"
    path.parent.mkdir(parents=True)
    first = _document_with_response_schema({"type": "string"})
    path.write_text(json.dumps(first), encoding="utf-8")
    calls = 0
    real_validate = proxmox_schema.validate_openapi_document_limits

    def _count_validation(document):
        nonlocal calls
        calls += 1
        return real_validate(document)

    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(
        proxmox_schema,
        "validate_openapi_document_limits",
        _count_validation,
    )
    proxmox_schema._BUNDLED_OPENAPI_CACHE.clear()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: proxmox_schema.load_proxmox_generated_openapi("latest"),
                range(4),
            )
        )

    assert results == [first] * 4
    assert proxmox_schema.load_proxmox_generated_openapi("latest") == first
    assert calls == 1

    changed = _document_with_response_schema({"type": "integer"})
    path.write_text(json.dumps(changed), encoding="utf-8")

    assert proxmox_schema.load_proxmox_generated_openapi("latest") == changed
    assert calls == 2


@pytest.mark.parametrize("consumer", ["registration", "model-construction", "cache-write"])
def test_mutated_bundled_document_invalidates_cached_validation_receipt(
    monkeypatch,
    tmp_path: Path,
    consumer: str,
):
    bundled = tmp_path / "bundled"
    path = bundled / "latest" / "openapi.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_document_with_response_schema({"type": "string"})),
        encoding="utf-8",
    )
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(
        runtime_generated,
        "proxmox_generated_route_cache_path",
        lambda: tmp_path / "routes.json",
    )
    proxmox_schema._BUNDLED_OPENAPI_CACHE.clear()
    runtime_generated._MODEL_MODULE_CACHE.clear()
    runtime_generated._REGISTRATION_PLAN_CACHE.clear()
    document = proxmox_schema.load_proxmox_generated_openapi("latest")
    validation = proxmox_schema.bundled_openapi_document_validation(document)
    assert validation is not None
    document["paths"]["/mutated-after-validation"] = document["paths"].pop("/bounded")

    with pytest.raises(SchemaShapeError, match="current document content"):
        if consumer == "registration":
            runtime_generated.register_generated_proxmox_routes(
                FastAPI(),
                version_tag="latest",
                openapi_document=document,
                force_rebuild=True,
            )
        elif consumer == "model-construction":
            build_pydantic_models_from_openapi(document, validation=validation)
        else:
            runtime_generated._write_generated_route_cache(
                documents={"latest": document},
                alias_version_tag="latest",
                validations={"latest": validation},
            )


def test_concurrent_source_mutation_cannot_poison_snapshot_backed_caches(
    monkeypatch,
    tmp_path: Path,
):
    bundled = tmp_path / "bundled"
    path = bundled / "latest" / "openapi.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_document_with_response_schema({"type": "string"})),
        encoding="utf-8",
    )
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(
        runtime_generated,
        "proxmox_generated_route_cache_path",
        lambda: tmp_path / "routes.json",
    )
    proxmox_schema._BUNDLED_OPENAPI_CACHE.clear()
    runtime_generated._MODEL_MODULE_CACHE.clear()
    runtime_generated._REGISTRATION_PLAN_CACHE.clear()
    document = proxmox_schema.load_proxmox_generated_openapi("latest")
    validation = proxmox_schema.bundled_openapi_document_validation(document)
    assert validation is not None

    snapshot_ready = threading.Event()
    mutation_done = threading.Event()
    real_snapshot = pydantic_generator.snapshot_openapi_document

    def _snapshot_then_pause(document_to_snapshot, receipt=None):
        snapshot = real_snapshot(document_to_snapshot, receipt)
        snapshot_ready.set()
        assert mutation_done.wait(timeout=5)
        return snapshot

    def _mutate_source() -> None:
        if not snapshot_ready.wait(timeout=5):
            return
        operation = document["paths"].pop("/bounded")
        operation["get"]["operationId"] = "get_mutated_during_build"
        operation["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
            "type": "integer"
        }
        document["paths"]["/mutated-during-build"] = operation
        mutation_done.set()

    monkeypatch.setattr(
        pydantic_generator,
        "snapshot_openapi_document",
        _snapshot_then_pause,
    )
    mutation_thread = threading.Thread(target=_mutate_source)
    mutation_thread.start()
    application = FastAPI()
    try:
        runtime_generated.register_generated_proxmox_routes(
            application,
            version_tag="latest",
            openapi_document=document,
        )
    finally:
        mutation_thread.join(timeout=5)

    assert not mutation_thread.is_alive()
    cached_module = runtime_generated._MODEL_MODULE_CACHE[("latest", validation.digest)]
    assert cached_module.GetBoundedResourceResponse.model_fields["root"].annotation is str
    assert not hasattr(cached_module, "GetMutatedDuringBuildResponse")
    generated_paths = {
        route.path
        for route in application.routes
        if str(getattr(route, "name", "")).startswith("generated_proxmox_route__")
    }
    assert generated_paths == {"/proxmox/api2/latest/bounded", "/proxmox/api2/bounded"}
    cache_payload = json.loads((tmp_path / "routes.json").read_text(encoding="utf-8"))
    assert set(cache_payload["documents"]["latest"]["paths"]) == {"/bounded"}
    assert ("latest", sha256(security.openapi_document_bytes(document)).hexdigest()) not in (
        runtime_generated._MODEL_MODULE_CACHE
    )


def test_bundled_and_model_caches_evict_oldest_entries(monkeypatch, tmp_path: Path):
    bundled = tmp_path / "bundled"
    documents = [
        _document_with_response_schema(
            {"type": schema_type}, operation_id=f"get_cache_{schema_type}"
        )
        for schema_type in ("string", "integer", "boolean")
    ]
    builder_calls = 0
    real_builder = runtime_generated.build_pydantic_models_from_openapi

    def _count_builder(document, **kwargs):
        nonlocal builder_calls
        builder_calls += 1
        return real_builder(document, **kwargs)

    monkeypatch.setattr(proxmox_schema, "_BUNDLED_OPENAPI_CACHE_MAX_SIZE", 2)
    monkeypatch.setattr(proxmox_schema, "get_bundled_generated_dir", lambda: bundled)
    monkeypatch.setattr(runtime_generated, "_MODEL_MODULE_CACHE_MAX_SIZE", 2)
    monkeypatch.setattr(
        runtime_generated,
        "build_pydantic_models_from_openapi",
        _count_builder,
    )
    proxmox_schema._BUNDLED_OPENAPI_CACHE.clear()
    runtime_generated._MODEL_MODULE_CACHE.clear()

    for index, document in enumerate(documents):
        version_tag = f"bounded-{index}"
        path = bundled / version_tag / "openapi.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        loaded = proxmox_schema.load_proxmox_generated_openapi(version_tag)
        runtime_generated._load_model_module(loaded, version_tag=version_tag)

    assert len(proxmox_schema._BUNDLED_OPENAPI_CACHE) == 2
    assert len(runtime_generated._MODEL_MODULE_CACHE) == 2

    first = proxmox_schema.load_proxmox_generated_openapi("bounded-0")
    runtime_generated._load_model_module(first, version_tag="bounded-0")

    assert builder_calls == 4


def _quarantine_process(directory: str, start, results) -> None:
    os.environ["PROXBOX_GENERATED_DIR"] = directory
    start.wait(timeout=10)
    try:
        paths = proxmox_schema.quarantine_legacy_codegen_artifacts()
        results.put(("ok", [str(path) for path in paths]))
    except Exception as error:
        results.put(("error", repr(error)))


def test_quarantine_is_idempotent_across_racing_processes(tmp_path: Path):
    user = tmp_path / "user"
    version_dir = user / "8.4"
    version_dir.mkdir(parents=True)
    (version_dir / "pydantic_models.py").write_text("legacy", encoding="utf-8")
    (user / "runtime_generated_routes_cache.json").write_text("{}", encoding="utf-8")
    # ``spawn``, never ``fork``: a pytest-xdist worker is multi-threaded and
    # carries the whole imported application plus coverage state, so forking
    # it twice on the memory-bounded CI runner duplicated hundreds of megabytes
    # per test and twice took the entire job down at the tail of the run with
    # no summary written. A spawned child imports only this module.
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_quarantine_process, args=(str(user), start, results))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=60) for _ in processes]
        for process in processes:
            process.join(timeout=60)
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    assert [status for status, _ in outcomes] == ["ok", "ok"]
    assert not (version_dir / "pydantic_models.py").exists()
    assert not (user / "runtime_generated_routes_cache.json").exists()
    assert len(list(user.rglob("*.quarantined-*"))) == 2


def test_runtime_generated_module_import_ignores_escaping_cache_symlinks(tmp_path: Path):
    user = tmp_path / "user"
    user.mkdir()
    outside_cache = tmp_path / "outside-cache.json"
    outside_provenance = tmp_path / "outside-provenance.json"
    outside_cache.write_text("outside cache", encoding="utf-8")
    outside_provenance.write_text("outside provenance", encoding="utf-8")
    (user / "runtime_generated_routes_cache.json").symlink_to(outside_cache)
    (user / "runtime_generated_routes_cache.provenance.json").symlink_to(outside_provenance)
    environment = os.environ.copy()
    environment["PROXBOX_GENERATED_DIR"] = str(user)

    result = subprocess.run(
        [sys.executable, "-c", "import proxbox_api.routes.proxmox.runtime_generated"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert outside_cache.read_text(encoding="utf-8") == "outside cache"
    assert outside_provenance.read_text(encoding="utf-8") == "outside provenance"


@pytest.mark.asyncio
async def test_startup_quarantines_cache_and_provenance_symlinks_without_following(
    monkeypatch,
    tmp_path: Path,
):
    user = tmp_path / "user"
    user.mkdir()
    outside_cache = tmp_path / "outside-cache.json"
    outside_provenance = tmp_path / "outside-provenance.json"
    outside_cache.write_text("outside cache", encoding="utf-8")
    outside_provenance.write_text("outside provenance", encoding="utf-8")
    (user / "runtime_generated_routes_cache.json").symlink_to(outside_cache)
    (user / "runtime_generated_routes_cache.provenance.json").symlink_to(outside_provenance)
    application = FastAPI()

    async def _skip_bootstrap(app: FastAPI) -> None:
        return None

    async def _skip_dispose() -> None:
        return None

    real_register = runtime_generated.register_generated_proxmox_routes
    monkeypatch.setenv("PROXBOX_GENERATED_DIR", str(user))
    monkeypatch.setattr(factory.bootstrap, "init_database_and_netbox", lambda _owner: None)
    monkeypatch.setattr(factory, "validate_auth_lockout_identity_key", lambda: None)
    monkeypatch.setattr(factory, "_run_bootstrap_pass", _skip_bootstrap)
    monkeypatch.setattr(factory.database, "dispose_database", _skip_dispose)
    monkeypatch.setattr(
        factory,
        "register_generated_proxmox_routes",
        lambda app: real_register(
            app,
            openapi_documents={"latest": _document_with_response_schema({"type": "string"})},
        ),
    )

    async with factory._lifespan(application):
        assert (user / "runtime_generated_routes_cache.json").is_file()
        assert (user / "runtime_generated_routes_cache.provenance.json").is_file()

    assert outside_cache.read_text(encoding="utf-8") == "outside cache"
    assert outside_provenance.read_text(encoding="utf-8") == "outside provenance"


@pytest.mark.asyncio
async def test_rendered_pydantic_source_is_threaded_and_cached_by_digest(monkeypatch):
    document = _document_with_response_schema({"type": "string"})
    caller_thread = threading.get_ident()
    calls: list[int] = []

    def _render(schema: dict[str, object]) -> str:
        calls.append(threading.get_ident())
        return "rendered"

    monkeypatch.setattr(viewer_codegen, "generate_pydantic_models_from_openapi", _render)

    async def _controlled_to_thread(function, *args):
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(function, *args).result()

    monkeypatch.setattr(viewer_codegen.asyncio, "to_thread", _controlled_to_thread)
    viewer_codegen._RENDERED_PYDANTIC_CACHE.clear()

    assert await viewer_codegen._render_pydantic_models(document) == "rendered"
    assert await viewer_codegen._render_pydantic_models(document) == "rendered"
    assert len(calls) == 1
    assert calls[0] != caller_thread


def test_rendered_pydantic_source_has_fixed_byte_ceiling(monkeypatch):
    document = _document_with_response_schema({"type": "string"})
    monkeypatch.setattr(viewer_codegen, "MAX_RENDERED_PYDANTIC_BYTES", 1)
    monkeypatch.setattr(
        viewer_codegen,
        "generate_pydantic_models_from_openapi",
        lambda schema: "too large",
    )
    viewer_codegen._RENDERED_PYDANTIC_CACHE.clear()

    with pytest.raises(SchemaLimitError):
        viewer_codegen._render_pydantic_models_bounded(document)
