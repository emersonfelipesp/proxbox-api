"""In-memory Firecracker host-agent app for development and contract tests."""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, HTTPException, status

from proxbox_api.schemas.firecracker import (
    FirecrackerAssetPrepareRequest,
    FirecrackerAssetPrepareResponse,
    FirecrackerHostAgentHealth,
    FirecrackerHostCapabilities,
    FirecrackerMicroVMAction,
    FirecrackerMicroVMCreateRequest,
    FirecrackerMicroVMMetrics,
    FirecrackerMicroVMState,
)

_MICROVMS: dict[UUID, FirecrackerMicroVMState] = {}
_LOGS: dict[UUID, list[str]] = {}


def create_firecracker_agent_app() -> FastAPI:  # noqa: C901
    """Create a local host-agent app with the production HTTP contract."""
    app = FastAPI(title="Firecracker Host Agent", version="0.1.0")

    @app.get("/health", response_model=FirecrackerHostAgentHealth)
    async def health() -> FirecrackerHostAgentHealth:
        return FirecrackerHostAgentHealth()

    @app.get("/capabilities", response_model=FirecrackerHostCapabilities)
    async def capabilities() -> FirecrackerHostCapabilities:
        allocated_vcpus = sum(vm.vcpus for vm in _MICROVMS.values())
        allocated_memory = sum(vm.memory_mib for vm in _MICROVMS.values())
        allocated_disk = sum(vm.disk_mib for vm in _MICROVMS.values())
        max_vcpus = 32
        max_memory = 65536
        max_disk = 524288
        return FirecrackerHostCapabilities(
            supports_nat=True,
            supports_bridge=True,
            max_vcpus=max_vcpus,
            max_memory_mib=max_memory,
            max_disk_mib=max_disk,
            available_vcpus=max(max_vcpus - allocated_vcpus, 0),
            available_memory_mib=max(max_memory - allocated_memory, 0),
            available_disk_mib=max(max_disk - allocated_disk, 0),
        )

    @app.get("/assets")
    async def assets() -> dict[str, list[str]]:
        return {"images": []}

    @app.post("/assets/prepare", response_model=FirecrackerAssetPrepareResponse)
    async def prepare_assets(
        request: FirecrackerAssetPrepareRequest,
    ) -> FirecrackerAssetPrepareResponse:
        image_slug = request.image.name.lower().replace(" ", "-")
        return FirecrackerAssetPrepareResponse(
            kernel_image_path=f"/var/lib/firecracker/images/{image_slug}/vmlinux",
            rootfs_image_path=f"/var/lib/firecracker/images/{image_slug}/rootfs.ext4",
        )

    @app.get("/microvms", response_model=list[FirecrackerMicroVMState])
    async def list_microvms() -> list[FirecrackerMicroVMState]:
        return list(_MICROVMS.values())

    @app.post("/microvms", response_model=FirecrackerMicroVMState)
    async def create_microvm(
        request: FirecrackerMicroVMCreateRequest,
    ) -> FirecrackerMicroVMState:
        state = FirecrackerMicroVMState(
            microvm_id=request.microvm_id,
            name=request.name,
            status="created",
            network_mode=request.network.mode,
            guest_ip=request.network.guest_ip,
            vcpus=request.vcpus,
            memory_mib=request.memory_mib,
            disk_mib=request.disk_mib,
        )
        _MICROVMS[state.microvm_id] = state
        _LOGS[state.microvm_id] = [f"created {state.name}"]
        return state

    @app.get("/microvms/{microvm_id}", response_model=FirecrackerMicroVMState)
    async def get_microvm(microvm_id: UUID) -> FirecrackerMicroVMState:
        return _get_state(microvm_id)

    @app.post(
        "/microvms/{microvm_id}/actions/{action}",
        response_model=FirecrackerMicroVMState,
    )
    async def microvm_action(
        microvm_id: UUID,
        action: FirecrackerMicroVMAction,
    ) -> FirecrackerMicroVMState:
        state = _get_state(microvm_id)
        status_by_action = {
            FirecrackerMicroVMAction.start: "running",
            FirecrackerMicroVMAction.stop: "stopped",
            FirecrackerMicroVMAction.pause: "paused",
            FirecrackerMicroVMAction.resume: "running",
            FirecrackerMicroVMAction.reboot: "running",
            FirecrackerMicroVMAction.delete: "deleted",
        }
        updated = state.model_copy(update={"status": status_by_action[action]})
        _MICROVMS[microvm_id] = updated
        _LOGS.setdefault(microvm_id, []).append(f"{action.value} -> {updated.status}")
        return updated

    @app.get("/microvms/{microvm_id}/metrics", response_model=FirecrackerMicroVMMetrics)
    async def metrics(microvm_id: UUID) -> FirecrackerMicroVMMetrics:
        _get_state(microvm_id)
        return FirecrackerMicroVMMetrics(microvm_id=microvm_id)

    @app.get("/microvms/{microvm_id}/logs")
    async def logs(microvm_id: UUID, tail: int = 200) -> dict[str, list[str]]:
        _get_state(microvm_id)
        return {"lines": _LOGS.get(microvm_id, [])[-tail:]}

    return app


def _get_state(microvm_id: UUID) -> FirecrackerMicroVMState:
    try:
        return _MICROVMS[microvm_id]
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="micro-VM not found",
        ) from error
