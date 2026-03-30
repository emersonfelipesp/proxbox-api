"""Proxmox API session wrapper (single cluster / node)."""

import re
from typing import Any

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema


def _proxmox_api_factory():
    """Return ``ProxmoxAPI`` from ``session.proxmox`` so tests can monkeypatch it."""
    import proxbox_api.session.proxmox as prox_mod

    return prox_mod.ProxmoxAPI


class ProxmoxSession:
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
                proxmox_session = _proxmox_api_factory()(self.domain, **kwargs)

                # Get Proxmox Version to test connection.
                # Object instatiation does not actually connect to Proxmox, need to make an API call to test connection.
                self.version = proxmox_session.version.get()
                return proxmox_session
            else:
                logger.info(
                    f"Using IP {self.ip_address} address to authenticate with Proxmox as domain is not provided"
                )
                proxmox_session = _proxmox_api_factory()(self.ip_address, **kwargs)

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
                proxmox_session = _proxmox_api_factory()(self.ip_address, **kwargs)

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
