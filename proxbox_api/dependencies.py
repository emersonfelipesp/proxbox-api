"""FastAPI dependency providers shared by route modules."""

# Sessions
from typing import Annotated

from fastapi import Depends, Query, Request

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import RestRecord, ensure_tag_async
from proxbox_api.schemas.sync import (
    SyncBehaviorFlags,
    SyncOverwriteFlags,
    behavior_flags_from_query_params,
    overwrite_flags_from_query_params,
)
from proxbox_api.services.netbox_bootstrap import BootstrapStatus, run_netbox_bootstrap
from proxbox_api.session.netbox import NetBoxAsyncSessionDep, NetBoxSessionDep  # noqa: F401


async def proxbox_tag(netbox_session: NetBoxAsyncSessionDep) -> RestRecord:
    try:
        return await ensure_tag_async(
            netbox_session,
            name="Proxbox",
            slug="proxbox",
            color="ff5722",
            description="Proxbox Identifier (used to identify the items the plugin created)",
        )
    except ProxboxException as error:
        # Preserve the real underlying status and detail instead of flattening
        # every upstream failure to a bare HTTP 400. A NetBox 5xx during tag
        # ensure must surface as 5xx with its detail, so the plugin logs the
        # true cause rather than the opaque "Error ensuring Proxbox tag" 400.
        raise ProxboxException(
            message="Error ensuring Proxbox tag",
            detail=getattr(error, "detail", None),
            python_exception=error.python_exception or str(error),
            http_status_code=error.http_status_code,
        ) from error
    except Exception as error:
        # Arbitrary (non-Proxbox) error: no reliable status to preserve, but
        # still expose the real cause in the detail so it is not swallowed.
        raise ProxboxException(
            message="Error ensuring Proxbox tag",
            detail=str(error),
            python_exception=str(error),
        ) from error


# Proxbox Tag Dependency (used to identify the items the plugin created)
# It's used to tag the items created by the plugin
# NetBox Tag Object.
ProxboxTagDep = Annotated[RestRecord, Depends(proxbox_tag)]


async def ensure_netbox_sync_dependencies(
    netbox_session: NetBoxAsyncSessionDep,
) -> BootstrapStatus:
    """Reconcile NetBox-owned support objects before a sync route writes data."""
    try:
        status = await run_netbox_bootstrap(netbox_session, enabled=True)
    except ProxboxException:
        raise
    except Exception as error:
        raise ProxboxException(
            message="Error ensuring NetBox sync dependencies",
            python_exception=str(error),
        ) from error

    if status.warnings:
        logger.warning(
            "NetBox sync dependency bootstrap completed with %d warning(s): %s",
            len(status.warnings),
            status.warnings,
        )
    return status


NetBoxSyncDependenciesDep = Annotated[
    BootstrapStatus,
    Depends(ensure_netbox_sync_dependencies),
]


def resolved_sync_overwrite_flags(
    request: Request,
    overwrite_flags: Annotated[SyncOverwriteFlags, Query()] = SyncOverwriteFlags(),
) -> SyncOverwriteFlags:
    return overwrite_flags_from_query_params(request.query_params, overwrite_flags)


ResolvedSyncOverwriteFlagsDep = Annotated[
    SyncOverwriteFlags,
    Depends(resolved_sync_overwrite_flags),
]


def resolved_sync_behavior_flags(
    request: Request,
    behavior_flags: Annotated[SyncBehaviorFlags, Query()] = SyncBehaviorFlags(),
) -> SyncBehaviorFlags:
    return behavior_flags_from_query_params(request.query_params, behavior_flags)


ResolvedSyncBehaviorFlagsDep = Annotated[
    SyncBehaviorFlags,
    Depends(resolved_sync_behavior_flags),
]
