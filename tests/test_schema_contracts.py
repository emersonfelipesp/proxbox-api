"""Schema contract tests for generated API artifacts."""

from __future__ import annotations

import importlib.util
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from proxbox_api.main import app
from proxbox_api.proxmox_to_netbox import netbox_schema
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    load_proxmox_generated_openapi,
    proxmox_generated_openapi_path,
    proxmox_operation_schema,
)
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema
from tests.fixtures import NETBOX_OPENAPI_SNAPSHOT

OpenAPIDocument = dict[str, object]
PUBLIC_PLUGIN_NAMESPACES = frozenset({"bng", "gpon", "proxbox"})
REPRESENTATIVE_PUBLIC_PATHS = (
    "/api/virtualization/virtual-machines/",
    "/api/dcim/devices/",
    "/api/ipam/prefixes/",
)
# NetBox 4.6 exposes VM operations through the *WithConfigContext schemas;
# NetBox 4.7 renamed them to the plain VirtualMachine names. Either complete
# set satisfies the contract, so a refresh against newer upstream stays valid.
VIRTUAL_MACHINE_SCHEMA_REF_SETS = (
    frozenset(
        {
            "#/components/schemas/PaginatedVirtualMachineWithConfigContextList",
            "#/components/schemas/VirtualMachineWithConfigContext",
            "#/components/schemas/WritableVirtualMachineWithConfigContextRequest",
        }
    ),
    frozenset(
        {
            "#/components/schemas/PaginatedVirtualMachineList",
            "#/components/schemas/VirtualMachine",
            "#/components/schemas/WritableVirtualMachineRequest",
        }
    ),
)


def _is_local_json_pointer(ref: str) -> bool:
    return ref.startswith("#/")


def _decode_json_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_local_json_pointer(document: OpenAPIDocument, pointer: str) -> object | None:
    if not _is_local_json_pointer(pointer):
        return None
    current: object = document
    for raw_token in pointer[2:].split("/"):
        if not raw_token:
            return None
        token = _decode_json_pointer_token(raw_token)
        if not isinstance(current, dict) or token not in current:
            return None
        current = current[token]
    return current


def _iter_local_json_pointers(node: object) -> Iterable[str]:
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and _is_local_json_pointer(ref):
                yield ref
            stack.extend(current.values())
            continue
        if isinstance(current, list):
            stack.extend(current)


def _collect_dangling_local_references(document: OpenAPIDocument) -> list[str]:
    seen: set[str] = set()
    dangling: list[str] = []
    for ref in _iter_local_json_pointers(document):
        if ref in seen:
            continue
        seen.add(ref)
        if _resolve_local_json_pointer(document, ref) is None:
            dangling.append(ref)
    return dangling


def _plugin_namespace(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) > 3 and parts[1:3] == ["api", "plugins"]:
        return parts[3]
    return None


def _assert_representative_public_paths(paths: dict[str, object]) -> None:
    for path in REPRESENTATIVE_PUBLIC_PATHS:
        assert path in paths


def _assert_no_private_control_plane_surface(document: OpenAPIDocument) -> None:
    paths = document.get("paths")
    assert isinstance(paths, dict)
    plugin_namespaces = {
        namespace for path in paths if (namespace := _plugin_namespace(path)) is not None
    }
    assert plugin_namespaces <= PUBLIC_PLUGIN_NAMESPACES

    components = document.get("components")
    assert isinstance(components, dict)
    schemas = components.get("schemas")
    assert isinstance(schemas, dict)
    assert not any("Backend" in name for name in schemas)


def _assert_virtual_machine_operation_schemas(document: OpenAPIDocument) -> None:
    paths = document.get("paths")
    assert isinstance(paths, dict)
    vm_path = paths["/api/virtualization/virtual-machines/"]
    assert isinstance(vm_path, dict)

    refs = set(_iter_local_json_pointers(vm_path))
    candidates = [ref_set for ref_set in VIRTUAL_MACHINE_SCHEMA_REF_SETS if ref_set <= refs]
    assert candidates, (
        "VM path references no complete schema set; missing per set: "
        f"{[sorted(ref_set - refs) for ref_set in VIRTUAL_MACHINE_SCHEMA_REF_SETS]}"
    )
    assert any(
        all(_resolve_local_json_pointer(document, ref) is not None for ref in ref_set)
        for ref_set in candidates
    ), "VM schema refs do not resolve in components"


def _assert_all_local_json_pointers_resolve(document: OpenAPIDocument) -> None:
    dangling = _collect_dangling_local_references(document)
    assert dangling == []


def test_bundled_netbox_openapi_cache_is_sanitized_public_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for issue #439: bundled NetBox OpenAPI stays a public contract."""

    monkeypatch.delenv("PROXBOX_NETBOX_OPENAPI_PERSIST", raising=False)
    monkeypatch.setattr(netbox_schema, "_in_memory_openapi_cache", None)

    bundled_path = netbox_schema.netbox_openapi_cache_path()
    assert bundled_path.is_file()

    document = netbox_schema.load_netbox_openapi_cache()
    assert isinstance(document, dict)
    assert document.get("openapi") == "3.0.3"

    paths = document.get("paths")
    assert isinstance(paths, dict)

    _assert_representative_public_paths(paths)
    _assert_no_private_control_plane_surface(document)
    _assert_virtual_machine_operation_schemas(document)
    _assert_all_local_json_pointers_resolve(document)


def test_custom_openapi_contains_embedded_generated_proxmox_schema():
    schema = app.openapi()
    assert schema["info"]["x-proxmox-generated-openapi"]["source"].endswith(
        "proxbox_api/generated/proxmox/latest/openapi.json"
    )
    assert "x-proxmox-generated-openapi" in schema["info"]


def test_generated_proxmox_sdk_snapshot_is_available():
    document = load_proxmox_generated_openapi()
    assert proxmox_generated_openapi_path().exists()
    assert document["openapi"] == "3.1.0"
    assert "/cluster/resources" in document["paths"]
    assert (
        proxmox_operation_schema(
            "/cluster/resources",
            "GET",
            openapi=document,
        )
        is not None
    )


def test_generated_proxmox_pydantic_models_are_importable():
    base = Path(__file__).resolve().parents[1] / "proxbox_api" / "generated" / "proxmox"
    path = base / "latest" / "pydantic_models.py"
    if not path.exists():
        path = base / "pydantic_models.py"
    spec = importlib.util.spec_from_file_location("generated_proxmox_models", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    assert hasattr(module, "GetAccessResponse")
    assert hasattr(module, "GetAccessAclResponse")


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout", 0),
        ("timeout", 3601),
        ("max_retries", -1),
        ("max_retries", 101),
        ("retry_backoff", -0.1),
        ("retry_backoff", 300.1),
    ],
)
def test_proxmox_session_schema_rejects_out_of_bounds_values(field, value):
    with pytest.raises(ValidationError):
        ProxmoxSessionSchema(**{field: value})


def test_proxmox_session_schema_accepts_valid_bounds():
    schema = ProxmoxSessionSchema(timeout=30, max_retries=5, retry_backoff=1.5)
    assert schema.timeout == 30
    assert schema.max_retries == 5
    assert schema.retry_backoff == 1.5


def test_netbox_schema_resolution_prefers_live_then_cache_then_fallback(
    monkeypatch,
    tmp_path,
):
    cache_path = tmp_path / "openapi.json"
    monkeypatch.setattr(netbox_schema, "netbox_openapi_cache_path", lambda: cache_path)

    monkeypatch.setattr(
        netbox_schema,
        "fetch_live_netbox_openapi",
        lambda timeout=20: NETBOX_OPENAPI_SNAPSHOT,
    )
    live_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert live_resolved["source"] == "live"
    assert cache_path.exists()

    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", lambda timeout=20: None)
    netbox_schema.reset_netbox_schema_contract_cache()
    cached_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert cached_resolved["source"] == "cache"
    assert cached_resolved["openapi"]["paths"]

    cache_path.unlink()
    netbox_schema.reset_netbox_schema_contract_cache()
    fallback_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert fallback_resolved["source"] == "fallback"
    assert fallback_resolved["contract"]["required_fields"] == [
        "name",
        "status",
        "cluster",
    ]


def test_netbox_schema_resolution_in_memory_when_persistence_disabled(
    monkeypatch,
    tmp_path,
):
    """With PROXBOX_NETBOX_OPENAPI_PERSIST disabled, resolution never touches disk."""

    cache_path = tmp_path / "openapi.json"
    monkeypatch.setattr(netbox_schema, "netbox_openapi_cache_path", lambda: cache_path)
    monkeypatch.setattr(netbox_schema, "_in_memory_openapi_cache", None)
    monkeypatch.setenv("PROXBOX_NETBOX_OPENAPI_PERSIST", "false")

    assert netbox_schema.netbox_openapi_persistence_enabled() is False

    # Live fetch: the document must be retained in-memory, not written to disk.
    monkeypatch.setattr(
        netbox_schema,
        "fetch_live_netbox_openapi",
        lambda timeout=20: NETBOX_OPENAPI_SNAPSHOT,
    )
    live_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert live_resolved["source"] == "live"
    assert not cache_path.exists()

    # Second resolution with no live endpoint reuses the in-memory document.
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", lambda timeout=20: None)
    netbox_schema.reset_netbox_schema_contract_cache()
    cached_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert cached_resolved["source"] == "cache"
    assert cached_resolved["openapi"]["paths"]
    assert not cache_path.exists()

    # Clearing the in-memory store falls back to the docs-derived contract.
    monkeypatch.setattr(netbox_schema, "_in_memory_openapi_cache", None)
    netbox_schema.reset_netbox_schema_contract_cache()
    fallback_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert fallback_resolved["source"] == "fallback"
    assert not cache_path.exists()


class _SchemaFetchCounter:
    """Counting stand-in for the live NetBox OpenAPI download."""

    def __init__(self, document: dict[str, object] | None, delay: float = 0.0) -> None:
        self.document = document
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, timeout: int = 20) -> dict[str, object] | None:
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.document


@pytest.fixture
def schema_contract_env(monkeypatch, tmp_path):
    """Isolate contract resolution from disk, the database, and the real clock."""

    clock = {"now": 1000.0}
    identity = {"value": (1, "https://netbox.example")}
    monkeypatch.setattr(
        netbox_schema, "netbox_openapi_cache_path", lambda: tmp_path / "openapi.json"
    )
    monkeypatch.setattr(netbox_schema, "_in_memory_openapi_cache", None)
    monkeypatch.setenv("PROXBOX_NETBOX_OPENAPI_PERSIST", "false")
    monkeypatch.delenv("PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL", raising=False)
    monkeypatch.setattr(netbox_schema, "_monotonic", lambda: clock["now"])
    monkeypatch.setattr(netbox_schema, "_netbox_endpoint_identity", lambda: identity["value"])
    return {"clock": clock, "identity": identity}


def test_netbox_schema_contract_is_fetched_once_for_many_resolutions(
    monkeypatch, schema_contract_env
):
    """A sync resolves the contract per VM; that must not mean one download per VM."""

    fetch = _SchemaFetchCounter(NETBOX_OPENAPI_SNAPSHOT)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)

    sources = [netbox_schema.resolve_netbox_schema_contract()["source"] for _ in range(46)]

    assert sources == ["live"] * 46
    assert fetch.calls == 1


def test_netbox_schema_contract_single_flight_across_threads(monkeypatch, schema_contract_env):
    """Parallel sync workers wait for one download instead of starting their own."""

    fetch = _SchemaFetchCounter(NETBOX_OPENAPI_SNAPSHOT, delay=0.05)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)
    barrier = threading.Barrier(8)

    def resolve() -> object:
        barrier.wait(timeout=5)
        return netbox_schema.resolve_netbox_schema_contract()["source"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        sources = [future.result(timeout=10) for future in [pool.submit(resolve) for _ in range(8)]]

    assert sources == ["live"] * 8
    assert fetch.calls == 1


def test_netbox_schema_contract_is_resolved_again_after_ttl(monkeypatch, schema_contract_env):
    fetch = _SchemaFetchCounter(NETBOX_OPENAPI_SNAPSHOT)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)
    monkeypatch.setenv("PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL", "120")
    clock = schema_contract_env["clock"]

    netbox_schema.resolve_netbox_schema_contract()
    clock["now"] += 119.0
    netbox_schema.resolve_netbox_schema_contract()
    assert fetch.calls == 1

    clock["now"] += 1.0
    netbox_schema.resolve_netbox_schema_contract()
    assert fetch.calls == 2


def test_netbox_schema_fallback_contract_is_reused_only_briefly(monkeypatch, schema_contract_env):
    """An unreachable NetBox is not probed per VM, but recovery is picked up fast."""

    fetch = _SchemaFetchCounter(None)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)
    clock = schema_contract_env["clock"]

    assert netbox_schema.resolve_netbox_schema_contract()["source"] == "fallback"
    clock["now"] += netbox_schema.RETRY_CONTRACT_TTL_SECONDS - 1.0
    assert netbox_schema.resolve_netbox_schema_contract()["source"] == "fallback"
    assert fetch.calls == 1

    fetch.document = NETBOX_OPENAPI_SNAPSHOT
    clock["now"] += 1.0
    assert netbox_schema.resolve_netbox_schema_contract()["source"] == "live"
    assert fetch.calls == 2


def test_netbox_schema_fallback_ttl_never_exceeds_configured_ttl(monkeypatch, schema_contract_env):
    fetch = _SchemaFetchCounter(None)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)
    monkeypatch.setenv("PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL", "10")
    clock = schema_contract_env["clock"]

    netbox_schema.resolve_netbox_schema_contract()
    clock["now"] += 10.0
    netbox_schema.resolve_netbox_schema_contract()

    assert fetch.calls == 2


def test_netbox_schema_contract_ttl_zero_disables_reuse(monkeypatch, schema_contract_env):
    fetch = _SchemaFetchCounter(NETBOX_OPENAPI_SNAPSHOT)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)
    monkeypatch.setenv("PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL", "0")

    for _ in range(3):
        netbox_schema.resolve_netbox_schema_contract()

    assert fetch.calls == 3


def test_netbox_schema_contract_is_not_reused_for_another_endpoint(
    monkeypatch, schema_contract_env
):
    fetch = _SchemaFetchCounter(NETBOX_OPENAPI_SNAPSHOT)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)

    netbox_schema.resolve_netbox_schema_contract()
    schema_contract_env["identity"]["value"] = (1, "https://other-netbox.example")
    netbox_schema.resolve_netbox_schema_contract()

    assert fetch.calls == 2


def test_netbox_schema_cached_contract_is_retried_soon_after_a_failed_live_fetch(
    monkeypatch, schema_contract_env
):
    """A cache result means the live fetch failed, so it must not be pinned for the full TTL.

    This also bounds how long a document cached from a previous endpoint can
    stand in after the configured endpoint changes and the new one is down.
    """

    endpoint_a_document = {"paths": {"/a/": {}}}
    endpoint_b_document = {"paths": {"/b/": {}}}
    fetch = _SchemaFetchCounter(endpoint_a_document)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)
    clock = schema_contract_env["clock"]

    assert netbox_schema.resolve_netbox_schema_contract()["openapi"] is endpoint_a_document

    schema_contract_env["identity"]["value"] = (2, "https://netbox-b.example")
    fetch.document = None
    stand_in = netbox_schema.resolve_netbox_schema_contract()
    assert stand_in["source"] == "cache"
    assert fetch.calls == 2

    clock["now"] += netbox_schema.RETRY_CONTRACT_TTL_SECONDS - 1.0
    assert netbox_schema.resolve_netbox_schema_contract()["source"] == "cache"
    assert fetch.calls == 2

    fetch.document = endpoint_b_document
    clock["now"] += 1.0
    recovered = netbox_schema.resolve_netbox_schema_contract()
    assert recovered["source"] == "live"
    assert recovered["openapi"] is endpoint_b_document
    assert fetch.calls == 3


def test_netbox_schema_contract_ttl_zero_does_not_serialize_callers(
    monkeypatch, schema_contract_env
):
    """With reuse disabled nothing is shared, so callers must not queue behind a lock."""

    monkeypatch.setenv("PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL", "0")
    inside_fetch = threading.Barrier(4)

    def fetch(timeout: int = 20) -> dict[str, object]:
        # Passes only when all four callers are inside the fetch at the same time.
        inside_fetch.wait(timeout=5)
        return NETBOX_OPENAPI_SNAPSHOT

    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(netbox_schema.resolve_netbox_schema_contract) for _ in range(4)]
        sources = [future.result(timeout=10)["source"] for future in futures]

    assert sources == ["live"] * 4


def test_netbox_schema_contract_reset_forces_a_new_resolution(monkeypatch, schema_contract_env):
    fetch = _SchemaFetchCounter(NETBOX_OPENAPI_SNAPSHOT)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)

    netbox_schema.resolve_netbox_schema_contract()
    netbox_schema.reset_netbox_schema_contract_cache()
    netbox_schema.resolve_netbox_schema_contract()

    assert fetch.calls == 2


def test_netbox_schema_contract_callers_cannot_corrupt_the_reused_result(
    monkeypatch, schema_contract_env
):
    fetch = _SchemaFetchCounter(NETBOX_OPENAPI_SNAPSHOT)
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", fetch)

    first = netbox_schema.resolve_netbox_schema_contract()
    first["source"] = "tampered"

    assert netbox_schema.resolve_netbox_schema_contract()["source"] == "live"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, netbox_schema.DEFAULT_CONTRACT_TTL_SECONDS),
        ("900", 900.0),
        ("0", 0.0),
        ("-5", 0.0),
        ("not-a-number", netbox_schema.DEFAULT_CONTRACT_TTL_SECONDS),
    ],
)
def test_netbox_schema_contract_ttl_setting(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL", raising=False)
    else:
        monkeypatch.setenv("PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL", raw)

    assert netbox_schema.netbox_schema_contract_ttl_seconds() == expected


def test_netbox_openapi_persistence_resolves_env_over_plugin_setting(monkeypatch):
    """Persistence toggle resolves env override > plugin setting > default."""

    # Plugin setting disables persistence; no env override -> disabled.
    monkeypatch.delenv("PROXBOX_NETBOX_OPENAPI_PERSIST", raising=False)
    monkeypatch.setattr(
        netbox_schema.runtime_settings,
        "_load_settings",
        lambda: {"netbox_openapi_persist": False},
    )
    assert netbox_schema.netbox_openapi_persistence_enabled() is False

    # Env override wins over the plugin setting.
    monkeypatch.setenv("PROXBOX_NETBOX_OPENAPI_PERSIST", "true")
    assert netbox_schema.netbox_openapi_persistence_enabled() is True

    # Env can also force-disable regardless of the plugin setting.
    monkeypatch.setattr(
        netbox_schema.runtime_settings,
        "_load_settings",
        lambda: {"netbox_openapi_persist": True},
    )
    monkeypatch.setenv("PROXBOX_NETBOX_OPENAPI_PERSIST", "off")
    assert netbox_schema.netbox_openapi_persistence_enabled() is False

    # No env and no plugin value -> default enabled.
    monkeypatch.delenv("PROXBOX_NETBOX_OPENAPI_PERSIST", raising=False)
    monkeypatch.setattr(netbox_schema.runtime_settings, "_load_settings", lambda: None)
    assert netbox_schema.netbox_openapi_persistence_enabled() is True


def _vm_document(refs: Iterable[str], resolvable: bool = True) -> OpenAPIDocument:
    refs = list(refs)
    schemas = {ref.rsplit("/", 1)[1]: {} for ref in refs} if resolvable else {}
    return {
        "paths": {
            "/api/virtualization/virtual-machines/": {
                "get": {"responses": {str(i): {"$ref": ref} for i, ref in enumerate(refs)}}
            }
        },
        "components": {"schemas": schemas},
    }


@pytest.mark.parametrize("ref_set", VIRTUAL_MACHINE_SCHEMA_REF_SETS)
def test_vm_operation_schema_assertion_accepts_either_naming(ref_set: frozenset[str]) -> None:
    _assert_virtual_machine_operation_schemas(_vm_document(ref_set))


def test_vm_operation_schema_assertion_rejects_broken_documents() -> None:
    partial = sorted(VIRTUAL_MACHINE_SCHEMA_REF_SETS[1])[:2]
    with pytest.raises(AssertionError):
        _assert_virtual_machine_operation_schemas(_vm_document(partial))
    with pytest.raises(AssertionError):
        _assert_virtual_machine_operation_schemas(_vm_document([]))
    with pytest.raises(AssertionError):
        _assert_virtual_machine_operation_schemas(
            _vm_document(VIRTUAL_MACHINE_SCHEMA_REF_SETS[1], resolvable=False)
        )
