"""PDMClient factory keyed by ``PDMEndpoint`` record id."""

from __future__ import annotations

from typing import TYPE_CHECKING

from proxbox_api.database import PDMEndpoint
from proxbox_api.exception import ProxboxException

if TYPE_CHECKING:
    from proxmox_sdk.pdm import PDMClient


def _split_token_id(token_id: str) -> tuple[str, str]:
    """Split a PDM API token id of the form ``user@realm!tokenname``.

    Returns ``(user, token_name)``. Raises :class:`ProxboxException` on a
    malformed token id, which signals a misconfigured endpoint record.
    """
    if "!" not in token_id:
        raise ProxboxException(
            message=f"PDM endpoint token_id missing '!' separator: {token_id!r}",
            python_exception="ValueError: token_id must look like 'user@realm!tokenname'",
        )
    user, _, token_name = token_id.partition("!")
    if not user or not token_name:
        raise ProxboxException(
            message=f"PDM endpoint token_id has empty parts: {token_id!r}",
            python_exception="ValueError: empty user or token_name",
        )
    return user, token_name


def build_pdm_client(endpoint: PDMEndpoint) -> "PDMClient":
    """Construct a :class:`proxmox_sdk.pdm.PDMClient` from a stored endpoint.

    The PDM extra (``proxmox-sdk[pdm]``) must be installed; otherwise an
    ``ImportError`` propagates and the route handler converts it to a 503.
    """
    from proxmox_sdk.pdm import PDMClient  # noqa: PLC0415 — optional extra

    user, token_name = _split_token_id(endpoint.token_id)
    secret = endpoint.get_decrypted_token_secret()
    if not secret:
        raise ProxboxException(
            message=f"PDM endpoint {endpoint.name!r} has no decryptable token secret.",
        )
    return PDMClient(
        host=endpoint.host,
        user=user,
        token_name=token_name,
        token_value=secret,
        port=endpoint.port,
        verify_ssl=endpoint.verify_ssl,
        timeout=endpoint.timeout_seconds,
    )


__all__ = ["build_pdm_client"]
