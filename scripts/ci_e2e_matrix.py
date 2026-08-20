#!/usr/bin/env python3
"""Generate the bounded GitHub E2E matrix for every supported event."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MAX_GITHUB_MATRIX_JOBS = 256
PROXMOX_SERVICES = ("pve", "pbs", "pdm")
BASE_TRANSPORTS: tuple[dict[str, Any], ...] = (
    {
        "netbox_transport": "https_nginx",
        "netbox_public_url": "https://127.0.0.1:18443",
        "netbox_endpoint_host": "netbox-e2e-nginx",
        "netbox_endpoint_port": 8443,
        "netbox_endpoint_verify_ssl": True,
        "netbox_wait_url": "https://127.0.0.1:18443/api/",
        "netbox_app_container": "netbox-e2e-http",
        "proxbox_transport": "http_raw",
        "proxbox_docker_target": "raw",
        "proxbox_base_url": "http://127.0.0.1:18081",
        "proxbox_host_port": 18081,
        "proxbox_curl_flags": "",
        "network_stack": "ipv4",
    },
    {
        "netbox_transport": "https_granian",
        "netbox_public_url": "https://127.0.0.1:18443",
        "netbox_endpoint_host": "netbox-e2e-granian",
        "netbox_endpoint_port": 8443,
        "netbox_endpoint_verify_ssl": True,
        "netbox_wait_url": "https://127.0.0.1:18443/api/",
        "netbox_app_container": "netbox-e2e-granian",
        "proxbox_transport": "http_raw",
        "proxbox_docker_target": "raw",
        "proxbox_base_url": "http://127.0.0.1:18081",
        "proxbox_host_port": 18081,
        "proxbox_curl_flags": "",
        "network_stack": "ipv4",
    },
    {
        "netbox_transport": "http_manage",
        "netbox_public_url": "http://127.0.0.1:18080",
        "netbox_endpoint_host": "netbox-e2e-http",
        "netbox_endpoint_port": 8000,
        "netbox_endpoint_verify_ssl": False,
        "netbox_wait_url": "http://127.0.0.1:18080/api/",
        "netbox_app_container": "netbox-e2e-http",
        "proxbox_transport": "https_nginx",
        "proxbox_docker_target": "nginx",
        "proxbox_base_url": "https://127.0.0.1:18481",
        "proxbox_host_port": 18481,
        "proxbox_curl_flags": "-k",
        "network_stack": "ipv4",
    },
    {
        "netbox_transport": "http_manage",
        "netbox_public_url": "http://127.0.0.1:18080",
        "netbox_endpoint_host": "netbox-e2e-http",
        "netbox_endpoint_port": 8000,
        "netbox_endpoint_verify_ssl": False,
        "netbox_wait_url": "http://127.0.0.1:18080/api/",
        "netbox_app_container": "netbox-e2e-http",
        "proxbox_transport": "https_granian",
        "proxbox_docker_target": "granian",
        "proxbox_base_url": "https://127.0.0.1:18481",
        "proxbox_host_port": 18481,
        "proxbox_curl_flags": "-k",
        "network_stack": "ipv4",
    },
    {
        "netbox_transport": "https_granian",
        "netbox_public_url": "https://127.0.0.1:18443",
        "netbox_endpoint_host": "netbox-e2e-granian",
        "netbox_endpoint_port": 8443,
        "netbox_endpoint_verify_ssl": True,
        "netbox_wait_url": "https://127.0.0.1:18443/api/",
        "netbox_app_container": "netbox-e2e-granian",
        "proxbox_transport": "https_granian",
        "proxbox_docker_target": "granian",
        "proxbox_base_url": "https://127.0.0.1:18481",
        "proxbox_host_port": 18481,
        "proxbox_curl_flags": "-k",
        "network_stack": "ipv4",
    },
    {
        "netbox_transport": "http_manage",
        "netbox_public_url": "http://127.0.0.1:18080",
        "netbox_endpoint_host": "netbox-e2e-http",
        "netbox_endpoint_port": 8000,
        "netbox_endpoint_verify_ssl": False,
        "netbox_wait_url": "http://127.0.0.1:18080/api/",
        "netbox_app_container": "netbox-e2e-http",
        "proxbox_transport": "http_raw",
        "proxbox_docker_target": "raw",
        "proxbox_base_url": "http://127.0.0.1:18081",
        "proxbox_host_port": 18081,
        "proxbox_curl_flags": "",
        "network_stack": "ipv4",
    },
    {
        "netbox_transport": "https_granian",
        "netbox_public_url": "https://[::1]:18443",
        "netbox_endpoint_host": "netbox-e2e-granian",
        "netbox_endpoint_port": 8443,
        "netbox_endpoint_verify_ssl": True,
        "netbox_wait_url": "https://[::1]:18443/api/",
        "netbox_app_container": "netbox-e2e-granian",
        "proxbox_transport": "http_raw",
        "proxbox_docker_target": "raw",
        "proxbox_base_url": "http://[::1]:18081",
        "proxbox_host_port": 18081,
        "proxbox_curl_flags": "",
        "network_stack": "ipv6",
    },
)


def modes_for_event(event: str, mode_input: str) -> tuple[str, ...]:
    if event == "release":
        return ("dev", "pypi")
    if event == "workflow_dispatch":
        if mode_input not in {"dev", "pypi"}:
            raise ValueError("Unsupported workflow-dispatch package mode")
        return (mode_input,)
    if event in {"push", "pull_request"}:
        return ("dev",)
    raise ValueError(f"Unsupported matrix event: {event!r}")


def generate_matrix(
    *, event: str, mode_input: str, netbox_versions: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Return a complete matrix that always stays within GitHub's hard limit."""
    modes = modes_for_event(event, mode_input)
    includes: list[dict[str, Any]] = []
    for index, entry in enumerate(BASE_TRANSPORTS):
        # A final release doubles package mode coverage. Keep every transport,
        # NetBox line, and PVE path, while running PBS/PDM on the baseline
        # transport; push/manual events retain the full historical cross-product.
        services = PROXMOX_SERVICES if event != "release" or index == 0 else ("pve",)
        for mode in modes:
            for version in netbox_versions:
                for service in services:
                    includes.append(
                        {
                            **entry,
                            "netbox_proxbox_mode": mode,
                            "netbox_version": version,
                            "proxmox_service": service,
                        }
                    )
    if not includes or len(includes) > MAX_GITHUB_MATRIX_JOBS:
        raise ValueError(f"E2E matrix has {len(includes)} jobs; limit is {MAX_GITHUB_MATRIX_JOBS}")
    return {"include": includes}


def image_matrix(*, event: str, mode_input: str) -> dict[str, list[dict[str, str]]]:
    modes = modes_for_event(event, mode_input)
    targets = sorted({entry["proxbox_docker_target"] for entry in BASE_TRANSPORTS})
    return {
        "include": [
            {"netbox_proxbox_mode": mode, "proxbox_docker_target": target}
            for mode in modes
            for target in targets
        ]
    }


def main() -> None:
    event = os.environ.get("GITHUB_EVENT_NAME", "push")
    mode_input = os.environ.get("INPUT_NETBOX_PROXBOX_MODE", "dev")
    versions = json.loads(Path(".github/netbox-versions.json").read_text(encoding="utf-8"))
    if not isinstance(versions, list) or not all(isinstance(item, str) for item in versions):
        raise ValueError("NetBox version inventory must be a list of strings")
    output = Path(os.environ["GITHUB_OUTPUT"])
    with output.open("a", encoding="utf-8") as stream:
        stream.write(
            f"matrix={json.dumps(generate_matrix(event=event, mode_input=mode_input, netbox_versions=versions))}\n"
        )
        stream.write(f"netbox_versions={json.dumps(versions)}\n")
        stream.write(f"proxmox_services={json.dumps(PROXMOX_SERVICES)}\n")
        stream.write(
            f"proxbox_image_matrix={json.dumps(image_matrix(event=event, mode_input=mode_input))}\n"
        )


if __name__ == "__main__":
    main()
