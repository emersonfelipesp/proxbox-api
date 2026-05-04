"""Proxmox route handlers for sessions, storage, and VM config."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request
from proxmox_sdk.sdk.exceptions import ResourceException
from pydantic import BaseModel, Field, field_validator

from proxbox_api.enum.proxmox import *
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep
from proxbox_api.schemas._coerce import normalize_bool
from proxbox_api.schemas.proxmox import *
from proxbox_api.schemas.virtualization import VMConfig
from proxbox_api.services.proxmox.config import resolve_vm_config
from proxbox_api.services.proxmox_helpers import (
    dump_models,
    get_storage_list,
)
from proxbox_api.services.proxmox_helpers import (
    get_node_storage_content as get_typed_node_storage_content,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep, close_proxmox_sessions

router = APIRouter()

from proxbox_api.routes.proxmox.endpoints import router as endpoints_router
from proxbox_api.routes.proxmox.viewer_codegen import router as viewer_codegen_router

router.include_router(viewer_codegen_router, prefix="/viewer", tags=["proxmox / viewer"])
router.include_router(endpoints_router, tags=["proxmox / endpoints"])

#
# /proxmox/* API Endpoints
#


@router.get("/sessions")
async def proxmox_sessions(pxs: ProxmoxSessionsDep, request: Request):
    """
    ### Asynchronously retrieves Proxmox session details and returns them as a JSON response.

    **Args:**
    - **pxs (`ProxmoxSessionsDep`):** A dependency injection of Proxmox sessions.
    - **request (Request):** FastAPI request used to trigger schema auto-generation.

    **Returns:**
    - **list:** A list of dictionaries containing Proxmox session details, each with the following keys:
        - **domain (str):** The domain of the Proxmox session.
        - **http_port (int):** The HTTP port of the Proxmox session.
        - **user (str):** The user associated with the Proxmox session.
        - **name (str):** The name of the Proxmox session.
        - **mode (str):** The mode of the Proxmox session.
        - **proxmox_version:** The version info reported by the connected Proxmox host.
        - **schema_status (dict):** Schema availability and any background generation status.
    """
    from proxbox_api.schema_version_manager import ensure_schema_for_version, extract_release_tag

    json_response = []

    for px in pxs:
        version_info = getattr(px, "version", None)
        schema_status: dict | None = None
        if version_info is not None:
            try:
                schema_status = await ensure_schema_for_version(request.app, version_info)
            except Exception as schema_err:
                logger.debug("Schema version check skipped: %s", schema_err)

        json_response.append(
            {
                "ip_address": getattr(px, "ip_address", None),
                "domain": getattr(px, "domain", None),
                "http_port": getattr(px, "http_port", None),
                "user": getattr(px, "user", None),
                "name": getattr(px, "name", None),
                "mode": getattr(px, "mode", None),
                "proxmox_version": version_info,
                "schema_release": extract_release_tag(version_info),
                "schema_status": schema_status,
            }
        )

    return json_response


@router.get(
    "/version",
)
async def proxmox_version(pxs: ProxmoxSessionsDep):
    """
    ### Retrieve the version information from multiple Proxmox sessions.

    *Args:**
        **pxs (`ProxmoxSessionsDep`):** A dependency injection of Proxmox sessions.

    **Returns:**
        **list:** A list of dictionaries containing the name and version of each Proxmox session.
    """
    json_response = []

    try:
        for px in pxs:
            if not getattr(px, "CONNECTED", False):
                continue

            session = getattr(px, "session", None)
            if session is None:
                raise ProxboxException(
                    message="Invalid Proxmox session state",
                    detail="Connected session is missing SDK client instance.",
                )

            try:
                version = await resolve_async(session.version.get())
                json_response.append({getattr(px, "name", None): version})
            except ResourceException as error:
                target = getattr(px, "domain", None) or getattr(px, "ip_address", None)
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Failed to query Proxmox version "
                        f"for endpoint '{getattr(px, 'name', 'unknown')}' ({target}). "
                        f"Upstream responded with HTTP {error.status_code} {error.status_message}."
                    ),
                ) from error
            except Exception as error:
                raise ProxboxException(
                    message="Error retrieving Proxmox version",
                    detail="Unexpected error while querying upstream Proxmox API.",
                    python_exception=str(error),
                ) from error

        if not json_response:
            raise HTTPException(
                status_code=404,
                detail="No Proxmox active connections found, not able to retrieve version information",
            )

        return json_response
    finally:
        await close_proxmox_sessions(pxs)


@router.get("/")
async def proxmox(pxs: ProxmoxSessionsDep):
    """
    #### Fetches and compiles data from multiple Proxmox sessions.

    **Args:**
    - **pxs (ProxmoxSessionsDep):** A dependency injection of Proxmox sessions.

    **Returns:**
    - **dict:** A dictionary containing:
        - **message (`str`):** A static message "Proxmox API".
        - **proxmox_api_viewer (`str`):** URL to the Proxmox API viewer.
        - **github (`dict`):** URLs to relevant GitHub repositories.
        - **clusters (`list`):** A list of dictionaries, each representing a Proxmox session with keys:
            - **ccess (`list`):** Minimized result of the "access" endpoint.
            - **cluster (`list`):** Minimized result of the "cluster" endpoint.
            - **nodes (`list`):** Result of the "nodes" endpoint.
            - **pools (`list`):** Result of the "pools" endpoint.
            - **storage (`list`):** Result of the "storage" endpoint.
            - **version (`dict`):** Result of the "version" endpoint.
    """

    json_response = []

    async def minimize_result(endpoint_name):
        """
        Minimize the result obtained from a Proxmox endpoint.
        This function retrieves data from a specified Proxmox endpoint and extracts
        specific fields based on the endpoint name. The extracted fields are then
        returned as a list.

        **Args:**
        - **endpoint_name (`str`):** The name of the Proxmox endpoint to query.
        Supported values are "access" and "cluster".

        **Returns:**
        - **list:** A list of extracted fields from the Proxmox endpoint. For the
            - "access" endpoint, it returns a list of "subdir" values. For the
            - "cluster" endpoint, it returns a list of "name" values.
        """

        endpoint_list = []
        result = await resolve_async(px.session(endpoint_name).get())

        match endpoint_name:
            case "access":
                for obj in result:
                    endpoint_list.append(obj.get("subdir"))

            case "cluster":
                for obj in result:
                    endpoint_list.append(obj.get("name"))

        return endpoint_list

    try:
        for px in pxs:
            nodes = await resolve_async(px.session.nodes.get())
            pools = await resolve_async(px.session.pools.get())
            storage = await resolve_async(px.session.storage.get())
            version = await resolve_async(px.session.version.get())
            json_response.append(
                {
                    f"{px.name}": {
                        "access": await minimize_result("access"),
                        "cluster": await minimize_result("cluster"),
                        "nodes": nodes,
                        "pools": pools,
                        "storage": storage,
                        "version": version,
                    }
                }
            )

        return {
            "message": "Proxmox API",
            "proxmox_api_viewer": "https://pve.proxmox.com/pve-docs/api-viewer/",
            "github": {
                "netbox": "https://github.com/netbox-community/netbox",
                "netbox-sdk": "https://github.com/netbox-community/netbox-sdk",
                "proxmox-sdk": "https://github.com/emersonfelipesp/proxmox-sdk",
                "proxbox": "https://github.com/netdevopsbr/netbox-proxbox",
            },
            "clusters": json_response,
        }
    finally:
        await close_proxmox_sessions(pxs)


class BackupVerification(BaseModel):
    upid: str
    state: str


class ProxmoxStorageContent(BaseModel):
    subtype: str | None = None
    format: str | None = None  # Format identifier ('raw', 'qcow2', 'subvol', 'iso', 'tgz' ...)
    size: int | None = None  # Volume size in bytes.
    ctime: int | None = None  # Creation time (seconds since the UNIX Epoch)
    notes: str | None = (
        None  # Optional Notes. If they contain multiple lines, only the first one is returned here.
    )
    content: str | None = None
    volid: str | None = None
    vmid: int | None = None
    used: int | None = (
        None  # Used space. Please note that most storage plugins do not report anything useful here.
    )
    encrypted: str | None = None
    verification: BackupVerification | None = None


ProxmoxStorageContentList = list[ProxmoxStorageContent]


class ProxmoxStorage(BaseModel):
    type: str | None = None
    storage: str | None = None
    path: str | None = None
    content: str | None = None
    digest: str | None = None
    nodes: str | None = None
    prune_backups: str | None = Field(None, alias="prune-backups")
    shared: bool | None = None
    export: str | None = None
    server: str | None = None
    disable: bool | None = None
    pool: str | None = None
    sparse: bool | None = None
    username: str | None = None
    datastore: str | None = None
    fingerprint: str | None = None
    mountpoint: str | None = None

    @field_validator("shared", "disable", "sparse", mode="before")
    @classmethod
    def _coerce_bool(cls, value: object) -> bool | None:
        return normalize_bool(value)


ProxmoxStorageList = list[ProxmoxStorage]
ClusterProxmoxStorage = list[dict[str, ProxmoxStorageList]]


@router.get("/storage", response_model=ClusterProxmoxStorage)
async def get_proxmox_storage(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
):
    logger.debug("Fetching storage inventory for %s Proxmox sessions", len(pxs))
    """
    ### Retrieve the storage information from multiple Proxmox sessions.

    """
    result = []
    for proxmox in pxs:
        result.append({proxmox.name: dump_models(await get_storage_list(proxmox))})

    return result


@router.get("/nodes/{node}/storage/{storage}/content", response_model=ProxmoxStorageContentList)
async def get_proxmox_node_storage_content(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: Annotated[
        str,
        Path(
            title="Node",
            description="The name of the node to retrieve the storage content for.",
        ),
    ],
    storage: Annotated[
        str,
        Path(
            title="Storage",
            description="The name of the storage to retrieve the content for.",
        ),
    ],
    vmid: Annotated[
        str,
        Query(title="VM ID", description="The ID of the VM to retrieve the content for."),
    ] = None,
    content: Annotated[
        str,
        Query(
            title="Content",
            description="The type of content to retrieve. Example: 'backup'",
        ),
    ] = None,
):
    """
    ### Retrieve the content of a specific storage volume.

    **Args:**
    - **pxs (ProxmoxSessionsDep):** A dependency injection of Proxmox sessions.
    - **cluster_status (ClusterStatusDep):** A dependency injection of cluster status.
    - **node (str):** The name of the node to retrieve the content for.
    - **storage (str):** The name of the storage to retrieve the content for.
    - **vmid (str):** The ID of the VM to retrieve the content for.
    - **content (str):** The type of content to retrieve. Example: 'backup'

    **Returns:**
    - **list:** A list of dictionaries (JSON)containing the content of the storage volume.
    """

    for proxmox, cluster in zip(pxs, cluster_status):
        for cluster_node in cluster.node_list:
            if cluster_node.name == node:
                return dump_models(
                    await get_typed_node_storage_content(
                        proxmox,
                        node=node,
                        storage=storage,
                        vmid=vmid,
                        content=content,
                    )
                )

    raise HTTPException(status_code=404, detail="Node or Storage not found")


@router.get("/{top_level}")
async def top_level_endpoint(
    pxs: ProxmoxSessionsDep,
    top_level: ProxmoxUpperPaths,
):
    """
    ### Asynchronously retrieves data from multiple Proxmox sessions for a given top-level path.

    **Args:**
    - **pxs (`ProxmoxSessionsDep`):** A dependency injection of Proxmox sessions.
    - **top_level (`ProxmoxUpperPaths`):** The top-level path to query in each Proxmox session.

    **Returns:**
    - **list:** A list of dictionaries containing the session name as the key and the response data as the value.
    """

    json_response = []

    try:
        for px in pxs:
            json_response.append({px.name: await resolve_async(px.session(top_level).get())})

        return json_response
    finally:
        await close_proxmox_sessions(pxs)


@router.get(
    "/{node}/{type}/{vmid}/config",
    response_model=VMConfig,
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def get_vm_config(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    name: str = Query(title="Cluster", description="Proxmox Cluster Name", default=None),
    node: str = Path(..., title="Node", description="Proxmox Node Name"),
    type: str = Path(..., title="Type", description="Proxmox VM Type"),
    vmid: int = Path(..., title="VM ID", description="Proxmox VM ID"),
):
    """Return the VM config by matching node across all Proxmox clusters."""
    return await resolve_vm_config(
        pxs=pxs,
        cluster_status=cluster_status,
        node=node,
        vm_type=type,
        vmid=vmid,
    )
