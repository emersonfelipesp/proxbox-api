"""Virtual machine read/query routes."""

# FastAPI Imports

from fastapi import APIRouter

# NetBox compatibility wrappers
from proxbox_api.netbox_compat import (
    VirtualMachine,
)
from proxbox_api.schemas.virtualization import (  # Schemas
    CPU,
    Backup,
    Disk,
    Memory,
    Network,
    Snapshot,
    VirtualMachineSummary,
)

router = APIRouter()

@router.get(
    "/",
    response_model=list[dict],
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def get_virtual_machines():
    virtual_machine = VirtualMachine()
    return virtual_machine.all()


@router.get(
    "/{id}",
    response_model=dict,
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def get_virtual_machine(id: int):
    try:
        virtual_machine = VirtualMachine().find(id=id)
        if virtual_machine:
            return virtual_machine
        else:
            return {}
    except Exception as error:
        print(f"Error getting virtual machine: {error}")
        return {}


@router.get(
    "/summary/example",
    response_model=VirtualMachineSummary,
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def get_virtual_machine_summary_example():

    # Example usage
    vm_summary = VirtualMachineSummary(
        id="vm-102",
        name="db-server-01",
        status="running",
        node="pve-node-02",
        cluster="Production Cluster",
        os="CentOS 8",
        description="Primary database server for production applications",
        uptime="43 days, 7 hours, 12 minutes",
        created="2023-01-15",
        cpu=CPU(cores=8, sockets=1, type="host", usage=32),
        memory=Memory(total=16384, used=10240, usage=62),
        disks=[
            Disk(
                id="scsi0",
                storage="local-lvm",
                size=102400,
                used=67584,
                usage=66,
                format="raw",
                path="/dev/pve/vm-102-disk-0",
            ),
            Disk(
                id="scsi1",
                storage="local-lvm",
                size=409600,
                used=215040,
                usage=52,
                format="raw",
                path="/dev/pve/vm-102-disk-1",
            ),
        ],
        networks=[
            Network(
                id="net0",
                model="virtio",
                bridge="vmbr0",
                mac="AA:BB:CC:DD:EE:FF",
                ip="10.0.0.102",
                netmask="255.255.255.0",
                gateway="10.0.0.1",
            ),
            Network(
                id="net1",
                model="virtio",
                bridge="vmbr1",
                mac="AA:BB:CC:DD:EE:00",
                ip="192.168.1.102",
                netmask="255.255.255.0",
                gateway="192.168.1.1",
            ),
        ],
        snapshots=[
            Snapshot(
                id="snap1",
                name="pre-update",
                created="2023-05-10 14:30:00",
                description="Before system update",
            ),
            Snapshot(
                id="snap2",
                name="db-config-change",
                created="2023-06-15 09:45:00",
                description="After database configuration change",
            ),
            Snapshot(
                id="snap3",
                name="monthly-backup",
                created="2023-07-01 00:00:00",
                description="Monthly automated snapshot",
            ),
        ],
        backups=[
            Backup(
                id="backup1",
                storage="backup-nfs",
                created="2023-07-01 01:00:00",
                size=75840,
                status="successful",
            ),
            Backup(
                id="backup2",
                storage="backup-nfs",
                created="2023-06-01 01:00:00",
                size=72560,
                status="successful",
            ),
            Backup(
                id="backup3",
                storage="backup-nfs",
                created="2023-05-01 01:00:00",
                size=70240,
                status="successful",
            ),
        ],
    )

    return vm_summary


@router.get(
    "/{id}/summary",
)
async def get_virtual_machine_summary(id: int):
    pass


@router.get("/interfaces/create")
async def create_virtual_machines_interfaces():
    # TODO
    pass


@router.get("/interfaces/ip-address/create")
async def create_virtual_machines_interfaces_ip_address():
    # TODO
    pass
