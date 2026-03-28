"""FastAPI dependency providers shared by route modules."""

# Sessions
from proxbox_api.session.netbox import NetBoxSessionDep
from proxbox_api.database import DatabaseSessionDep
from proxbox_api.netbox_sdk_helpers import ensure_tag

from fastapi import Depends
from typing import Annotated, Any


async def proxbox_tag(netbox_session: NetBoxSessionDep):
    return await ensure_tag(
        netbox_session,
        name="Proxbox",
        slug="proxbox",
        color="ff5722",
        description="Proxbox Identifier (used to identify the items the plugin created)",
    )


# Proxbox Tag Dependency (used to identify the items the plugin created)
# It's used to tag the items created by the plugin
# NetBox Tag Object.
ProxboxTagDep = Annotated[Any, Depends(proxbox_tag)]
