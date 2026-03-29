"""Proxmox session management and dependency provider utilities."""

import re
from json import JSONDecodeError
from typing import Annotated, Any

from fastapi import Depends, Query

# Proxmox
from proxmoxer import ProxmoxAPI
from sqlmodel import select

from proxbox_api.database import DatabaseSessionDep, ProxmoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema, ProxmoxTokenSchema

# Pynetbox-api Imports
from proxbox_api.session.netbox import get_netbox_async_session


#
# PROXMOX SESSION
#
class ProxmoxSession:
    """
    (Single-cluster) This class takes user-defined parameters to establish Proxmox connection and returns ProxmoxAPI object (with no further details)

    INPUT must be:
    - dict
    - pydantic model - will be converted to dict
    - json (string) - will be converted to dict

    Example of class instantiation:
    ```python
    ProxmoxSessionSchema(
        {
            "domain": "proxmox.domain.com",
            "http_port": 8006,
            "user": "user@pam",
            "password": "password",
            "token": {
                "name": "token_name",
                "value": "token_value"
            },
        }
    )
    ```
    """

    def __init__(self, cluster_config: Any):
        self.CONNECTED = False
        self.permission_limited = False
        #
        # Validate cluster_config type
        #
        if isinstance(cluster_config, ProxmoxSessionSchema):
            logger.info("INPUT is Pydantic Model ProxmoxSessionSchema")
            cluster_config = cluster_config.model_dump(mode="python")

        # FIXME: This is not working
        elif isinstance(cluster_config, str):
            logger.info("INPUT is string")
            import json

            cluster_config = json.loads(cluster_config)
            logger.info(f"json_loads: {cluster_config} - type: {type(cluster_config)}}}")

            """
            except Exception as error:
                raise ProxboxException(
                    message = f"Could not proccess the input provided, check if it is correct. Input type provided: {type(cluster_config)}",
                    detail = "ProxmoxSession class tried to convert INPUT to dict, but failed.",
                    python_exception = f"{error}",
                )
            """
        elif isinstance(cluster_config, dict):
            logger.info("INPUT is dict")
            pass
        else:
            raise ProxboxException(
                message=f"INPUT of ProxmoxSession() must be a pydantic model or dict (either one will work). Input type provided: {type(cluster_config)}",
            )

        try:
            # Save cluster_config as class attributes
            self.ip_address = cluster_config["ip_address"]
            self.domain = cluster_config["domain"]
            self.http_port = cluster_config["http_port"]
            self.user = cluster_config["user"]
            self.password = cluster_config["password"]
            self.token_name = cluster_config["token"]["name"]
            self.token_value = cluster_config["token"]["value"]
            self.ssl = cluster_config["ssl"]

            self._normalize_token_auth_fields()

        except KeyError:
            raise ProxboxException(
                message="ProxmoxSession class wasn't able to find all required parameters to establish Proxmox connection. Check if you provided all required parameters.",
                detail="Python KeyError raised",
            )

        #
        # Establish Proxmox Session
        #
        try:
            # DISABLE SSL WARNING
            if not self.ssl:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

            # Prefer using token to authenticate

            self.proxmoxer = (
                self._auth(auth_method="token")
                if self.token_name and self.token_value
                else self._auth(auth_method="password")
            )
            if self.proxmoxer:
                self.session = self.proxmoxer
                self.CONNECTED = True

        except ProxboxException as error:
            raise error

        except Exception as error:
            raise ProxboxException(
                message=f"Could not establish Proxmox connection to '{self.domain}:{self.http_port}' using token name '{self.token_name}'.",
                detail="Unknown error.",
                python_exception=f"{error}",
            )

        #
        # Test Connection and Return Cluster Status if succeeded.
        #
        if self.CONNECTED:
            try:
                """Test Proxmox Connection and return Cluster Status API response as class attribute"""
                self.cluster_status = self.session("cluster/status").get()
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
                            "After instatiating object connection, could not make API call to "
                            f"Proxmox '{self.domain}:{self.http_port}' using token name '{self.token_name}'."
                        ),
                        detail="Unknown error.",
                        python_exception=f"{__name__}: {error}",
                    )

        #
        # Add more attributes to class about Proxmox Session
        #
        self.mode = None
        self.cluster_name = None
        self.node_name = None
        self.fingerprints = None
        self.name = self.domain or self.ip_address

        if self.CONNECTED:
            if self.permission_limited:
                self.mode = "restricted"
                self.fingerprints = []
            else:
                self.mode = self.get_cluster_mode()
                if self.mode == "cluster":
                    cluster_name: str = self.get_cluster_name()

                    self.cluster_name = cluster_name
                    self.name = cluster_name
                    self.fingerprints: list = self.get_node_fingerprints(self.proxmoxer)

                elif self.mode == "standalone":
                    standalone_node_name: str = self.get_standalone_name()

                    self.node_name = standalone_node_name
                    self.name = standalone_node_name
                    self.fingerprints = None

    def __repr__(self):
        return f"Proxmox Connection Object. URL: {self.domain}:{self.http_port}"

    #
    # Proxmox Authentication Modes: TOKEN-BASED & PASSWORD-BASED
    #

    def _auth(self, auth_method: str):
        if auth_method != "token" and auth_method != "password":
            raise ProxboxException(
                message=f"Invalid authentication method provided: {auth_method}",
                detail="ProxmoxSession class only accepts 'token' or 'password' as authentication method",
            )

        target = self.domain or self.ip_address
        error_message = f"Error trying to initialize Proxmox API connection to '{target}:{self.http_port}' using {auth_method} authentication"

        # Establish Proxmox Session with Token
        USE_IP_ADDRESS = False
        try:
            logger.info(f"Using {auth_method} to authenticate with Proxmox")
            kwargs = (
                {
                    "port": self.http_port,
                    "user": self.user,
                    "token_name": self.token_name,
                    "token_value": self.token_value,
                    "verify_ssl": self.ssl,
                }
                if auth_method == "token"
                else {
                    "port": self.http_port,
                    "user": self.user,
                    "password": self.password,
                    "verify_ssl": self.ssl,
                }
            )

            # Initialize Proxmox Session using Token or Password
            if self.domain:
                logger.info(f"Using domain {self.domain} to authenticate with Proxmox")
                proxmox_session = ProxmoxAPI(self.domain, **kwargs)

                # Get Proxmox Version to test connection.
                # Object instatiation does not actually connect to Proxmox, need to make an API call to test connection.
                self.version = proxmox_session.version.get()
                return proxmox_session
            else:
                logger.info(
                    f"Using IP {self.ip_address} address to authenticate with Proxmox as domain is not provided"
                )
                proxmox_session = ProxmoxAPI(self.ip_address, **kwargs)

                # Get Proxmox Version to test connection.
                # Object instatiation does not actually connect to Proxmox, need to make an API call to test connection.
                self.version = proxmox_session.version.get()
                return proxmox_session

        except Exception as error:
            logger.info(
                f"Proxmox connection using domain failed, trying to connect using IP address {self.ip_address}\n{error}"
            )
            USE_IP_ADDRESS = True

        if USE_IP_ADDRESS:
            # If domain connection failed, try to connect using IP address.
            try:
                proxmox_session = ProxmoxAPI(self.ip_address, **kwargs)

                # Get Proxmox Version to test connection.
                # Object instatiation does not actually connect to Proxmox, need to make an API call to test connection.
                self.version = proxmox_session.version.get()
                return proxmox_session

            except Exception as error:
                raise ProxboxException(
                    message=error_message,
                    detail="Unknown error.",
                    python_exception=f"{error}",
                )

    def _normalize_token_auth_fields(self) -> None:
        """Normalize token fields from common Proxmox token string formats."""

        token_name = (self.token_name or "").strip()
        token_value = (self.token_value or "").strip()

        if token_name and token_name.startswith("PVEAPIToken=") and "!" in token_name:
            token_name = token_name.split("!", 1)[1].strip()

        # Accept full token strings like:
        # - PVEAPIToken=root@pam!tokenid=secret
        # - root@pam!tokenid=secret
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
        self.token_value = token_value or None

    @staticmethod
    def _is_permission_denied_error(error: Exception) -> bool:
        text = str(error).lower()
        return "permission check failed" in text or "403 forbidden" in text

    #
    # Get Proxmox Details about Cluster and Nodes
    #
    def get_node_fingerprints(self, px):
        """Get Nodes Fingerprints. It is the way I better found to differentiate clusters."""
        try:
            join_info = px("cluster/config/join").get()

            fingerprints = []
            for node in join_info.get("nodelist"):
                fingerprints.append(node.get("pve_fp"))

            return fingerprints

        except Exception as error:
            raise ProxboxException(
                message="Could not get Nodes Fingerprints", python_exception=f"{error}"
            )

    def get_cluster_mode(self):
        """Get Proxmox Cluster Mode (Standalone or Cluster)"""
        if self.CONNECTED:
            try:
                if len(self.cluster_status) == 1 and self.cluster_status[0].get("type") == "node":
                    return "standalone"
                else:
                    return "cluster"

            except Exception as error:
                raise ProxboxException(
                    message="Could not get Proxmox Cluster Mode (Standalone or Cluster)",
                    python_exception=f"{error}",
                )
        else:
            logger.info("Proxmox Session is not connected, so not able to get Cluster Mode")

    def get_cluster_name(self):
        """Get Proxmox Cluster Name"""
        try:
            for item in self.cluster_status:
                if item.get("type") == "cluster":
                    return item.get("name")

        except Exception as error:
            raise ProxboxException(
                message="Could not get Proxmox Cluster Name and Nodes Fingerprints",
                python_exception=f"{error}",
            )

    def get_standalone_name(self):
        """Get Proxmox Standalone Node Name"""
        try:
            if len(self.cluster_status) == 1 and self.cluster_status[0].get("type") == "node":
                return self.cluster_status[0].get("name")

        except Exception as error:
            raise ProxboxException(
                message="Could not get Proxmox Standalone Node Name",
                python_exception=f"{error}",
            )


async def proxmox_sessions(
    database_session: DatabaseSessionDep,
    source: str = "database",
    name: Annotated[
        str | None,
        Query(
            title="Proxmox Name",
            description="Name of Proxmox Cluster or Proxmox Node (if standalone).",
        ),
    ] = None,
    domain: Annotated[
        str | None,
        Query(
            title="Proxmox Domain",
            description="Domain of Proxmox Cluster or Proxmox Node (if standalone).",
        ),
    ] = None,
    ip_address: Annotated[
        str | None,
        Query(
            title="Proxmox IP Address",
            description="IP Address of Proxmox Cluster or Proxmox Node (if standalone).",
        ),
    ] = None,
    port: Annotated[
        int,
        Query(
            title="Proxmox HTTP Port",
            description="HTTP Port of Proxmox Cluster or Proxmox Node (if standalone).",
        ),
    ] = 8006,
    endpoint_ids: Annotated[
        str | None,
        Query(
            title="Proxmox Endpoint IDs",
            description="Comma-separated list of Proxmox endpoint database IDs to filter by.",
        ),
    ] = None,
):
    """
    Default Behavior: Instantiate Proxmox Sessions and return a list of Proxmox Sessions objects.
    If 'name' is provided, return only the Proxmox Session with that name.
    If 'endpoint_ids' is provided, filter by those database IDs.
    """

    endpoint_id_list = None
    if endpoint_ids:
        try:
            endpoint_id_list = [int(eid.strip()) for eid in endpoint_ids.split(",") if eid.strip()]
        except ValueError:
            pass

    proxmox_schemas = await load_proxmox_session_schemas(
        database_session=database_session,
        source=source,
        endpoint_ids=endpoint_id_list,
    )

    def return_single_session(field, value):
        for proxmox_schema in proxmox_schemas:
            if value == getattr(proxmox_schema, field, None):
                return [ProxmoxSession(proxmox_schema)]

        raise ProxboxException(
            message=f"No result found for Proxmox Sessions based on the provided {field}",
            detail="Check if the provided parameters are correct",
        )

    try:
        if ip_address is not None:
            return return_single_session("ip_address", ip_address)

        if domain is not None:
            return return_single_session("domain", domain)

        if name is not None:
            return return_single_session("name", name)
    except ProxboxException as error:
        raise error

    try:
        return [ProxmoxSession(px_schema) for px_schema in proxmox_schemas]
    except Exception as error:
        raise ProxboxException(
            message="Could not return Proxmox Sessions", python_exception=f"{error}"
        )


ProxmoxSessionsDep = Annotated[list, Depends(proxmox_sessions)]


def _netbox_field(endpoint: Any, field: str, default: Any = None) -> Any:
    if isinstance(endpoint, dict):
        return endpoint.get(field, default)
    return getattr(endpoint, field, default)


def _parse_db_endpoint(endpoint: ProxmoxEndpoint) -> ProxmoxSessionSchema:
    return ProxmoxSessionSchema(
        name=endpoint.name,
        ip_address=endpoint.ip_address,
        domain=endpoint.domain,
        http_port=endpoint.port,
        user=endpoint.username,
        password=endpoint.password,
        ssl=endpoint.verify_ssl,
        token=ProxmoxTokenSchema(
            name=endpoint.token_name,
            value=endpoint.token_value,
        ),
    )


def _parse_netbox_endpoint(endpoint: Any) -> ProxmoxSessionSchema:
    ip = None
    ip_address_object = _netbox_field(endpoint, "ip_address")
    if ip_address_object:
        if isinstance(ip_address_object, dict):
            ip_address_with_mask = ip_address_object.get("address")
        else:
            ip_address_with_mask = getattr(ip_address_object, "address", None)
        if ip_address_with_mask:
            ip = ip_address_with_mask.split("/")[0]

    return ProxmoxSessionSchema(
        name=_netbox_field(endpoint, "name"),
        ip_address=ip,
        domain=_netbox_field(endpoint, "domain"),
        http_port=_netbox_field(endpoint, "port"),
        user=_netbox_field(endpoint, "username"),
        password=_netbox_field(endpoint, "password"),
        ssl=bool(_netbox_field(endpoint, "verify_ssl", False)),
        token=ProxmoxTokenSchema(
            name=_netbox_field(endpoint, "token_name"),
            value=_netbox_field(endpoint, "token_value"),
        ),
    )


async def load_proxmox_session_schemas(
    database_session: DatabaseSessionDep,
    source: str = "database",
    endpoint_ids: list[int] | None = None,
) -> list[ProxmoxSessionSchema]:
    """Load configured Proxmox endpoint schemas without creating Proxmox API sessions."""

    if source == "netbox":
        netbox_session = get_netbox_async_session(database_session=database_session)

        try:
            url = "/api/plugins/proxbox/endpoints/proxmox/"
            if endpoint_ids:
                ids_param = ",".join(str(eid) for eid in endpoint_ids)
                url = f"{url}?id={ids_param}"
            netbox_endpoints = await rest_list_async(
                netbox_session,
                url,
            )
        except JSONDecodeError as error:
            raise ProxboxException(
                message="NetBox returned invalid JSON while fetching Proxmox endpoints",
                python_exception=str(error),
            )
        return [_parse_netbox_endpoint(endpoint) for endpoint in netbox_endpoints]

    query = select(ProxmoxEndpoint)
    if endpoint_ids:
        query = query.where(ProxmoxEndpoint.id.in_(endpoint_ids))
    db_endpoints = database_session.exec(query).all()
    return [_parse_db_endpoint(endpoint) for endpoint in db_endpoints]


async def resolve_proxmox_target_session(
    database_session: DatabaseSessionDep,
    *,
    source: str = "database",
    name: str | None = None,
    domain: str | None = None,
    ip_address: str | None = None,
) -> ProxmoxSession:
    """Resolve a single Proxmox target for generated live proxy routes."""

    proxmox_schemas = await load_proxmox_session_schemas(
        database_session=database_session,
        source=source,
    )

    selectors = (
        ("ip_address", ip_address),
        ("domain", domain),
        ("name", name),
    )
    for field, value in selectors:
        if value is None:
            continue
        for proxmox_schema in proxmox_schemas:
            if value == getattr(proxmox_schema, field, None):
                return ProxmoxSession(proxmox_schema)
        raise ProxboxException(
            message=f"No result found for Proxmox Sessions based on the provided {field}",
            detail="Check if the provided parameters are correct",
        )

    if not proxmox_schemas:
        raise ProxboxException(
            message="No Proxmox endpoints found for generated proxy route.",
            detail="Configure at least one Proxmox endpoint before using generated proxy routes.",
        )

    if len(proxmox_schemas) > 1:
        raise ProxboxException(
            message="Multiple Proxmox endpoints configured; provide name, domain, or ip_address.",
            detail="Generated Proxmox proxy routes require an explicit target when more than one endpoint is configured.",
        )

    return ProxmoxSession(proxmox_schemas[0])
