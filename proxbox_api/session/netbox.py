"""NetBox API session creation and dependency wiring."""

from typing import Any
from typing import Annotated
from fastapi import Depends
from proxbox_api.netbox_sdk_sync import SyncProxy
from proxbox_api.database import DatabaseSessionDep, NetBoxEndpoint
from proxbox_api.exception import ProxboxException
from sqlmodel import select
from netbox_sdk import api


def get_netbox_session(database_session: DatabaseSessionDep) -> Any:
    """
    Get NetBox API parameters from database and establish a netbox-sdk API session.
    """
    try:
        # Get the first NetBox endpoint from the database
        netbox_endpoint = database_session.exec(select(NetBoxEndpoint)).first()

        if not netbox_endpoint:
            raise ProxboxException(
                message="No NetBox endpoint found",
                detail="Please add a NetBox endpoint in the database",
            )

        # Establish netbox-sdk API session
        netbox_session = SyncProxy(
            api(netbox_endpoint.url, token=netbox_endpoint.token)
        )

        return netbox_session

    except ProxboxException as error:
        raise error

    except Exception as error:
        raise ProxboxException(
            message="Error establishing NetBox API session", python_exception=str(error)
        )


NetBoxSessionDep = Annotated[Any, Depends(get_netbox_session)]
