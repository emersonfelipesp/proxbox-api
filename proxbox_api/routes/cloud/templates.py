"""Cloud Portal template catalog route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.schemas.cloud_provision import CloudTemplateListResponse, CloudTemplateSummary
from proxbox_api.session.netbox import get_netbox_async_session

router = APIRouter()

_TEMPLATES_PATH = "/api/plugins/proxbox/cloud-image-templates/"


def _record_dict(record: object) -> dict[str, object]:
    if hasattr(record, "serialize"):
        serialized = record.serialize()
        return serialized if isinstance(serialized, dict) else {}
    return dict(record) if isinstance(record, dict) else {}


def _nested_int(value: object) -> int | None:
    if isinstance(value, dict):
        raw_id = value.get("id")
    else:
        raw_id = getattr(value, "id", None)
    try:
        return int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        return None


def _nested_name(value: object) -> str | None:
    if isinstance(value, dict):
        raw_name = value.get("name") or value.get("display")
    else:
        raw_name = getattr(value, "name", None) or getattr(value, "display", None)
    return str(raw_name) if raw_name else None


def _tenant_ids(value: object) -> list[int]:
    tenants = value if isinstance(value, list) else []
    tenant_ids: list[int] = []
    for tenant in tenants:
        tenant_id = _nested_int(tenant)
        if tenant_id is not None:
            tenant_ids.append(tenant_id)
    return tenant_ids


def _summary_from_record(record: object) -> CloudTemplateSummary:
    data = _record_dict(record)
    cluster = data.get("cluster")
    return CloudTemplateSummary(
        id=int(data["id"]),
        name=str(data.get("name") or ""),
        slug=str(data.get("slug") or ""),
        cluster_id=_nested_int(cluster),
        cluster_name=_nested_name(cluster),
        source_vmid=int(data["source_vmid"]),
        os_family=str(data.get("os_family") or ""),
        os_release=str(data.get("os_release") or ""),
        default_ciuser=str(data.get("default_ciuser") or "cloud-user"),
        is_active=bool(data.get("is_active", True)),
        allowed_tenant_ids=_tenant_ids(data.get("allowed_tenants")),
    )


@router.get("/templates", response_model=CloudTemplateListResponse)
async def list_cloud_templates(session: SessionDep) -> CloudTemplateListResponse:
    try:
        nb = await get_netbox_async_session(database_session=session)
        records = await rest_list_async(
            nb,
            _TEMPLATES_PATH,
            query={"limit": 0, "is_active": "true"},
        )
    except ProxboxException as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "netbox_templates_unavailable", "error": str(error)},
        ) from error

    results = [_summary_from_record(record) for record in records]
    return CloudTemplateListResponse(count=len(results), results=results)
