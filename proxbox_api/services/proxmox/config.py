"""VM config resolution helpers used by routes and sync services."""

from __future__ import annotations

from typing import Iterable

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.services.proxmox_helpers import get_vm_config as get_typed_vm_config


def _session_label(px: object) -> str:
    return (
        str(getattr(px, "name", "") or "")
        or str(getattr(px, "domain", "") or "")
        or str(getattr(px, "ip_address", "") or "")
        or "unknown"
    )


def _format_session_error(error: ProxboxException) -> str:
    parts = [str(error.message)]
    if error.detail:
        parts.append(f"detail={error.detail}")
    if error.python_exception:
        parts.append(f"python_exception={error.python_exception}")
    return " | ".join(parts)


async def resolve_vm_config(
    *,
    pxs: Iterable[object],
    node: str,
    vm_type: str,
    vmid: int,
) -> dict[str, object]:
    """Resolve a VM config across all Proxmox sessions and return the dumped payload.

    Iterates sessions directly without a cluster-status preflight.  The preflight
    was fragile (FQDN vs short-name mismatches, stale cluster data, multi-cluster
    topologies) and unnecessary because ProxmoxSessionsDep already scopes sessions
    to the requested endpoint via the name/endpoint_ids query params.

    Raises:
        ProxboxException: invalid VM type, no session returned a config, or an
            unhandled downstream error.
    """
    if vm_type not in ("qemu", "lxc"):
        raise ProxboxException(
            message="Invalid VM Type. Use 'qemu' or 'lxc'.",
            http_status_code=400,
        )

    errors: list[str] = []
    try:
        for px in pxs:
            try:
                config = await get_typed_vm_config(px, node=node, vm_type=vm_type, vmid=vmid)
                if config:
                    return config.model_dump(
                        mode="python",
                        by_alias=True,
                        exclude_none=True,
                    )
            except ProxboxException as error:
                errors.append(f"{_session_label(px)}: {_format_session_error(error)}")

        raise ProxboxException(
            message="VM Config not found.",
            detail=(
                "VM Config not found. Check if the 'node', 'type', and 'vmid' are correct."
                + (f" Session errors: {'; '.join(errors)}" if errors else "")
            ),
        )
    except ProxboxException:
        raise
    except Exception as error:
        logger.exception(
            "Unhandled error while getting VM config for node=%s type=%s vmid=%s",
            node,
            vm_type,
            vmid,
        )
        raise ProxboxException(
            message="Unknown error getting VM Config. Search parameters probably wrong.",
            detail="Check if the node, type, and vmid are correct.",
            python_exception=str(error),
        )
