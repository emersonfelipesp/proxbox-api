from typing import Annotated
from fastapi import Depends
from proxbox_api.database import DatabaseSessionDep, NetBoxEndpoint
from proxbox_api.exception import ProxboxException
from sqlmodel import select
import pynetbox

def get_netbox_session(database_session: DatabaseSessionDep) -> pynetbox.api:
    """
    Get a NetBox API parameters from database and establish pynetbox API session
    """
    try:
        # Get the first NetBox endpoint from the database
        netbox_endpoint = database_session.exec(select(NetBoxEndpoint)).first()
        
        if not netbox_endpoint:
            raise ProxboxException(
                message="No NetBox endpoint found",
                detail="Please add a NetBox endpoint in the database"
            )
        
        # Establish pynetbox API session
        netbox_session = pynetbox.api(
            netbox_endpoint.url,
            token=netbox_endpoint.token,
            threading=True
        )
        
        return netbox_session

    except ProxboxException as error: raise error
    
    except Exception as error:
        raise ProxboxException(
            message="Error establishing NetBox API session",
            python_exception=str(error)
        )

NetBoxSessionDep = Annotated[pynetbox.api, Depends(get_netbox_session)]





