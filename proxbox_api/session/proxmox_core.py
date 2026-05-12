"""Proxmox API session wrapper (single cluster / node)."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Mapping
from types import TracebackType
from typing import TYPE_CHECKING

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema

if TYPE_CHECKING:
    from proxmox_sdk import ProxmoxSDK


class SensitiveString:
    """Wrapper for sensitive string values to prevent accidental exposure in logs/serialization."""

    def __init__(self, value: str | None):
        self._value = value

    def __str__(self) -> str:
        return "[REDACTED]"

    def __repr__(self) -> str:
        return "[REDACTED]"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SensitiveString):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return False

    def get(self) -> str | None:
        return self._value


def _proxmox_api_factory() -> type[ProxmoxSDK]:
    """Return ``ProxmoxAPI`` from ``session.proxmox`` so tests can monkeypatch it."""
    import proxbox_api.session.proxmox as prox_mod

    return prox_mod.ProxmoxAPI


class ProxmoxSession:
    """Proxmox API session wrapper with async factory pattern."""

    def __init__(
        self, cluster_config: ProxmoxSessionSchema | Mapping[str, object] | str | None = None
    ) -> None:
        """Initialize empty session.

        If ``cluster_config`` is provided, perform eager initialization for
        backwards-compatible sync call sites.
        """
        self.CONNECTED = False
        self.permission_limited = False
        self.ip_address: str | None = None
        self.domain: str | None = None
        self.http_port: int = 8006
        self.user: str | None = None
        self.password: SensitiveString | None = None
        self.token_name: str | None = None
        self.token_value: SensitiveString | None = None
        self.ssl: bool = True
        self.timeout: int = 5
        self.connect_timeout: int | None = None
        self.max_retries: int = 0
        self.retry_backoff: float = 0.5
        self.proxmox: ProxmoxSDK | None = None
        self.session: ProxmoxSDK | None = None
        self.version: str | None = None
        self.cluster_status: list[dict[str, object]] = []
        self.mode: str | None = None
        self.cluster_name: str | None = None
        self.node_name: str | None = None
        self.fingerprints: list[str] | None = None
        self.name: str | None = None
        self.site_id: int | None = None
        self.site_slug: str | None = None
        self.site_name: str | None = None
        self.tenant_id: int | None = None
        self.tenant_slug: str | None = None
        self.tenant_name: str | None = None

        if cluster_config is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(self._initialize(cluster_config))

    def __repr__(self) -> str:
        return f"Proxmox Connection Object. URL: {self.domain}:{self.http_port}"

    async def __aenter__(self) -> "ProxmoxSession":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @classmethod
    async def create(
        cls, cluster_config: ProxmoxSessionSchema | Mapping[str, object] | str
    ) -> "ProxmoxSession":
        """Async factory method to create and initialize a ProxmoxSession."""
        instance = cls()
        await instance._initialize(cluster_config)
        return instance

    async def _initialize(
        self, cluster_config: ProxmoxSessionSchema | Mapping[str, object] | str
    ) -> None:
        """Async initialization of the session."""
        config = self._parse_config(cluster_config)
        self._set_attributes_from_config(config)

        if not self.ssl:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.proxmox = await self._auth_async()
        if self.proxmox:
            self.session = self.proxmox
            self.CONNECTED = True

        if self.CONNECTED:
            await self._post_connect_init()

    def _parse_config(
        self, cluster_config: ProxmoxSessionSchema | Mapping[str, object] | str
    ) -> dict[str, object]:
        """Parse and validate cluster configuration."""
        if isinstance(cluster_config, ProxmoxSessionSchema):
            logger.info("INPUT is Pydantic Model ProxmoxSessionSchema")
            return cluster_config.model_dump(mode="python")

        if isinstance(cluster_config, str):
            logger.info("INPUT is string")
            try:
                return json.loads(cluster_config)
            except json.JSONDecodeError as error:
                raise ProxboxException(
                    message=(
                        "Could not process the input provided; expected JSON object string. "
                        f"Input type: {type(cluster_config)}"
                    ),
                    detail="ProxmoxSession failed to parse string input as JSON.",
                    python_exception=str(error),
                ) from error

        if isinstance(cluster_config, Mapping):
            logger.info("INPUT is dict")
            return dict(cluster_config)

        raise ProxboxException(
            message=f"INPUT of ProxmoxSession() must be a pydantic model or dict (either one will work). Input type provided: {type(cluster_config)}",
        )

    def _set_attributes_from_config(self, config: dict[str, object]) -> None:
        """Set instance attributes from parsed config."""
        try:
            self.ip_address = config["ip_address"]
            self.domain = config["domain"]
            self.http_port = config["http_port"]
            self.user = config["user"]
            self.password = SensitiveString(config["password"])
            self.token_name = config["token"]["name"]
            self.token_value = SensitiveString(config["token"]["value"])
            self.ssl = config["ssl"]
            self.timeout = int(config["timeout"]) if config.get("timeout") is not None else 5
            self.connect_timeout = (
                int(config["connect_timeout"])
                if config.get("connect_timeout") is not None
                else None
            )
            self.max_retries = (
                int(config["max_retries"]) if config.get("max_retries") is not None else 0
            )
            self.retry_backoff = (
                float(config["retry_backoff"]) if config.get("retry_backoff") is not None else 0.5
            )
            self.site_id = int(config["site_id"]) if config.get("site_id") is not None else None
            self.site_slug = str(config["site_slug"]) if config.get("site_slug") else None
            self.site_name = str(config["site_name"]) if config.get("site_name") else None
            self.tenant_id = (
                int(config["tenant_id"]) if config.get("tenant_id") is not None else None
            )
            self.tenant_slug = str(config["tenant_slug"]) if config.get("tenant_slug") else None
            self.tenant_name = str(config["tenant_name"]) if config.get("tenant_name") else None
            self._normalize_token_auth_fields()
        except KeyError:
            raise ProxboxException(
                message="ProxmoxSession class wasn't able to find all required parameters to establish Proxmox connection. Check if you provided all required parameters.",
                detail="Python KeyError raised",
            )

    async def _post_connect_init(self) -> None:
        """Async post-connection initialization."""
        try:
            self.cluster_status = await resolve_async(self.session("cluster/status").get())
        except Exception as error:
            if self._is_permission_denied_error(error):
                logger.warning(
                    "Connected to Proxmox %s:%s but cluster/status is forbidden; continuing in restricted mode",
                    self.domain or self.ip_address,
                    self.http_port,
                )
                self.permission_limited = True
                self.cluster_status = []
            else:
                raise ProxboxException(
                    message=(
                        "After initializing object connection, could not make API call to "
                        f"Proxmox '{self.domain}:{self.http_port}' using token name '{self.token_name}'."
                    ),
                    detail="Unknown error.",
                    python_exception=f"{__name__}: {error}",
                )

        self.name = self.domain or self.ip_address

        if self.CONNECTED:
            if self.permission_limited:
                self.mode = "restricted"
                self.fingerprints = []
            else:
                self.mode = self._get_cluster_mode()
                if self.mode == "cluster":
                    self.cluster_name = self._get_cluster_name()
                    self.name = self.cluster_name
                    self.fingerprints = await self._get_node_fingerprints_async(self.proxmox)
                elif self.mode == "standalone":
                    self.node_name = self._get_standalone_name()
                    self.name = self.node_name
                    self.fingerprints = None

    async def _auth_async(self) -> ProxmoxSDK:
        """Async authentication to Proxmox."""
        auth_method = "token" if (self.token_name and self._get_token_value()) else "password"
        target = self.domain or self.ip_address
        error_message = f"Error trying to initialize Proxmox API connection to '{target}:{self.http_port}' using {auth_method} authentication"

        kwargs = self._build_auth_kwargs(auth_method)

        # Try domain first, then IP address
        if self.domain:
            logger.info("Using %s to authenticate with Proxmox", auth_method)
            logger.info("Using domain %s to authenticate with Proxmox", self.domain)
            try:
                proxmox_session = _proxmox_api_factory()(self.domain, **kwargs)
                self.version = await resolve_async(proxmox_session.version.get())
                return proxmox_session
            except Exception as error:
                logger.info(
                    "Proxmox connection using domain failed, trying IP %s: %s",
                    self.ip_address,
                    error,
                )

        # Fallback to IP address
        try:
            logger.info("Using IP %s to authenticate with Proxmox", self.ip_address)
            proxmox_session = _proxmox_api_factory()(self.ip_address, **kwargs)
            self.version = await resolve_async(proxmox_session.version.get())
            return proxmox_session
        except Exception as error:
            raise ProxboxException(
                message=error_message,
                detail="Unknown error.",
                python_exception=f"{error}",
            ) from error

    def _build_auth_kwargs(self, auth_method: str) -> dict[str, object]:
        """Build authentication kwargs for Proxmox API."""
        connection_kwargs: dict[str, object] = {
            "verify_ssl": self.ssl,
            "timeout": self.timeout,
            "connect_timeout": self.connect_timeout,
            "max_retries": self.max_retries,
            "retry_backoff": self.retry_backoff,
        }
        if auth_method == "token":
            return {
                "port": self.http_port,
                "user": self.user,
                "token_name": self.token_name,
                "token_value": self._get_token_value(),
                **connection_kwargs,
            }
        return {
            "port": self.http_port,
            "user": self.user,
            "password": self._get_password(),
            **connection_kwargs,
        }

    def _get_token_value(self) -> str | None:
        """Get token value from SensitiveString."""
        return (
            self.token_value.get()
            if isinstance(self.token_value, SensitiveString)
            else self.token_value
        )

    def _get_password(self) -> str | None:
        """Get password from SensitiveString."""
        return self.password.get() if isinstance(self.password, SensitiveString) else self.password

    def _normalize_token_auth_fields(self) -> None:
        """Normalize token fields from common Proxmox token string formats."""
        raw_token_value = self._get_token_value()
        token_name = (self.token_name or "").strip()
        token_value = (raw_token_value or "").strip()

        if token_name and token_name.startswith("PVEAPIToken=") and "!" in token_name:
            token_name = token_name.split("!", 1)[1].strip()

        if token_value and ("!" in token_value or token_value.startswith("PVEAPIToken=")):
            full_token_match = re.match(
                r"^(?:PVEAPIToken=)?(?P<user>[^!]+)!(?P<name>[^=]+)=(?P<value>.+)$",
                token_value,
            )
            if full_token_match:
                parsed_name = full_token_match.group("name").strip()
                parsed_value = full_token_match.group("value").strip()
                if parsed_name:
                    token_name = parsed_name
                token_value = parsed_value

        self.token_name = token_name or None
        self.token_value = SensitiveString(token_value) if token_value else None

    @staticmethod
    def _is_permission_denied_error(error: Exception) -> bool:
        """Check if error is a permission denied error."""
        text = str(error).lower()
        return "permission check failed" in text or "403 forbidden" in text

    def _get_cluster_mode(self) -> str | None:
        """Get Proxmox Cluster Mode (Standalone or Cluster)."""
        if not self.CONNECTED:
            logger.info("Proxmox Session is not connected, so not able to get Cluster Mode")
            return None

        try:
            if len(self.cluster_status) == 1 and self.cluster_status[0].get("type") == "node":
                return "standalone"
            return "cluster"
        except Exception as error:
            raise ProxboxException(
                message="Could not get Proxmox Cluster Mode (Standalone or Cluster)",
                python_exception=f"{error}",
            ) from error

    def _get_cluster_name(self) -> str | None:
        """Get Proxmox Cluster Name."""
        try:
            for item in self.cluster_status:
                if item.get("type") == "cluster":
                    return item.get("name")
            return None
        except Exception as error:
            raise ProxboxException(
                message="Could not get Proxmox Cluster Name and Nodes Fingerprints",
                python_exception=f"{error}",
            ) from error

    def _get_standalone_name(self) -> str | None:
        """Get Proxmox Standalone Node Name."""
        try:
            if len(self.cluster_status) == 1 and self.cluster_status[0].get("type") == "node":
                return self.cluster_status[0].get("name")
            return None
        except Exception as error:
            raise ProxboxException(
                message="Could not get Proxmox Standalone Node Name",
                python_exception=f"{error}",
            ) from error

    async def _get_node_fingerprints_async(self, px: ProxmoxSDK) -> list[str]:
        """Get Nodes Fingerprints asynchronously."""
        try:
            join_info = px("cluster/config/join").get()
            join_info = await resolve_async(join_info)
            fingerprints: list[str] = []
            for node in join_info.get("nodelist", []):
                fingerprints.append(node.get("pve_fp"))
            return fingerprints
        except Exception as error:
            raise ProxboxException(
                message="Could not get Nodes Fingerprints",
                python_exception=f"{error}",
            ) from error

    async def aclose(self) -> None:
        """Async close for session cleanup."""
        sdk_session = getattr(self, "session", None)
        if sdk_session is not None and hasattr(sdk_session, "close"):
            close_result = sdk_session.close()
            if inspect.isawaitable(close_result):
                await close_result
        self.session = None

    def close(self) -> None:
        """Sync close - logs warning if called (use aclose() in async contexts)."""
        logger.warning(
            "close() called on ProxmoxSession; use aclose() in async contexts for proper cleanup"
        )
