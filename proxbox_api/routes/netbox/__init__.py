from fastapi import APIRouter, Depends, Query, HTTPException, Depends
from sqlmodel import select

from typing import Annotated, Any

from proxbox_api.exception import ProxboxException          
from proxbox_api.database import NetBoxEndpoint         
from proxbox_api.dependencies import DatabaseSessionDep as SessionDep, NetBoxSessionDep

# FastAPI Router
router = APIRouter()

#
# Endpoints: /netbox/<endpoint>
#

@router.post('/endpoint')
def create_netbox_endpoint(netbox: NetBoxEndpoint, session: SessionDep) -> NetBoxEndpoint:
    if session.exec(select(NetBoxEndpoint).where(NetBoxEndpoint.id == netbox.id, NetBoxEndpoint.name == netbox.name)).first():
        raise HTTPException(status_code=400, detail="NetBox Endpoint already exists")
    session.add(netbox)
    session.commit()
    session.refresh(netbox)
    return netbox

@router.get('/endpoint')
def get_netbox_endpoints(
    session: SessionDep,
    offset: int = 0,
    limit: Annotated[int, Query(le=100)] = 100
) -> list[NetBoxEndpoint]:
    netbox_endpoints = session.exec(select(NetBoxEndpoint).offset(offset).limit(limit)).all()
    return list(netbox_endpoints)

GetNetBoxEndpoint = Annotated[list[NetBoxEndpoint], Depends(get_netbox_endpoints)]

@router.get('/endpoint/{netbox_id}')
def get_netbox_endpoint(netbox_id: int, session: SessionDep) -> NetBoxEndpoint:
    netbox_endpoint = session.get(NetBoxEndpoint, netbox_id)
    if not netbox_endpoint:
        raise HTTPException(status_code=404, detail="Netbox Endpoint not found")
    return netbox_endpoint

@router.put('/endpoint/{netbox_id}')
def update_netbox_endpoint(netbox_id: int, netbox: NetBoxEndpoint, session: SessionDep) -> NetBoxEndpoint:
    db_netbox = session.get(NetBoxEndpoint, netbox_id)
    if not db_netbox:
        raise HTTPException(status_code=404, detail="NetBox Endpoint not found")
    
    # Update the existing endpoint with new data
    for key, value in netbox.dict(exclude_unset=True).items():
        setattr(db_netbox, key, value)
    
    session.add(db_netbox)
    session.commit()
    session.refresh(db_netbox)
    return db_netbox

@router.delete('/endpoint/{netbox_id}')
def delete_netbox_endpoint(netbox_id: int, session: SessionDep) -> dict:
    netbox_endpoint = session.get(NetBoxEndpoint, netbox_id)
    if not netbox_endpoint:
        raise HTTPException(status_code=404, detail='NetBox Endpoint not found.')
    session.delete(netbox_endpoint)
    session.commit()
    return {'message': 'NetBox Endpoint deleted.'}


@router.get("/status")
async def netbox_status(netbox_session: NetBoxSessionDep):
    """
    ### Asynchronously retrieves the status of the Netbox session.
    
    **Returns:**
    - The status of the Netbox session.
    """
    
    try:
        return netbox_session.status()
    except Exception as error:
        raise ProxboxException(
            message='Error fetching status from NetBox API.',
            python_exception=str(error)
        )


@router.get("/openapi")
async def netbox_openapi(netbox_session: NetBoxSessionDep):
    """
    ### Fetches the OpenAPI documentation from the Netbox session.
    
    **Returns:**
    - **dict:** The OpenAPI documentation retrieved from the Netbox session.
    """
    
    from proxbox_api.session.netbox import get_netbox_session
    from proxbox_api.database import get_session
    
    try:
        output = netbox_session.openapi()
        return output
    except Exception as error:
        raise ProxboxException(
            message='Error fetching OpenAPI documentation from NetBox API.',
            python_exception=str(error)
        )



