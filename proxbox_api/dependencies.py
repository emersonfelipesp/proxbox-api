"""FastAPI dependency providers shared by route modules."""

# Sessions
from typing import Annotated

from fastapi import Depends, Query, Request

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import RestRecord, ensure_tag_async
from proxbox_api.schemas.sync import (
    SyncBehaviorFlags,
    SyncOverwriteFlags,
    behavior_flags_from_query_params,
    overwrite_flags_from_query_params,
)
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
        raise ProxboxException(
            message="Error ensuring Proxbox tag",
            python_exception=str(error),
        ) from error
    except Exception as error:
        raise ProxboxException(
            message="Error ensuring Proxbox tag",
            python_exception=str(error),
        ) from error


# Proxbox Tag Dependency (used to identify the items the plugin created)
# It's used to tag the items created by the plugin
# NetBox Tag Object.
ProxboxTagDep = Annotated[RestRecord, Depends(proxbox_tag)]


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
