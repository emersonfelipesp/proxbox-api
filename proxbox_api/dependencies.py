"""FastAPI dependency providers shared by route modules."""

# Sessions
from typing import Annotated, Any

from fastapi import Depends

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import ensure_tag_async
from proxbox_api.session.netbox import NetBoxAsyncSessionDep


async def proxbox_tag(netbox_session: NetBoxAsyncSessionDep):
    try:
        return await ensure_tag_async(
            netbox_session,
            name="Proxbox",
            slug="proxbox",
            color="ff5722",
            description="Proxbox Identifier (used to identify the items the plugin created)",
        )
    except ProxboxException:
        raise
    except Exception as error:
        raise ProxboxException(
            message="Error ensuring Proxbox tag",
            python_exception=str(error),
        )


# Proxbox Tag Dependency (used to identify the items the plugin created)
# It's used to tag the items created by the plugin
# NetBox Tag Object.
ProxboxTagDep = Annotated[Any, Depends(proxbox_tag)]
