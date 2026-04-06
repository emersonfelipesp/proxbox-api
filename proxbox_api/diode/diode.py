"""Experimental Diode client integration example script."""

import os

from netboxlabs.diode.sdk import DiodeClient
from netboxlabs.diode.sdk.ingester import (
    Device,
    Entity,
    IPAddress,
)

from proxbox_api.logger import logger


def main() -> None:
    api_key = os.environ.get("DIODE_API_KEY", "")
    if not api_key:
        logger.error("DIODE_API_KEY environment variable is not set")
        return

    with DiodeClient(
        target="grpc://localhost:8081",
        app_name="my-test-app",
        app_version="0.0.1",
        api_key=api_key,
    ) as client:
        entities = []

        """
        Ingest device with device type, platform, manufacturer, site, role, and tags.
        """

        device = Device(
            name="TESTE",
            device_type="Device Type A",
            platform="Platform A",
            manufacturer="Manufacturer A",
            site="Site ABC",
            role="Role ABC",
            serial="123456",
            asset_tag="123456",
            status="active",
            tags=["tag 1", "tag 2"],
        )

        # device = Device(name="Device A")

        ip_address = IPAddress(
            address="172.16.0.1/24",
        )

        logger.debug("Device payload: %s", device)

        entities.append(Entity(ip_address=ip_address))

        response = client.ingest(entities=entities)
        logger.info("Diode ingest response: %s", response)
        if response.errors:
            logger.error("Diode ingest errors: %s", response.errors)


if __name__ == "__main__":
    main()
