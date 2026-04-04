"""Exception types for the standalone Proxmox mock package."""

from __future__ import annotations


class ProxmoxMockError(Exception):
    """Base exception for schema-driven Proxmox mock failures."""

    def __init__(
        self,
        message: str,
        detail: str | dict[str, object] | None = None,
        python_exception: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.python_exception = python_exception
