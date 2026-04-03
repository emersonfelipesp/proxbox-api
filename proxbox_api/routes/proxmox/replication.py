"""Proxmox cluster replication endpoints and response schemas."""

from fastapi import APIRouter
from pydantic import BaseModel

from proxbox_api.logger import logger
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


class ReplicationJobSchema(BaseModel):
    cluster_name: str | None = None
    status: str = "ok"
    error: str | None = None
    comment: str | None = None
    disable: bool | None = None
    guest: int | None = None
    id: str | None = None
    jobnum: int | None = None
    rate: float | None = None
    remove_job: str | None = None
    schedule: str | None = None
    source: str | None = None
    target: str | None = None
    type: str | None = None


ReplicationJobSchemaList = list[ReplicationJobSchema]


@router.get("/replication", response_model=ReplicationJobSchemaList)
async def cluster_replication(pxs: ProxmoxSessionsDep):
    """
    ### Retrieve the replication jobs from multiple Proxmox sessions.

    **Args:**
    - **pxs (`ProxmoxSessionsDep`):** A list of Proxmox session dependencies.

    **Returns:**
    - **list:** A list of dictionaries containing the replication jobs from each cluster.

    ### Example Response:
    ```json
    [
        {
            "comment": "My replication job",
            "disable": false,
            "guest": 100,
            "id": "100-1",
            "jobnum": 1,
            "rate": 10.5,
            "remove_job": null,
            "schedule": "*/15",
            "source": "proxmox-node-1",
            "target": "proxmox-node-2",
            "type": "local"
        }
    ]
    ```
    """
    results = []

    for px in pxs:
        try:
            replications = px.session.cluster.replication.get()
        except Exception as error:
            logger.exception("Error fetching replication jobs for Proxmox cluster %s", px.name)
            results.append(
                ReplicationJobSchema(
                    cluster_name=px.name,
                    status="error",
                    error=str(error),
                )
            )
            continue

        try:
            for rep in replications:
                results.append(
                    ReplicationJobSchema(
                        cluster_name=px.name,
                        status="ok",
                        comment=rep.get("comment"),
                        disable=rep.get("disable"),
                        guest=rep.get("guest"),
                        id=rep.get("id"),
                        jobnum=rep.get("jobnum"),
                        rate=rep.get("rate"),
                        remove_job=rep.get("remove_job"),
                        schedule=rep.get("schedule"),
                        source=rep.get("source"),
                        target=rep.get("target"),
                        type=rep.get("type"),
                    )
                )
        except Exception as error:
            logger.exception("Unexpected replication payload for Proxmox cluster %s", px.name)
            results.append(
                ReplicationJobSchema(
                    cluster_name=px.name,
                    status="error",
                    error=str(error),
                )
            )

    return results
