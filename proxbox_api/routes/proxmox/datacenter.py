"""Proxmox datacenter-level endpoints.

Exposes PVE 9.2 features:
- Custom CPU models CRUD (``GET/POST /cluster/qemu/custom-cpu-models``,
  ``GET/PUT/DELETE /cluster/qemu/custom-cpu-models/{cputype}``) — allows
  managing the set of virtual CPU models exposed to VMs from the API.
- Datacenter options (``GET/PUT /cluster/options``) — includes the PVE 9.2
  CRS sub-object and the new node physical ``location`` field.

All write routes (POST, PUT, DELETE) require that the request is directed
to a single cluster via the ``cluster_name`` query parameter.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session_for_request
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


# ---------------------------------------------------------------------------
# Custom CPU models (PVE 9.2)
# ---------------------------------------------------------------------------


class CustomCpuModelSchema(BaseModel):
    """A custom CPU model definition."""

    cluster_name: str | None = None
    cputype: str | None = None
    base_cputype: str | None = None
    flags: str | None = None
    vendor_id: str | None = None
    level: int | None = None
    description: str | None = None
    status: str = "ok"
    error: str | None = None


def _to_cpu_model(cluster_name: str, raw: object) -> CustomCpuModelSchema:
    data: dict[str, object] = {}
    if hasattr(raw, "model_dump"):
        data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
    elif isinstance(raw, dict):
        data = dict(raw)
    return CustomCpuModelSchema(
        cluster_name=cluster_name,
        cputype=str(data.get("cputype") or data.get("name"))
        if (data.get("cputype") or data.get("name")) is not None
        else None,
        base_cputype=str(data.get("base-cputype") or data.get("base_cputype"))
        if (data.get("base-cputype") or data.get("base_cputype")) is not None
        else None,
        flags=str(data.get("flags")) if data.get("flags") is not None else None,
        vendor_id=str(data.get("vendor_id") or data.get("vendor-id"))
        if (data.get("vendor_id") or data.get("vendor-id")) is not None
        else None,
        level=int(data["level"]) if isinstance(data.get("level"), (int, str)) else None,
        description=str(data.get("description")) if data.get("description") is not None else None,
    )


@router.get("/datacenter/cpu-models", response_model=list[CustomCpuModelSchema])
async def list_custom_cpu_models(pxs: ProxmoxSessionsDep) -> list[CustomCpuModelSchema]:
    """List all custom CPU models across all clusters (PVE 9.2+).

    Proxies ``GET /cluster/qemu/custom-cpu-models``.  Custom CPU models
    allow tailoring the virtual CPU exposed to VMs to a specific feature
    set, enabling cluster-wide compatibility management.
    """
    results: list[CustomCpuModelSchema] = []
    for px in pxs:
        try:
            raw = await resolve_async(px.session("cluster/qemu/custom-cpu-models").get())
            rows = raw if isinstance(raw, list) else (raw.root if hasattr(raw, "root") else [raw])
            for row in rows:
                results.append(_to_cpu_model(px.name, row))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error fetching custom CPU models for Proxmox cluster %s", px.name)
            results.append(
                CustomCpuModelSchema(cluster_name=px.name, status="error", error=str(exc))
            )
    return results


@router.get("/datacenter/cpu-models/{cputype}", response_model=CustomCpuModelSchema | None)
async def get_custom_cpu_model(
    pxs: ProxmoxSessionsDep,
    cputype: str,
    cluster_name: str | None = Query(None, description="Target cluster name"),
) -> CustomCpuModelSchema | None:
    """Get a single custom CPU model by name (PVE 9.2+).

    Proxies ``GET /cluster/qemu/custom-cpu-models/{cputype}``.
    Returns ``null`` when no cluster has a model with that name.
    """
    for px in pxs:
        if cluster_name and px.name != cluster_name:
            continue
        try:
            raw = await resolve_async(px.session(f"cluster/qemu/custom-cpu-models/{cputype}").get())
            if raw is not None:
                return _to_cpu_model(px.name, raw)
        except Exception:  # noqa: BLE001
            logger.debug("Custom CPU model %s not found on cluster %s", cputype, px.name)
    return None


class CustomCpuModelCreateSchema(BaseModel):
    """Payload for creating a custom CPU model."""

    cputype: str
    base_cputype: str | None = None
    flags: str | None = None
    vendor_id: str | None = None
    level: int | None = None
    description: str | None = None


@router.post("/datacenter/cpu-models", response_model=CustomCpuModelSchema)
async def create_custom_cpu_model(
    pxs: ProxmoxSessionsDep,
    payload: CustomCpuModelCreateSchema,
    cluster_name: str | None = Query(None, description="Target cluster name"),
) -> CustomCpuModelSchema:
    """Create a custom CPU model (PVE 9.2+).

    Proxies ``POST /cluster/qemu/custom-cpu-models``.
    Specify ``cluster_name`` to target a specific cluster when more than
    one is configured.
    """
    px = resolve_proxmox_session_for_request(
        pxs, cluster_name, resource_name="create custom CPU model"
    )
    body: dict[str, object] = {"cputype": payload.cputype}
    if payload.base_cputype is not None:
        body["base-cputype"] = payload.base_cputype
    if payload.flags is not None:
        body["flags"] = payload.flags
    if payload.vendor_id is not None:
        body["vendor-id"] = payload.vendor_id
    if payload.level is not None:
        body["level"] = payload.level
    if payload.description is not None:
        body["description"] = payload.description
    try:
        raw = await resolve_async(px.session("cluster/qemu/custom-cpu-models").post(**body))
        if raw is None:
            return _to_cpu_model(px.name, {"cputype": payload.cputype})
        return _to_cpu_model(px.name, raw)
    except Exception as exc:
        logger.exception("Error creating custom CPU model on cluster %s", px.name)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class CustomCpuModelUpdateSchema(BaseModel):
    """Payload for updating a custom CPU model."""

    base_cputype: str | None = None
    flags: str | None = None
    vendor_id: str | None = None
    level: int | None = None
    description: str | None = None


@router.put("/datacenter/cpu-models/{cputype}", response_model=CustomCpuModelSchema)
async def update_custom_cpu_model(
    pxs: ProxmoxSessionsDep,
    cputype: str,
    payload: CustomCpuModelUpdateSchema,
    cluster_name: str | None = Query(None, description="Target cluster name"),
) -> CustomCpuModelSchema:
    """Update a custom CPU model (PVE 9.2+).

    Proxies ``PUT /cluster/qemu/custom-cpu-models/{cputype}``.
    """
    px = resolve_proxmox_session_for_request(
        pxs, cluster_name, resource_name="update custom CPU model"
    )
    body: dict[str, object] = {}
    if payload.base_cputype is not None:
        body["base-cputype"] = payload.base_cputype
    if payload.flags is not None:
        body["flags"] = payload.flags
    if payload.vendor_id is not None:
        body["vendor-id"] = payload.vendor_id
    if payload.level is not None:
        body["level"] = payload.level
    if payload.description is not None:
        body["description"] = payload.description
    try:
        raw = await resolve_async(
            px.session(f"cluster/qemu/custom-cpu-models/{cputype}").put(**body)
        )
        if raw is None:
            return _to_cpu_model(px.name, {"cputype": cputype})
        return _to_cpu_model(px.name, raw)
    except Exception as exc:
        logger.exception("Error updating custom CPU model %s on cluster %s", cputype, px.name)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/datacenter/cpu-models/{cputype}", response_model=CustomCpuModelSchema)
async def delete_custom_cpu_model(
    pxs: ProxmoxSessionsDep,
    cputype: str,
    cluster_name: str | None = Query(None, description="Target cluster name"),
) -> CustomCpuModelSchema:
    """Delete a custom CPU model (PVE 9.2+).

    Proxies ``DELETE /cluster/qemu/custom-cpu-models/{cputype}``.
    """
    px = resolve_proxmox_session_for_request(
        pxs, cluster_name, resource_name="delete custom CPU model"
    )
    try:
        await resolve_async(px.session(f"cluster/qemu/custom-cpu-models/{cputype}").delete())
        return CustomCpuModelSchema(cluster_name=px.name, cputype=cputype)
    except Exception as exc:
        logger.exception("Error deleting custom CPU model %s on cluster %s", cputype, px.name)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Datacenter options — includes CRS and node location (PVE 9.2)
# ---------------------------------------------------------------------------


class DatacenterOptionsSchema(BaseModel):
    """Datacenter-level options from ``GET /cluster/options``.

    Includes the PVE 9.2 ``crs`` sub-object (Cluster Resource Scheduler
    configuration) and the ``location`` field for physical site mapping.
    All fields are optional — only populated values are returned.
    """

    cluster_name: str | None = None
    keyboard: str | None = None
    language: str | None = None
    email_from: str | None = None
    max_workers: int | None = None
    migration_unsecure: bool | None = None
    next_id: dict[str, object] | None = None
    notify: dict[str, object] | None = None
    crs: dict[str, object] | None = None
    location: str | None = None
    status: str = "ok"
    error: str | None = None


@router.get("/datacenter/options", response_model=list[DatacenterOptionsSchema])
async def datacenter_options(pxs: ProxmoxSessionsDep) -> list[DatacenterOptionsSchema]:
    """Retrieve datacenter options across all clusters.

    Proxies ``GET /cluster/options``.  Exposes the PVE 9.2 ``crs``
    sub-object (CRS dynamic load balancer mode) and the new ``location``
    field for physical site mapping.
    """
    results: list[DatacenterOptionsSchema] = []
    for px in pxs:
        try:
            raw = await resolve_async(px.session("cluster/options").get())
            data: dict[str, object] = {}
            if hasattr(raw, "model_dump"):
                data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
            elif isinstance(raw, dict):
                data = dict(raw)
            crs = data.get("crs")
            results.append(
                DatacenterOptionsSchema(
                    cluster_name=px.name,
                    keyboard=str(data["keyboard"]) if data.get("keyboard") is not None else None,
                    language=str(data["language"]) if data.get("language") is not None else None,
                    email_from=str(data.get("email_from") or data.get("email-from"))
                    if (data.get("email_from") or data.get("email-from")) is not None
                    else None,
                    max_workers=int(data["max_workers"])
                    if isinstance(data.get("max_workers"), (int, str))
                    else None,
                    migration_unsecure=bool(
                        data.get("migration_unsecure", data.get("migration-unsecure"))
                    )
                    if "migration_unsecure" in data or "migration-unsecure" in data
                    else None,
                    next_id=dict(data["next_id"])
                    if isinstance(data.get("next_id"), dict)
                    else None,
                    notify=dict(data["notify"]) if isinstance(data.get("notify"), dict) else None,
                    crs=dict(crs) if isinstance(crs, dict) else None,
                    location=str(data["location"]) if data.get("location") is not None else None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error fetching datacenter options for Proxmox cluster %s", px.name)
            results.append(
                DatacenterOptionsSchema(cluster_name=px.name, status="error", error=str(exc))
            )
    return results
