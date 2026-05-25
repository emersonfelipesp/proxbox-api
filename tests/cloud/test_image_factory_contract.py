"""Contract tests for the Packer image factory cloud routes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes.cloud import image_factory
from proxbox_api.schemas.image_factory import (
    ImageFactoryBuildMode,
    PackerImageBuildRequest,
    PackerImageBuildResponse,
)
from proxbox_api.services.image_factory.logs import PackerEvent
from proxbox_api.services.image_factory.runner import CommandResult

TOKEN_MARKER = "TEST-TOKEN-MUST-NOT-LEAK"


class FakePackerRunner:
    instances: list["FakePackerRunner"] = []

    def __init__(self, *, env: dict[str, str], secrets: tuple[str, ...]) -> None:
        self.env = env
        self.secrets = secrets
        self.build_called = False
        self.cancelled = False
        FakePackerRunner.instances.append(self)

    async def init(self, workdir: Path) -> CommandResult:
        return CommandResult(command=("packer", "init", "."), exit_code=0)

    async def validate(self, workdir: Path, var_file: Path) -> CommandResult:
        return CommandResult(
            command=("packer", "validate", "-machine-readable", f"-var-file={var_file.name}", "."),
            exit_code=0,
            stdout=["1700000000,,ui,say,Template validated"],
        )

    async def build(self, workdir: Path, var_file: Path):
        self.build_called = True
        yield PackerEvent(
            name="packer_log",
            data={"phase": "build", "message": f"building with token={TOKEN_MARKER}"},
        )
        yield PackerEvent(
            name="packer_artifact",
            data={"template_name": "ubuntu-2404-golden", "token": TOKEN_MARKER},
        )

    async def cancel(self, build_id: str) -> None:
        self.cancelled = True


def _make_endpoint(db_session: Session, *, allow_writes: bool = True) -> int:
    endpoint = ProxmoxEndpoint(
        name=f"pve-image-factory-{allow_writes}-{len(FakePackerRunner.instances)}",
        ip_address="10.0.0.10",
        port=8006,
        username="root@pam",
        verify_ssl=False,
        allow_writes=allow_writes,
        token_name="packer",
        token_value=TOKEN_MARKER,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    assert endpoint.id is not None
    return endpoint.id


def _request_payload(endpoint_id: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "endpoint_id": endpoint_id,
        "target_node": "pve",
        "builder_type": "proxmox-clone",
        "template_vmid": 9000,
        "output_vmid": 9100,
        "output_name": "ubuntu-2404-golden",
        "os_family": "ubuntu",
        "os_release": "24.04",
        "image_version": "2026.05.17",
        "vm_storage": "local-lvm",
        "cloud_init_storage": "local-lvm",
        "bridge": "vmbr0",
        "memory_mb": 2048,
        "cores": 2,
        "cpu_type": "host",
        "provisioner_recipe": "ubuntu-base",
        "variables": {"memory": 2048},
        "force": False,
        "dry_run": False,
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def image_factory_fakes(monkeypatch, tmp_path):
    FakePackerRunner.instances.clear()
    monkeypatch.setenv("PROXBOX_PACKER_WORKDIR", str(tmp_path / "packer-builds"))
    monkeypatch.setattr(
        image_factory,
        "packer_runner_factory",
        lambda *, env, secrets: FakePackerRunner(env=env, secrets=secrets),
    )

    async def template_exists(endpoint, request):
        return None

    monkeypatch.setattr(image_factory, "_ensure_template_vmid_exists", template_exists)


def _events_from_sse(body: str) -> list[str]:
    events: list[str] = []
    for frame in body.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))
    return events


def _assert_subsequence(events: list[str], expected: list[str]) -> None:
    cursor = 0
    for event in events:
        if cursor < len(expected) and event == expected[cursor]:
            cursor += 1
    assert cursor == len(expected), events


async def test_image_factory_schema_shapes() -> None:
    request = PackerImageBuildRequest(**_request_payload(1))
    response = PackerImageBuildResponse(
        build_id="build-1",
        status="queued",
        endpoint_id=request.endpoint_id,
        target_node=request.target_node,
        output_vmid=request.output_vmid,
        output_name=request.output_name,
    )

    assert ImageFactoryBuildMode.packer_clone.value == "packer-clone"
    assert request.builder_type == "proxmox-clone"
    assert response.model_dump()["status"] == "queued"


async def test_create_build_returns_queued_response(authenticated_client, db_session):
    endpoint_id = _make_endpoint(db_session)

    response = await authenticated_client.post(
        "/cloud/image-factory/builds",
        json=_request_payload(endpoint_id),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["endpoint_id"] == endpoint_id
    assert body["log_url"] == f"/cloud/image-factory/builds/{body['build_id']}/stream"


async def test_invalid_endpoint_returns_404(authenticated_client):
    response = await authenticated_client.post(
        "/cloud/image-factory/builds",
        json=_request_payload(999999),
    )

    assert response.status_code == 404
    assert response.json()["reason"] == "endpoint_not_found"


async def test_missing_template_vmid_returns_422(authenticated_client, db_session, monkeypatch):
    endpoint_id = _make_endpoint(db_session)

    async def missing_template(endpoint, request):
        raise HTTPException(
            status_code=422,
            detail="template_vmid missing",
        )

    monkeypatch.setattr(image_factory, "_ensure_template_vmid_exists", missing_template)
    response = await authenticated_client.post(
        "/cloud/image-factory/builds",
        json=_request_payload(endpoint_id),
    )

    assert response.status_code == 422
    assert "template_vmid" in response.text


async def test_allow_writes_false_returns_gate_reason(authenticated_client, db_session):
    endpoint_id = _make_endpoint(db_session, allow_writes=False)

    response = await authenticated_client.post(
        "/cloud/image-factory/builds",
        json=_request_payload(endpoint_id),
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "endpoint_writes_disabled"


async def test_dry_run_does_not_invoke_build(authenticated_client, db_session):
    endpoint_id = _make_endpoint(db_session)

    response = await authenticated_client.post(
        "/cloud/image-factory/builds",
        json=_request_payload(endpoint_id, dry_run=True),
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "completed"
    assert FakePackerRunner.instances
    assert FakePackerRunner.instances[-1].build_called is False


async def test_sse_ordering_and_credentials_do_not_leak(
    authenticated_client,
    db_session,
):
    endpoint_id = _make_endpoint(db_session)

    create_response = await authenticated_client.post(
        "/cloud/image-factory/builds",
        json=_request_payload(endpoint_id),
    )
    assert create_response.status_code == 201, create_response.text
    build_id = create_response.json()["build_id"]

    stream_response = await authenticated_client.get(
        f"/cloud/image-factory/builds/{build_id}/stream"
    )
    assert stream_response.status_code == 200, stream_response.text
    assert "text/event-stream" in stream_response.headers["content-type"]
    assert TOKEN_MARKER not in stream_response.text

    events = _events_from_sse(stream_response.text)
    _assert_subsequence(
        events,
        [
            "build_started",
            "packer_init",
            "packer_validate",
            "packer_log",
            "packer_artifact",
            "build_completed",
            "complete",
        ],
    )


async def test_cancel_terminates_live_run(authenticated_client, db_session):
    endpoint_id = _make_endpoint(db_session)
    create_response = await authenticated_client.post(
        "/cloud/image-factory/builds",
        json=_request_payload(endpoint_id),
    )
    assert create_response.status_code == 201, create_response.text
    build_id = create_response.json()["build_id"]

    cancel_response = await authenticated_client.post(
        f"/cloud/image-factory/builds/{build_id}/cancel"
    )

    assert cancel_response.status_code == 200, cancel_response.text
    assert cancel_response.json()["status"] == "cancelled"
    assert FakePackerRunner.instances[-1].cancelled is True


async def test_validate_endpoint_does_not_invoke_build(authenticated_client, db_session):
    endpoint_id = _make_endpoint(db_session)

    response = await authenticated_client.post(
        "/cloud/image-factory/validate",
        json=_request_payload(endpoint_id),
    )

    assert response.status_code == 200, response.text
    assert response.json()["valid"] is True
    assert FakePackerRunner.instances[-1].build_called is False


async def test_gitea_recipe_creates_build(authenticated_client, db_session):
    endpoint_id = _make_endpoint(db_session)

    response = await authenticated_client.post(
        "/cloud/image-factory/builds",
        json=_request_payload(endpoint_id, provisioner_recipe="gitea"),
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "queued"


def test_gitea_recipe_renders_provisioner(tmp_path):
    from proxbox_api.services.image_factory.renderer import render_packer_workdir

    request = PackerImageBuildRequest(**_request_payload(1, provisioner_recipe="gitea"))
    rendered = render_packer_workdir(request=request, workdir=tmp_path)
    assert rendered.provisioner_path.exists()
    content = rendered.provisioner_path.read_text()
    assert "GITEA_VERSION" in content
    assert "/usr/local/bin/gitea" in content
    assert "gitea.service" in content


@pytest.mark.skipif(not shutil.which("packer"), reason="packer binary is not installed")
def test_bundled_hcl_packer_validate_smoke(tmp_path):
    endpoint_id = 1
    request = PackerImageBuildRequest(**_request_payload(endpoint_id))
    workdir = tmp_path / "packer-smoke"
    workdir.mkdir()
    from proxbox_api.services.image_factory.renderer import render_packer_workdir

    rendered = render_packer_workdir(request=request, workdir=workdir)
    env = {
        **os.environ,
        "PROXMOX_URL": "https://127.0.0.1:8006/api2/json",
        "PROXMOX_USERNAME": "root@pam!packer",
        "PROXMOX_TOKEN": "stub-token",
    }
    result = subprocess.run(
        [
            "packer",
            "validate",
            "-syntax-only",
            f"-var-file={rendered.var_file.name}",
            ".",
        ],
        cwd=workdir,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
