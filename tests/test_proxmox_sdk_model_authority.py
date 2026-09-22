"""Contracts for the single proxmox-sdk VM-config model authority."""

from proxbox_api.services import proxmox_helpers


def test_vm_config_models_come_from_pinned_proxmox_sdk() -> None:
    module = proxmox_helpers.generated_models

    assert module.__name__ == "proxmox_sdk.generated.proxmox.latest.pydantic_models"
    assert (
        module.GetNodesNodeQemuVmidConfigResponse.model_validate(
            {"digest": "test", "agent": 1, "memory": 4096}
        ).memory
        == 4096
    )
