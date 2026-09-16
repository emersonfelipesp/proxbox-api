"""Bounded Proxmox reads through the SDK's public bounded-read contract."""

from __future__ import annotations

from typing import Protocol

from proxmox_sdk.sdk.exceptions import (
    ResponseTooLargeError,
    UnsupportedResponseEncodingError,
)


class BoundedReadResource(Protocol):
    """The subset of ``ProxmoxResource`` used for bounded reads."""

    async def get_bounded(self, max_response_bytes: int, /, **params: object) -> object: ...


class ProxmoxResponseTooLargeError(Exception):
    """Raised before a Proxmox response can exceed its byte limit."""


class ProxmoxUnsupportedEncodingError(Exception):
    """Raised when a bounded response is transport-compressed."""


async def bounded_proxmox_get(
    resource: BoundedReadResource, maximum_bytes: int, **parameters: object
) -> object:
    """Read JSON without materializing more than ``maximum_bytes`` bytes.

    Proxmox expects ``0``/``1`` for booleans and the SDK forwards query values
    verbatim, so encode here; ``None`` values are dropped before the call.
    """
    encoded = {
        key: int(value) if isinstance(value, bool) else value
        for key, value in parameters.items()
        if value is not None
    }
    try:
        return await resource.get_bounded(maximum_bytes, **encoded)
    except ResponseTooLargeError as exc:
        raise ProxmoxResponseTooLargeError from exc
    except UnsupportedResponseEncodingError as exc:
        raise ProxmoxUnsupportedEncodingError from exc
