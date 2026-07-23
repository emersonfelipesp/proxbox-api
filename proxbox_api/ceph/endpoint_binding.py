"""Private, request-scoped binding for Ceph writes to one local endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from sqlmodel import select

from proxbox_api.ceph.v2_providers.base import (
    CephProviderBoundaryError,
    CephWriteGateDenied,
)
from proxbox_api.credentials import stable_keyed_fingerprint
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.database_protocols import DatabaseSessionProtocol
from proxbox_api.session.proxmox_core import ProxmoxSession, SensitiveString
from proxbox_api.session.proxmox_providers import _parse_db_endpoint
from proxbox_api.utils.async_compat import maybe_await

_TOKEN_VALUE_RE = re.compile(r"^(?:PVEAPIToken=)?(?P<user>[^!]+)!(?P<name>[^=]+)=(?P<value>.+)$")
_SESSION_KEY_BYTES = 32
_ENDPOINT_REVISION_PURPOSE = "proxbox-api/ceph-v2/endpoint-config/v1"


def _secret_value(value: object) -> str | None:
    if isinstance(value, SensitiveString):
        return value.get()
    getter = getattr(value, "get", None)
    if callable(getter):
        resolved = getter()
        return str(resolved) if resolved is not None else None
    return str(value) if value is not None else None


def _normalized_token(name: object, value: object) -> tuple[str | None, str | None]:
    token_name = str(name or "").strip()
    token_value = str(value or "").strip()
    if token_name.startswith("PVEAPIToken=") and "!" in token_name:
        token_name = token_name.split("!", 1)[1].strip()
    match = _TOKEN_VALUE_RE.match(token_value)
    if match:
        token_name = match.group("name").strip() or token_name
        token_value = match.group("value").strip()
    return token_name or None, token_value or None


def _endpoint_connection_payload(endpoint: ProxmoxEndpoint) -> dict[str, object]:
    token_name, token_value = _normalized_token(
        endpoint.token_name,
        endpoint.get_decrypted_token_value(),
    )
    return {
        "endpoint_id": endpoint.id,
        "ip_address": endpoint.ip_address,
        "domain": endpoint.domain,
        "http_port": endpoint.port,
        "user": endpoint.username,
        "password": endpoint.get_decrypted_password(),
        "token_name": token_name,
        "token_value": token_value,
        "ssl": endpoint.verify_ssl,
        "timeout": endpoint.timeout if endpoint.timeout is not None else 5,
        "connect_timeout": None,
        "max_retries": endpoint.max_retries if endpoint.max_retries is not None else 0,
        "retry_backoff": (
            float(endpoint.retry_backoff) if endpoint.retry_backoff is not None else 0.5
        ),
    }


def _session_connection_payload(session: object) -> dict[str, object]:
    token_name, token_value = _normalized_token(
        getattr(session, "token_name", None),
        _secret_value(getattr(session, "token_value", None)),
    )
    return {
        "endpoint_id": getattr(session, "db_endpoint_id", None),
        "ip_address": getattr(session, "ip_address", None),
        "domain": getattr(session, "domain", None),
        "http_port": getattr(session, "http_port", None),
        "user": getattr(session, "user", None),
        "password": _secret_value(getattr(session, "password", None)),
        "token_name": token_name,
        "token_value": token_value,
        "ssl": getattr(session, "ssl", None),
        "timeout": getattr(session, "timeout", None),
        "connect_timeout": getattr(session, "connect_timeout", None),
        "max_retries": getattr(session, "max_retries", None),
        "retry_backoff": getattr(session, "retry_backoff", None),
    }


def _endpoint_configuration_payload(endpoint: ProxmoxEndpoint) -> dict[str, object]:
    """Return the complete mutation-relevant endpoint schema for durable binding."""

    return {
        **_endpoint_connection_payload(endpoint),
        "enabled": endpoint.enabled,
        "allow_writes": endpoint.allow_writes,
        "access_methods": endpoint.access_methods,
    }


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _tag(key: bytes, payload: dict[str, object]) -> bytes:
    return hmac.new(key, _canonical_bytes(payload), hashlib.sha256).digest()


def endpoint_configuration_revision(endpoint: ProxmoxEndpoint) -> str:
    """Create a stable server-keyed revision without persisting endpoint secrets."""

    return stable_keyed_fingerprint(
        _canonical_bytes(_endpoint_configuration_payload(endpoint)),
        purpose=_ENDPOINT_REVISION_PURPOSE,
    )


async def _exact_local_endpoint(
    session: DatabaseSessionProtocol,
    endpoint_id: int,
) -> ProxmoxEndpoint:
    await maybe_await(session.rollback())
    result = await maybe_await(
        session.exec(
            select(ProxmoxEndpoint)
            .where(ProxmoxEndpoint.id == endpoint_id)
            .execution_options(populate_existing=True)
        )
    )
    rows = list(result.all())
    if not rows:
        raise CephWriteGateDenied(
            "endpoint_missing",
            "The selected local Proxmox endpoint does not exist.",
        )
    if len(rows) != 1:
        raise CephWriteGateDenied(
            "endpoint_session_ambiguous",
            "The selected local Proxmox endpoint did not resolve uniquely.",
        )
    return rows[0]


class BoundProxmoxSession:
    """One opaque session plus an unforgeable per-request endpoint binding.

    The HMAC key and tag are name-mangled, slot-only fields. They are never
    returned, logged, persisted, or included in ``repr``.
    """

    __slots__ = (
        "__binding_key",
        "__binding_tag",
        "__config_revision",
        "__endpoint_id",
        "__session",
    )

    def __init__(
        self,
        *,
        endpoint: ProxmoxEndpoint,
        session: ProxmoxSession,
        binding_key: bytes,
    ) -> None:
        endpoint_payload = _endpoint_connection_payload(endpoint)
        session_payload = _session_connection_payload(session)
        endpoint_tag = _tag(binding_key, endpoint_payload)
        session_tag = _tag(binding_key, session_payload)
        if not hmac.compare_digest(endpoint_tag, session_tag):
            raise CephWriteGateDenied(
                "endpoint_session_binding_mismatch",
                "The created Proxmox session does not match the selected endpoint schema.",
            )
        self.__endpoint_id = endpoint.id or 0
        self.__session = session
        self.__binding_key = binding_key
        self.__binding_tag = endpoint_tag
        self.__config_revision = endpoint_configuration_revision(endpoint)

    def __repr__(self) -> str:
        return f"BoundProxmoxSession(endpoint_id={self.__endpoint_id})"

    @property
    def endpoint_id(self) -> int:
        return self.__endpoint_id

    @property
    def endpoint_config_revision(self) -> str:
        """Opaque durable revision safe to persist with plans and approvals."""

        return self.__config_revision

    def raw_session(self) -> ProxmoxSession:
        """Return the wrapped session only to the provider adapter."""

        return self.__session

    async def verify_fresh(
        self,
        database_session: DatabaseSessionProtocol,
        *,
        expected_revision: str | None = None,
    ) -> None:
        """Reload policy and compare endpoint plus session binding in constant time."""

        endpoint = await _exact_local_endpoint(database_session, self.__endpoint_id)
        if not endpoint.enabled:
            raise CephWriteGateDenied(
                "endpoint_disabled",
                "The selected Proxmox endpoint is disabled.",
            )
        if not endpoint.allow_writes:
            raise CephWriteGateDenied(
                "endpoint_writes_disabled",
                "The selected Proxmox endpoint has allow_writes=false.",
            )
        current_revision = endpoint_configuration_revision(endpoint)
        expected_matches = expected_revision is None or hmac.compare_digest(
            expected_revision,
            current_revision,
        )
        bound_revision_matches = hmac.compare_digest(
            self.__config_revision,
            current_revision,
        )
        if not (expected_matches and bound_revision_matches):
            raise CephWriteGateDenied(
                "endpoint_configuration_changed",
                "The endpoint configuration no longer matches the canonical plan.",
            )
        endpoint_tag = _tag(self.__binding_key, _endpoint_connection_payload(endpoint))
        session_tag = _tag(
            self.__binding_key,
            _session_connection_payload(self.__session),
        )
        endpoint_matches = hmac.compare_digest(self.__binding_tag, endpoint_tag)
        session_matches = hmac.compare_digest(self.__binding_tag, session_tag)
        if not (endpoint_matches and session_matches):
            raise CephWriteGateDenied(
                "endpoint_session_binding_changed",
                "The endpoint connection schema or bound session changed after selection.",
            )

    async def aclose(self) -> None:
        """Close the private session even when the request is being cancelled.

        ``except Exception`` alone does not cover ``asyncio.CancelledError``:
        every caller runs this in a cleanup path, so a client disconnect while
        the underlying close is in flight would abandon the session and leak
        it. Shield the close until it reaches a terminal state, then re-raise
        the remembered cancellation.
        """
        close = getattr(self.__session, "aclose", None)
        if not callable(close):
            return
        close_task = asyncio.ensure_future(maybe_await(close()))
        cancellation_requested = False
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                if close_task.done() and close_task.cancelled():
                    raise
                cancellation_requested = True
            except Exception:  # noqa: BLE001 - cleanup is best effort and secret-free
                break
        if cancellation_requested:
            raise asyncio.CancelledError


async def create_bound_proxmox_session(
    database_session: DatabaseSessionProtocol,
    endpoint_id: int,
    *,
    session_factory: Callable[[Any], Awaitable[ProxmoxSession]] | None = None,
) -> tuple[BoundProxmoxSession, ProxmoxEndpoint]:
    """Resolve one local endpoint and create exactly one privately bound session."""

    proxmox_session: ProxmoxSession | None = None
    try:
        endpoint = await _exact_local_endpoint(database_session, endpoint_id)
        if not endpoint.enabled:
            raise CephWriteGateDenied(
                "endpoint_disabled",
                "The selected Proxmox endpoint is disabled.",
            )
        schema = _parse_db_endpoint(endpoint)
        factory = session_factory or ProxmoxSession.create
        proxmox_session = await factory(schema)
        bound = BoundProxmoxSession(
            endpoint=endpoint,
            session=proxmox_session,
            binding_key=secrets.token_bytes(_SESSION_KEY_BYTES),
        )
    except CephWriteGateDenied:
        await _close_unbound_session(proxmox_session)
        raise
    except Exception:  # noqa: BLE001
        await _close_unbound_session(proxmox_session)
        raise CephProviderBoundaryError(
            "endpoint_session_unavailable",
            "The selected Proxmox endpoint session could not be created.",
        ) from None
    return bound, endpoint


async def _close_unbound_session(session: object | None) -> None:
    close = getattr(session, "aclose", None)
    if not callable(close):
        return
    try:
        await maybe_await(close())
    except Exception:  # noqa: BLE001 - cleanup is best effort and secret-free
        return


__all__ = [
    "BoundProxmoxSession",
    "create_bound_proxmox_session",
    "endpoint_configuration_revision",
]
