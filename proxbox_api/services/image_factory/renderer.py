"""Render static Packer templates into an isolated build workdir."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from proxbox_api.schemas.image_factory import PackerImageBuildRequest

HCL_DECLARED_VARIABLES = {
    "proxmox_url",
    "node",
    "clone_vm_id",
    "vm_id",
    "vm_name",
    "template_name",
    "vm_storage",
    "bridge",
    "memory",
    "cores",
    "cpu_type",
    "cloud_init_storage_pool",
}

PKRVARS_WRITABLE_VARIABLES = HCL_DECLARED_VARIABLES - {"proxmox_url"}

ALLOWED_PROVISIONER_RECIPES = {
    "ubuntu-base": "ubuntu-base.sh",
    "debian-base": "debian-base.sh",
    "docker-host": "docker-host.sh",
    "qemu-agent": "qemu-agent.sh",
}


@dataclass(frozen=True)
class RenderedPackerWorkdir:
    workdir: Path
    template_path: Path
    var_file: Path
    variables: dict[str, str | int | bool]
    provisioner_path: Path


def _template_root() -> Path:
    return Path(str(resources.files("proxbox_api") / "packer_templates"))


def _copy_static_assets(workdir: Path, recipe: str) -> Path:
    recipe_file = ALLOWED_PROVISIONER_RECIPES.get(recipe)
    if recipe_file is None:
        allowed = ", ".join(sorted(ALLOWED_PROVISIONER_RECIPES))
        raise ValueError(f"provisioner_recipe must be one of: {allowed}")

    template_root = _template_root()
    template_src = template_root / "proxmox" / "clone-cloud-init.pkr.hcl"
    template_dst = workdir / "clone-cloud-init.pkr.hcl"
    shutil.copy2(template_src, template_dst)

    provisioner_dir = workdir / "provisioners"
    provisioner_dir.mkdir(parents=True, exist_ok=True)
    selected_dst = provisioner_dir / "selected-recipe.sh"
    shutil.copy2(template_root / "provisioners" / recipe_file, selected_dst)
    return selected_dst


def build_packer_variables(
    request: PackerImageBuildRequest,
) -> dict[str, str | int | bool]:
    unknown = sorted(set(request.variables) - PKRVARS_WRITABLE_VARIABLES)
    if unknown:
        raise ValueError(f"Unsupported Packer variable(s): {', '.join(unknown)}")

    variables: dict[str, str | int | bool] = {
        "node": request.target_node,
        "clone_vm_id": request.template_vmid,
        "vm_id": request.output_vmid,
        "vm_name": request.output_name,
        "template_name": request.output_name,
        "vm_storage": request.vm_storage,
        "bridge": request.bridge,
        "memory": request.memory_mb,
        "cores": request.cores,
        "cpu_type": request.cpu_type,
        "cloud_init_storage_pool": request.cloud_init_storage or request.vm_storage,
    }
    for key, value in request.variables.items():
        if key in variables:
            variables[key] = value
    return variables


def render_packer_workdir(
    *,
    request: PackerImageBuildRequest,
    workdir: Path,
) -> RenderedPackerWorkdir:
    provisioner_path = _copy_static_assets(workdir, request.provisioner_recipe)
    variables = build_packer_variables(request)
    var_file = workdir / "build.auto.pkrvars.json"
    var_file.write_text(json.dumps(variables, indent=2, sort_keys=True) + "\n")
    return RenderedPackerWorkdir(
        workdir=workdir,
        template_path=workdir / "clone-cloud-init.pkr.hcl",
        var_file=var_file,
        variables=variables,
        provisioner_path=provisioner_path,
    )


def load_pkrvars(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())
