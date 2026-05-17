"""Workdir lifecycle helpers for image factory Packer runs."""

from __future__ import annotations

import shutil
from pathlib import Path

from proxbox_api.runtime_settings import get_str


def get_packer_workdir_root() -> Path:
    return Path(
        get_str(
            settings_key="packer_workdir",
            env="PROXBOX_PACKER_WORKDIR",
            default="/var/lib/proxbox/packer/builds",
        )
    )


def create_workdir(build_id: str) -> Path:
    workdir = get_packer_workdir_root() / build_id
    workdir.mkdir(parents=True, exist_ok=False)
    return workdir


def cleanup_workdir(path: str | Path, *, keep_workdir: bool = False) -> None:
    if keep_workdir:
        return
    try:
        shutil.rmtree(Path(path), ignore_errors=True)
    except Exception:
        return
