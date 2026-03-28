"""FastAPI application entrypoint and route registration."""

import asyncio
import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from proxbox_api.database import NetBoxEndpoint, create_db_and_tables, get_session
from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep

# Proxbox API Imports
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_compat import NetBoxBase
from proxbox_api.openapi_custom import custom_openapi_builder

# ProxBox Admin Panel Routes
from proxbox_api.routes.admin import router as admin_router
from proxbox_api.routes.dcim import router as dcim_router
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.extras import router as extras_router
from proxbox_api.routes.netbox import router as netbox_router

# Proxmox Routes
from proxbox_api.routes.proxmox import router as proxmox_router

# Proxmox Deps
from proxbox_api.routes.proxmox.cluster import (
    ClusterResourcesDep,
    ClusterStatusDep,
)
from proxbox_api.routes.proxmox.cluster import (
    router as px_cluster_router,
)
from proxbox_api.routes.proxmox.nodes import router as px_nodes_router
from proxbox_api.routes.virtualization import router as virtualization_router
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines

# Proxbox API route imports
from proxbox_api.routes.virtualization.virtual_machines import (
    router as virtual_machines_router,
)
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.session.netbox import get_netbox_session

# Sessions
from proxbox_api.session.proxmox import ProxmoxSessionsDep

"""
CORS ORIGINS
"""

configuration = None
default_config: dict = {}
plugin_configuration: dict = {}
proxbox_cfg: dict = {}

PROXBOX_PLUGIN_NAME: str = "netbox_proxbox"

# Init FastAPI
app = FastAPI(
    title="Proxbox Backend",
    description="## Proxbox Backend made in FastAPI framework",
    version="0.0.1",
)

base_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(base_dir, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def custom_openapi():
    """Override FastAPI OpenAPI generation with embedded Proxmox generated schema."""

    return custom_openapi_builder(app)


app.openapi = custom_openapi

netbox_endpoint = None
database_session = None
netbox_endpoints = []
try:
    create_db_and_tables()
    database_session = next(get_session())
    netbox_session = get_netbox_session(database_session=database_session)
    NetBoxBase.nb = netbox_session

except Exception as error:
    print(error)
    pass

if database_session:
    try:
        netbox_endpoints = database_session.exec(select(NetBoxEndpoint)).all()
    except OperationalError:
        # If table does not exist, create it.
        create_db_and_tables()
        netbox_endpoints = database_session.exec(select(NetBoxEndpoint)).all()


origins = []
"""
CORS Middleware
"""
if netbox_endpoints:
    for netbox_endpoint in netbox_endpoints:
        protocol = "https" if netbox_endpoint.verify_ssl else "http"
        origins.extend(
            [
                f"{protocol}://{netbox_endpoint.domain}",
                f"{protocol}://{netbox_endpoint.domain}:80",
                f"{protocol}://{netbox_endpoint.domain}:443",
                f"{protocol}://{netbox_endpoint.domain}:8000",
            ]
        )

# Add default development origins (API + Next.js dev server on typical ports)
origins.extend(
    [
        "https://127.0.0.1:443",
        "http://127.0.0.1:80",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ]
)

for part in os.environ.get("PROXBOX_CORS_EXTRA_ORIGINS", "").split(","):
    origin = part.strip().rstrip("/")
    if origin:
        origins.append(origin)

origins = list(dict.fromkeys(origins))

print(origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ProxboxException)
async def proxmoxer_exception_handler(request: Request, exc: ProxboxException):
    return JSONResponse(
        status_code=400,
        content={
            "message": exc.message,
            "detail": exc.detail,
            "python_exception": exc.python_exception,
        },
    )


@app.get("/")
async def standalone_info():
    return {
        "message": "Proxbox Backend made in FastAPI framework",
        "proxbox": {
            "github": "https://github.com/netdevopsbr/netbox-proxbox",
            "docs": "https://docs.netbox.dev.br",
        },
        "fastapi": {
            "github": "https://github.com/tiangolo/fastapi",
            "website": "https://fastapi.tiangolo.com/",
            "reason": "FastAPI was chosen because of performance and reliability.",
        },
    }


from proxbox_api.cache import global_cache


@app.get("/cache")
async def get_cache():
    return global_cache.return_cache()


@app.get("/clear-cache")
async def clear_cache():
    global_cache.clear_cache()
    return {"message": "Cache cleared"}


from datetime import datetime


class SyncProcessIn(BaseModel):
    name: str
    sync_type: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None


class SyncProcess(SyncProcessIn):
    id: int
    url: str
    display: str


# Example instance
example_sync_process = SyncProcess(
    id=1,
    url="https://10.0.30.200/api/plugins/proxbox/sync-processes/1/",
    display="teste (all)",
    name="teste",
    sync_type="all",
    status="not-started",
    started_at="2025-03-13T15:08:09.051478Z",
    completed_at="2025-03-13T15:08:09.051478Z",
)


@app.get("/sync-processes", response_model=list[SyncProcess])
async def get_sync_processes():
    """
    Get all sync processes from Netbox.
    """

    nb = get_raw_netbox_session()
    if nb is None:
        raise ProxboxException(message="Failed to establish NetBox session")

    try:
        sync_processes = [
            process.serialize()
            for process in nb.plugins.proxbox.__getattr__("sync-processes").all()
        ]
        return sync_processes
    except Exception as error:
        raise ProxboxException(message="Error fetching sync processes", python_exception=str(error))


@app.post("/sync-processes", response_model=SyncProcess)
async def create_sync_process():
    """
    Create a new sync process in Netbox.
    """

    print(datetime.now())

    nb = get_raw_netbox_session()
    if nb is None:
        raise ProxboxException(message="Failed to establish NetBox session")

    try:
        sync_process = nb.plugins.proxbox.__getattr__("sync-processes").create(
            name=f"sync-process-{datetime.now()}",
            sync_type="all",
            status="not-started",
            started_at=str(datetime.now()),
            completed_at=str(datetime.now()),
        )
        return sync_process
    except Exception as error:
        raise ProxboxException(message="Error creating sync process", python_exception=str(error))


#
# Routes (Endpoints)
#

# Admin Routes
app.include_router(admin_router, prefix="/admin", tags=["admin"])

# Netbox Routes
app.include_router(netbox_router, prefix="/netbox", tags=["netbox"])

# Proxmox Routes
app.include_router(px_nodes_router, prefix="/proxmox/nodes", tags=["proxmox / nodes"])
app.include_router(px_cluster_router, prefix="/proxmox/cluster", tags=["proxmox / cluster"])
app.include_router(proxmox_router, prefix="/proxmox", tags=["proxmox"])

# DCIM Routes
app.include_router(dcim_router, prefix="/dcim", tags=["dcim"])

# Virtualization Routes
app.include_router(virtualization_router, prefix="/virtualization", tags=["virtualization"])
app.include_router(
    virtual_machines_router,
    prefix="/virtualization/virtual-machines",
    tags=["virtualization / virtual-machines"],
)

# Extras Routes
app.include_router(extras_router, prefix="/extras", tags=["extras"])


@app.websocket("/")
async def base_websocket(websocket: WebSocket):
    count = 0

    await websocket.accept()
    try:
        while True:
            # data = await websocket.receive_text()
            # await websocket.send_text(f"Message text was: {data}")
            count = count + 1
            await websocket.send_text(f"Message: {count}")
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        print("WebSocket connection closed")


@app.websocket("/ws/virtual-machines")
async def websocket_virtual_machines(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
):
    print("route ws/virtual-machines reached")

    try:
        await websocket.accept()
        await websocket.send_text("Connected!")
    except Exception as error:
        print(f"Error while accepting WebSocket connection: {error}")
        try:
            await websocket.close()
        except Exception as error:
            print(f"Error while closing WebSocket connection: {error}")

    await create_virtual_machines(
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        custom_fields=custom_fields,
        websocket=websocket,
        tag=tag,
        use_css=False,
    )


@app.get("/full-update")
async def full_update_sync(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
):
    start_time = datetime.now()
    sync_process = None

    try:
        sync_process = netbox_session.plugins.proxbox.__getattr__("sync-processes").create(
            name=f"sync-all-{start_time}",
            sync_type="all",
            status="not-started",
            started_at=str(start_time),
            completed_at=None,
            runtime=None,
            tags=[tag.get("id", 0)],
        )
    except Exception as error:
        print(error)
        raise ProxboxException(
            message="Error while creating sync process.", python_exception=str(error)
        )

    try:
        # Sync Nodes
        sync_nodes = await create_proxmox_devices(
            netbox_session=netbox_session,
            clusters_status=cluster_status,
            node=None,
            tag=tag,
            use_websocket=False,
        )
    except Exception as error:
        print(error)
        raise ProxboxException(message="Error while syncing nodes.", python_exception=str(error))

    if sync_nodes:
        # Sync Virtual Machines
        try:
            sync_vms = await create_virtual_machines(
                pxs=pxs,
                cluster_status=cluster_status,
                cluster_resources=cluster_resources,
                custom_fields=custom_fields,
                tag=tag,
                use_websocket=False,
            )
        except Exception as error:
            print(error)
            raise ProxboxException(
                message="Error while syncing virtual machines.",
                python_exception=str(error),
            )

    if sync_process:
        end_time = datetime.now()
        sync_process.status = "completed"
        sync_process.completed_at = str(end_time)
        sync_process.runtime = float((end_time - start_time).total_seconds())
        sync_process.save()

        print(sync_process)
        print(sync_process.runtime)
    return sync_nodes, sync_vms


@app.websocket("/ws")
async def websocket_sync_commands(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
):
    connection_open = False

    nb = netbox_session

    print("route ws reached")
    try:
        await websocket.accept()
        connection_open = True

        await websocket.send_text("Connected!")
    except Exception as error:
        print(f"Error while accepting WebSocket connection: {error}")
        try:
            await websocket.close()
        except Exception as error:
            print(f"Error while closing WebSocket connection: {error}")

    # 'data' is the message received from the WebSocket.
    data = None

    await websocket.send_text("Connected 2!")

    try:
        while True:
            try:
                data = await websocket.receive_text()
                print(f"Received message: {data}")
                await websocket.send_text(f"Received message: {data}")
            except Exception as error:
                print(f"Error while receiving data from WebSocket: {error}")
                break

            if data == "Full Update Sync":
                sync_process = None

                try:
                    sync_process = nb.plugins.proxbox.__getattr__("sync-processes").create(
                        name=f"sync-process-{datetime.now()}",
                        sync_type="all",
                        status="not-started",
                        started_at=str(datetime.now()),
                    )
                except Exception as error:
                    print(error)
                    pass

                # Sync Nodes
                sync_nodes = await create_proxmox_devices(
                    netbox_session=nb,
                    clusters_status=cluster_status,
                    node=None,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True,
                )

                if sync_nodes:
                    # Sync Virtual Machines
                    await create_virtual_machines(
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        custom_fields=custom_fields,
                        websocket=websocket,
                        tag=tag,
                        use_websocket=True,
                    )

                if sync_process:
                    sync_process.status = "completed"
                    sync_process.completed_at = str(datetime.now())
                    sync_process.save()

            if data == "Sync Nodes":
                print("Sync Nodes")
                await websocket.send_text("Sync Nodes")
                await create_proxmox_devices(
                    netbox_session=nb,
                    clusters_status=cluster_status,
                    node=None,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True,
                )

            elif data == "Sync Virtual Machines":
                await create_virtual_machines(
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    custom_fields=custom_fields,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True,
                )

            else:
                await websocket.send_text(f"Invalid command: {data}")
                await websocket.send_text(
                    "Valid commands: 'Sync Nodes', 'Sync Virtual Machines', 'Full Update Sync'"
                )
                # await websocket.send_denial_response("Invalid command.")

    except WebSocketDisconnect as error:
        print(f"WebSocket Disconnected: {error}")
        connection_open = False
    finally:
        if connection_open and websocket.client_state.CONNECTED:
            await websocket.close(code=1000, reason=None)


def get_raw_netbox_session():
    """Helper function to get a NetBox session with the same interface as RawNetBoxSession"""
    try:
        database_session = next(get_session())
        return get_netbox_session(database_session)
    except Exception as error:
        print(f"Error getting NetBox session: {error}")
        return None
