"""Proxmox cluster replication endpoints and response schemas."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


class ReplicationJobSchema(BaseModel):
    comment: str | None = None
    disable: bool | None = None
    guest: int
    id: str
    jobnum: int
    rate: float | None = None
    remove_job: str | None = None
    schedule: str | None = None
    source: str | None = None
    target: str
    type: str


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
            for rep in replications:
                results.append(
                    ReplicationJobSchema(
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
        except Exception as e:
            continue

    return results
