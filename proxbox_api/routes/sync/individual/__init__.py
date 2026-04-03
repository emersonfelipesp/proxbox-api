"""Individual sync route handlers."""

from fastapi import APIRouter

from proxbox_api.routes.sync.individual import (
    backup,
    cluster,
    device,
    disk,
    interface,
    ip,
    replication,
    snapshot,
    storage,
    task_history,
    vm,
)

router = APIRouter()

router.include_router(backup.router, tags=["sync / individual / backup"])
router.include_router(cluster.router, tags=["sync / individual / cluster"])
router.include_router(device.router, tags=["sync / individual / device"])
router.include_router(vm.router, tags=["sync / individual / vm"])
router.include_router(interface.router, tags=["sync / individual / interface"])
router.include_router(ip.router, tags=["sync / individual / ip"])
router.include_router(disk.router, tags=["sync / individual / disk"])
router.include_router(storage.router, tags=["sync / individual / storage"])
router.include_router(snapshot.router, tags=["sync / individual / snapshot"])
router.include_router(task_history.router, tags=["sync / individual / task-history"])
router.include_router(replication.router, tags=["sync / individual / replication"])
