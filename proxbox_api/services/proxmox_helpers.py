"""Typed helpers for direct proxmoxer calls validated through generated models."""

from __future__ import annotations

from typing import Any

from proxbox_api.exception import ProxboxException
from proxbox_api.generated.proxmox.latest import pydantic_models as generated_models
from proxbox_api.session.proxmox import ProxmoxSession


def _model_dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="python", by_alias=True, exclude_none=True)


def _wrap_backend_call(message: str, operation):
    try:
        return operation()
    except ProxboxException:
        raise
    except Exception as error:
        raise ProxboxException(message=message, python_exception=str(error))


def get_cluster_status(
    session: ProxmoxSession,
) -> list[generated_models.GetClusterStatusResponseItem]:
    return _wrap_backend_call(
        "Error fetching Proxmox cluster status",
        lambda: generated_models.GetClusterStatusResponse.model_validate(
            session.session("cluster/status").get()
        ).root,
    )


def get_cluster_resources(
    session: ProxmoxSession,
    resource_type: str | None = None,
) -> list[generated_models.GetClusterResourcesResponseItem]:
    return _wrap_backend_call(
        "Error fetching Proxmox cluster resources",
        lambda: generated_models.GetClusterResourcesResponse.model_validate(
            session.session("cluster/resources").get(type=resource_type)
            if resource_type
            else session.session("cluster/resources").get()
        ).root,
    )


def get_vm_config(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> (
    generated_models.GetNodesNodeQemuVmidConfigResponse
    | generated_models.GetNodesNodeLxcVmidConfigResponse
):
    def _fetch_config():
        if vm_type == "qemu":
            payload = session.session.nodes(node).qemu(vmid).config.get()
            return generated_models.GetNodesNodeQemuVmidConfigResponse.model_validate(payload)
        if vm_type == "lxc":
            payload = session.session.nodes(node).lxc(vmid).config.get()
            return generated_models.GetNodesNodeLxcVmidConfigResponse.model_validate(payload)
        raise ValueError(f"Unsupported VM type: {vm_type}")

    return _wrap_backend_call("Error fetching Proxmox VM config", _fetch_config)


def get_storage_list(
    session: ProxmoxSession,
) -> list[generated_models.GetStorageResponseItem]:
    return _wrap_backend_call(
        "Error fetching Proxmox storage list",
        lambda: generated_models.GetStorageResponse.model_validate(
            session.session.storage.get()
        ).root,
    )


def get_node_storage_content(
    session: ProxmoxSession,
    node: str,
    storage: str,
    **kwargs: Any,
) -> list[generated_models.GetNodesNodeStorageStorageContentResponseItem]:
    params = {key: value for key, value in kwargs.items() if value is not None}
    return _wrap_backend_call(
        "Error fetching Proxmox node storage content",
        lambda: generated_models.GetNodesNodeStorageStorageContentResponse.model_validate(
            session.session.nodes(node).storage(storage).content.get(**params)
        ).root,
    )


def dump_models(items: list[Any]) -> list[dict[str, Any]]:
    return [_model_dump(item) for item in items]
