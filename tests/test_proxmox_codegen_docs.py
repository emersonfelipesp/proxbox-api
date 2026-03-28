from __future__ import annotations

from proxbox_api.proxmox_codegen.crawler import _normalize_doc_sections
from proxbox_api.proxmox_codegen.normalize import normalize_captured_endpoints
from proxbox_api.proxmox_codegen.openapi_generator import generate_openapi_schema


def test_normalize_doc_sections_preserves_paragraph_breaks():
    sections = _normalize_doc_sections(
        [
            {
                "heading": "Description",
                "body": "Get role configuration.\n\nThis role can be audited.",
            },
            {
                "heading": "Usage",
                "body": "HTTP: GET /api2/json/access/roles/{roleid}\n\nCLI: pvesh get /access/roles/{roleid}",
            },
        ]
    )

    assert sections["Description"] == "Get role configuration.\n\nThis role can be audited."
    assert (
        sections["Usage"]
        == "HTTP: GET /api2/json/access/roles/{roleid}\n\nCLI: pvesh get /access/roles/{roleid}"
    )


def test_normalize_captured_endpoints_prefers_viewer_sections_in_description():
    operations = normalize_captured_endpoints(
        {
            "/access/roles/{roleid}": {
                "methods": {
                    "GET": {
                        "method": "GET",
                        "path": "/access/roles/{roleid}",
                        "method_name": "read_role",
                        "description": "Short fallback text.",
                        "viewer_description": "Get role configuration.",
                        "viewer_usage": "HTTP: GET /api2/json/access/roles/{roleid}\nCLI: pvesh get /access/roles/{roleid}",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "roleid": {
                                    "type": "string",
                                    "description": "Role identifier.",
                                    "optional": False,
                                }
                            },
                        },
                        "returns": {"type": "object", "properties": {"roleid": {"type": "string"}}},
                        "permissions": {"user": "all"},
                        "raw_sections": [],
                        "source": "viewer",
                    }
                }
            }
        }
    )

    operation = operations[0]
    assert (
        operation.description
        == "Get role configuration.\n\n## Usage\nHTTP: GET /api2/json/access/roles/{roleid}\nCLI: pvesh get /access/roles/{roleid}"
    )
    assert operation.extra["viewer_sections"]["description"] == "Get role configuration."
    assert operation.extra["viewer_sections"]["short_description"] == "Short fallback text."


def test_generate_openapi_schema_includes_usage_in_operation_description():
    operations = normalize_captured_endpoints(
        {
            "/access/roles/{roleid}": {
                "methods": {
                    "GET": {
                        "method": "GET",
                        "path": "/access/roles/{roleid}",
                        "method_name": "read_role",
                        "description": "Get role configuration.",
                        "viewer_description": "Get role configuration.",
                        "viewer_usage": "CLI: pvesh get /access/roles/{roleid}",
                        "parameters": {
                            "type": "object",
                            "properties": {"roleid": {"type": "string", "optional": False}},
                        },
                        "returns": {"type": "object"},
                        "permissions": {"user": "all"},
                        "raw_sections": [],
                        "source": "viewer",
                    }
                }
            }
        }
    )

    schema = generate_openapi_schema(operations, version="test")
    get_role = schema["paths"]["/access/roles/{roleid}"]["get"]

    assert "## Usage" in get_role["description"]
    assert "pvesh get /access/roles/{roleid}" in get_role["description"]
    assert (
        get_role["x-proxmox"]["viewer_sections"]["usage"] == "CLI: pvesh get /access/roles/{roleid}"
    )
