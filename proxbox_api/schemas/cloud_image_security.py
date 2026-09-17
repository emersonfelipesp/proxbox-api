"""Route-independent validation for Cloud Image Pipeline SSH authority."""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

_DEFAULT_SSH_KEY_DIR = Path("/etc/proxbox/ssh_keys")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_SSH_USER_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$")
_SSH_SHA256_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


def is_valid_hostname(value: str) -> bool:
    """Return whether ``value`` is a conservative RFC-style hostname."""

    if not value or len(value) > 253:
        return False
    hostname = value[:-1] if value.endswith(".") else value
    if not hostname:
        return False
    return all(_HOST_LABEL_RE.fullmatch(label) for label in hostname.split("."))


def _ssh_key_dir() -> Path:
    configured = os.environ.get("PROXBOX_SSH_KEY_DIR", "").strip()
    return Path(configured).resolve() if configured else _DEFAULT_SSH_KEY_DIR.resolve()


def normalize_ssh_host(value: str) -> str:
    """Validate one SSH host without permitting option injection."""

    host = value.strip()
    if not host:
        raise ValueError("SSH host must be a non-empty hostname or IP address.")
    if host.startswith("-"):
        raise ValueError("SSH host must not start with '-' or resemble an ssh option.")
    if "%" in host:
        raise ValueError("SSH host must not include an IPv6 zone identifier.")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not is_valid_hostname(host):
        raise ValueError("SSH host must be a valid hostname, IPv4 address, or IPv6 address.")
    return host


def normalize_ssh_user(value: str) -> str:
    """Validate the SSH user accepted by the fixed-argv execution boundary."""

    user = value.strip()
    if not _SSH_USER_RE.fullmatch(user):
        raise ValueError("SSH user must match ^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$.")
    return user


def normalize_ssh_identity_file(value: str) -> str:
    """Resolve an identity path and constrain it to the configured key directory."""

    candidate = Path(value)
    if candidate.is_symlink():
        raise ValueError("SSH identity file must not be a symbolic link.")
    resolved = candidate.resolve()
    allowed_dir = _ssh_key_dir()
    try:
        resolved.relative_to(allowed_dir)
    except ValueError as exc:
        raise ValueError(
            f"SSH identity file must resolve under PROXBOX_SSH_KEY_DIR ({allowed_dir})."
        ) from exc
    return str(resolved)


def validate_ssh_identity_file_security(value: str) -> str:
    """Fail closed unless an identity is a private, trusted regular file."""

    handle = open_ssh_identity_file(value)
    handle.close()
    return handle.source_path


@dataclass(frozen=True)
class OpenSSHIdentityFile:
    """Race-free identity descriptor inherited by an OpenSSH child."""

    fd: int
    source_path: str

    @property
    def child_path(self) -> str:
        return f"/proc/self/fd/{self.fd}"

    def close(self) -> None:
        os.close(self.fd)


def _validate_identity_metadata(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("SSH identity file must be a regular non-symlink file.")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise ValueError("SSH identity file must be owned by root or the service account.")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("SSH identity file must not grant group or world permissions.")


def open_ssh_identity_file(value: str) -> OpenSSHIdentityFile:
    """Open and verify a key once so later pathname swaps cannot change it."""

    path = Path(normalize_ssh_identity_file(value))
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("SSH identity file is unavailable.") from exc
    try:
        opened_metadata = os.fstat(fd)
        _validate_identity_metadata(opened_metadata)
        path_metadata = path.lstat()
        if stat.S_ISLNK(path_metadata.st_mode) or (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ) != (opened_metadata.st_dev, opened_metadata.st_ino):
            raise ValueError("SSH identity file changed while it was opened.")
    except (OSError, ValueError):
        os.close(fd)
        raise
    return OpenSSHIdentityFile(fd=fd, source_path=str(path))


def normalize_ssh_fingerprint(value: str) -> str:
    """Return one canonical OpenSSH SHA-256 host-key fingerprint."""

    fingerprint = value.strip()
    if fingerprint.lower().startswith("sha256:"):
        fingerprint = f"SHA256:{fingerprint.split(':', 1)[1]}"
    if not _SSH_SHA256_FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("SSH host-key fingerprint must be SHA256:<43 base64 characters>.")
    return fingerprint


class CloudImageSSHExecutionTarget(BaseModel):
    """Persisted, derived SSH authority used by executable image builds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    user: str
    port: int = Field(..., ge=1, le=65535)
    identity_file: str
    known_host_fingerprint: str

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return normalize_ssh_host(value)

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        return normalize_ssh_user(value)

    @field_validator("identity_file")
    @classmethod
    def validate_identity_file(cls, value: str) -> str:
        return normalize_ssh_identity_file(value)

    @field_validator("known_host_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        return normalize_ssh_fingerprint(value)


class SSHBindingEndpoint(Protocol):
    id: int | None
    enabled: bool
    allow_writes: bool
    ssh_enabled: bool
    has_cloud_image_ssh_binding: bool
    ssh_target_node: str | None
    ssh_host: str | None
    ssh_username: str | None
    ssh_port: int
    ssh_identity_file: str | None
    ssh_known_host_fingerprint: str | None


class SSHBindingAssertions(Protocol):
    ssh_host: str | None
    ssh_user: str
    ssh_port: int
    ssh_identity_file: str | None
    ssh_known_host_fingerprint: str | None
    model_fields_set: set[str]

    @property
    def target_node(self) -> str | None: ...


class SSHBindingError(ValueError):
    """Stable route-independent persisted SSH binding failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        endpoint_id: int,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.endpoint_id = endpoint_id
        self.field = field


def _binding_failure(
    endpoint: SSHBindingEndpoint, request: SSHBindingAssertions
) -> tuple[str, int, str] | None:
    checks = (
        (
            not endpoint.enabled,
            "endpoint_disabled",
            422,
            "The persisted Proxmox endpoint is disabled.",
        ),
        (
            not endpoint.allow_writes,
            "endpoint_writes_disabled",
            403,
            "The persisted Proxmox endpoint does not allow writes.",
        ),
        (
            not endpoint.ssh_enabled,
            "endpoint_ssh_disabled",
            403,
            "The persisted Proxmox endpoint does not allow SSH execution.",
        ),
        (
            not request.target_node,
            "target_node_required",
            422,
            "target_node is required for executable builds.",
        ),
        (
            not endpoint.has_cloud_image_ssh_binding,
            "endpoint_ssh_binding_incomplete",
            422,
            "The endpoint has no complete persisted Cloud Image SSH binding.",
        ),
        (
            request.target_node != endpoint.ssh_target_node,
            "endpoint_node_mismatch",
            409,
            "target_node does not match the endpoint's persisted SSH node.",
        ),
    )
    return next(
        ((code, status_code, message) for failed, code, status_code, message in checks if failed),
        None,
    )


def resolve_ssh_execution_target(
    endpoint: SSHBindingEndpoint,
    request: SSHBindingAssertions,
) -> CloudImageSSHExecutionTarget:
    """Derive an SSH target only from persisted endpoint authority."""

    endpoint_id = int(endpoint.id or 0)
    if failure := _binding_failure(endpoint, request):
        raise SSHBindingError(
            failure[0], failure[2], status_code=failure[1], endpoint_id=endpoint_id
        )
    try:
        target = CloudImageSSHExecutionTarget(
            host=str(endpoint.ssh_host),
            user=str(endpoint.ssh_username),
            port=endpoint.ssh_port,
            identity_file=str(endpoint.ssh_identity_file),
            known_host_fingerprint=str(endpoint.ssh_known_host_fingerprint),
        )
    except Exception as error:
        raise SSHBindingError(
            "endpoint_ssh_binding_invalid",
            "The endpoint's persisted Cloud Image SSH binding is invalid.",
            status_code=422,
            endpoint_id=endpoint_id,
        ) from error
    assertions = {
        "ssh_host": target.host,
        "ssh_user": target.user,
        "ssh_port": target.port,
        "ssh_identity_file": target.identity_file,
        "ssh_known_host_fingerprint": target.known_host_fingerprint,
    }
    for field, expected in assertions.items():
        if field in request.model_fields_set and getattr(request, field) != expected:
            raise SSHBindingError(
                "endpoint_ssh_binding_mismatch",
                "Caller SSH assertions do not match the persisted endpoint binding.",
                status_code=409,
                endpoint_id=endpoint_id,
                field=field,
            )
    return target


__all__ = (
    "CloudImageSSHExecutionTarget",
    "OpenSSHIdentityFile",
    "SSHBindingError",
    "is_valid_hostname",
    "normalize_ssh_fingerprint",
    "normalize_ssh_host",
    "normalize_ssh_identity_file",
    "normalize_ssh_user",
    "open_ssh_identity_file",
    "resolve_ssh_execution_target",
    "validate_ssh_identity_file_security",
)
