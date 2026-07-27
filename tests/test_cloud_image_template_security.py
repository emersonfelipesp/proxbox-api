"""Security validation tests for Cloud Image Build Pipeline requests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import proxbox_api.schemas.cloud_image_security as ssh_security
import proxbox_api.schemas.cloud_provision as cloud_schema
from proxbox_api.schemas.cloud_image_security import validate_ssh_identity_file_security
from proxbox_api.schemas.cloud_provision import CloudImageTemplateBuildRequest

PUBLIC_IMAGE_URL = "http://93.184.216.34/cloud.qcow2"


def _settings(*, allow_private_ips: bool) -> dict[str, object]:
    return {
        "ssrf_protection_enabled": True,
        "allow_private_ips": allow_private_ips,
        "allowed_ip_ranges": [],
        "blocked_ip_ranges": [],
    }


@pytest.fixture(autouse=True)
def isolate_ssrf_settings(monkeypatch):
    monkeypatch.setattr("proxbox_api.ssrf.is_registered_endpoint", lambda _host: False)
    monkeypatch.setattr(cloud_schema, "get_settings", lambda: _settings(allow_private_ips=False))


def _request(**overrides: object) -> CloudImageTemplateBuildRequest:
    return CloudImageTemplateBuildRequest(image_url=PUBLIC_IMAGE_URL, **overrides)


@pytest.mark.parametrize(
    "ssh_host",
    [
        "pve.example.test",
        "pve-01",
        "192.0.2.10",
        "2001:db8::1",
    ],
)
def test_ssh_host_validator_accepts_safe_hostnames_and_ips(ssh_host: str) -> None:
    assert _request(ssh_host=ssh_host).ssh_host == ssh_host


@pytest.mark.parametrize(
    "ssh_host",
    [
        "-oProxyCommand=sh",
        "bad host",
        "root@pve.example.test",
        "pve/example",
        "fe80::1%eth0",
        ".example.test",
        "bad-.example.test",
    ],
)
def test_ssh_host_validator_rejects_injection_and_invalid_hosts(ssh_host: str) -> None:
    with pytest.raises(ValidationError, match="ssh_host"):
        _request(ssh_host=ssh_host)


@pytest.mark.parametrize("ssh_user", ["root", "admin_user-01", "_svc"])
def test_ssh_user_validator_accepts_expected_usernames(ssh_user: str) -> None:
    assert _request(ssh_user=ssh_user).ssh_user == ssh_user


@pytest.mark.parametrize("ssh_user", ["-root", "bad.user", "bad user", "a" * 65])
def test_ssh_user_validator_rejects_invalid_usernames(ssh_user: str) -> None:
    with pytest.raises(ValidationError, match="ssh_user"):
        _request(ssh_user=ssh_user)


def test_ssh_identity_file_validator_accepts_path_under_configured_dir(
    monkeypatch,
    tmp_path,
) -> None:
    allowed_dir = tmp_path / "ssh_keys"
    allowed_dir.mkdir()
    identity_file = allowed_dir / "id_ed25519"
    monkeypatch.setenv("PROXBOX_SSH_KEY_DIR", str(allowed_dir))

    request = _request(ssh_identity_file=str(identity_file))

    assert request.ssh_identity_file == str(identity_file.resolve())


def test_ssh_identity_file_validator_rejects_path_escape(monkeypatch, tmp_path) -> None:
    allowed_dir = tmp_path / "ssh_keys"
    allowed_dir.mkdir()
    outside_file = tmp_path / "outside" / "id_ed25519"
    monkeypatch.setenv("PROXBOX_SSH_KEY_DIR", str(allowed_dir))

    with pytest.raises(ValidationError, match="PROXBOX_SSH_KEY_DIR"):
        _request(ssh_identity_file=str(outside_file))


def test_ssh_identity_file_validator_rejects_symlink(monkeypatch, tmp_path) -> None:
    allowed_dir = tmp_path / "ssh_keys"
    allowed_dir.mkdir()
    target = allowed_dir / "actual_key"
    target.write_text("private", encoding="utf-8")
    target.chmod(0o600)
    identity_file = allowed_dir / "id_ed25519"
    identity_file.symlink_to(target)
    monkeypatch.setenv("PROXBOX_SSH_KEY_DIR", str(allowed_dir))

    with pytest.raises(ValidationError, match="symbolic link"):
        _request(ssh_identity_file=str(identity_file))


def test_execution_identity_check_rejects_group_permissions(monkeypatch, tmp_path) -> None:
    allowed_dir = tmp_path / "ssh_keys"
    allowed_dir.mkdir()
    identity_file = allowed_dir / "id_ed25519"
    identity_file.write_text("private", encoding="utf-8")
    identity_file.chmod(0o640)
    monkeypatch.setenv("PROXBOX_SSH_KEY_DIR", str(allowed_dir))

    with pytest.raises(ValueError, match="group or world permissions"):
        validate_ssh_identity_file_security(str(identity_file))


def test_execution_identity_check_rejects_untrusted_owner(monkeypatch, tmp_path) -> None:
    allowed_dir = tmp_path / "ssh_keys"
    allowed_dir.mkdir()
    identity_file = allowed_dir / "id_ed25519"
    identity_file.write_text("private", encoding="utf-8")
    identity_file.chmod(0o600)
    monkeypatch.setenv("PROXBOX_SSH_KEY_DIR", str(allowed_dir))
    real_fstat = ssh_security.os.fstat

    def fake_fstat(fd: int):
        metadata = real_fstat(fd)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_uid=12345,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
        )

    monkeypatch.setattr(ssh_security.os, "fstat", fake_fstat)

    with pytest.raises(ValueError, match="owned by root or the service account"):
        validate_ssh_identity_file_security(str(identity_file))


def test_image_url_validator_accepts_public_url() -> None:
    assert _request().image_url == PUBLIC_IMAGE_URL


def test_image_url_validator_accepts_rfc1918_when_private_ips_allowed(monkeypatch) -> None:
    monkeypatch.setattr(cloud_schema, "get_settings", lambda: _settings(allow_private_ips=True))

    request = CloudImageTemplateBuildRequest(image_url="http://10.254.253.252/cloud.qcow2")

    assert request.image_url == "http://10.254.253.252/cloud.qcow2"


@pytest.mark.parametrize(
    "image_url",
    [
        "http://10.254.253.252/cloud.qcow2",
        "http://127.0.0.1/internal.qcow2",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_image_url_validator_rejects_internal_urls_when_private_ips_disallowed(
    image_url: str,
) -> None:
    with pytest.raises(ValidationError, match="image_url rejected by SSRF protection"):
        CloudImageTemplateBuildRequest(image_url=image_url)
