"""Route-independent validation for Cloud Image Pipeline SSH authority."""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

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


__all__ = (
    "CloudImageSSHExecutionTarget",
    "OpenSSHIdentityFile",
    "is_valid_hostname",
    "normalize_ssh_fingerprint",
    "normalize_ssh_host",
    "normalize_ssh_identity_file",
    "normalize_ssh_user",
    "open_ssh_identity_file",
    "validate_ssh_identity_file_security",
)
