"""Protocol definitions for type safety without requiring inheritance."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class NetBoxRecord(Protocol):
    """Protocol for NetBox API records with common attributes."""

    @property
    def id(self) -> int | None:
        """Unique identifier of the record."""
        ...

    @property
    def name(self) -> str | None:
        """Human-readable name of the record."""
        ...

    @property
    def slug(self) -> str | None:
        """URL-safe identifier."""
        ...

    @property
    def display(self) -> str | None:
        """Display representation."""
        ...


@runtime_checkable
class TagLike(Protocol):
    """Protocol for tag-like objects."""

    @property
    def name(self) -> str:
        """Tag name."""
        ...

    @property
    def slug(self) -> str:
        """Tag slug (URL-safe identifier)."""
        ...

    @property
    def color(self) -> str:
        """Tag color (hex code)."""
        ...


@runtime_checkable
class ProxmoxResource(Protocol):
    """Protocol for Proxmox API resource responses."""

    def get(self, key: str, default: object = None) -> object:
        """Get value by key (dict-like interface)."""
        ...

    def __getitem__(self, key: str) -> object:
        """Get value by key."""
        ...


@runtime_checkable
class SyncResult(Protocol):
    """Protocol for synchronization operation results."""

    @property
    def success(self) -> bool:
        """Whether the sync operation succeeded."""
        ...

    @property
    def created(self) -> int:
        """Number of resources created."""
        ...

    @property
    def updated(self) -> int:
        """Number of resources updated."""
        ...

    @property
    def failed(self) -> int:
        """Number of resources that failed to sync."""
        ...

    @property
    def errors(self) -> list[str]:
        """List of error messages."""
        ...
