"""Route-independent validation for Cloud Image Pipeline SSH authority."""

from __future__ import annotations

import ipaddress
import os
import re
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

    resolved = Path(value).resolve()
    allowed_dir = _ssh_key_dir()
    try:
        resolved.relative_to(allowed_dir)
    except ValueError as exc:
        raise ValueError(
            f"SSH identity file must resolve under PROXBOX_SSH_KEY_DIR ({allowed_dir})."
        ) from exc
    return str(resolved)


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
    "is_valid_hostname",
    "normalize_ssh_fingerprint",
    "normalize_ssh_host",
    "normalize_ssh_identity_file",
    "normalize_ssh_user",
)
