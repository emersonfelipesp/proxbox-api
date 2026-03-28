"""Basic API smoke tests for the FastAPI root endpoint."""

from pathlib import Path

from fastapi.testclient import TestClient

from proxbox_api.main import app
from proxbox_api.proxmox_codegen.normalize import normalize_captured_endpoints
from proxbox_api.proxmox_codegen.openapi_generator import generate_openapi_schema
from proxbox_api.proxmox_codegen.pydantic_generator import (
    generate_pydantic_models_from_openapi,
)
from proxbox_api.proxmox_codegen.pipeline import generate_proxmox_codegen_bundle

client = TestClient(app)


def test_read_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {
        "message": "Proxbox Backend made in FastAPI framework",
        "proxbox": {
            "github": "https://github.com/netdevopsbr/netbox-proxbox",
            "docs": "https://docs.netbox.dev.br",
        },
        "fastapi": {
            "github": "https://github.com/tiangolo/fastapi",
            "website": "https://fastapi.tiangolo.com/",
            "reason": "FastAPI was chosen because of performance and reliability.",
        },
    }


def test_custom_openapi_contains_embedded_proxmox_extension():
    schema = app.openapi()
    assert isinstance(schema, dict)
    assert "x-proxmox-generated-openapi" in schema


def test_openapi_generation_pipeline_from_sample_capture():
    sample_capture = {
        "/version": {
            "path": "/version",
            "text": "version",
            "methods": {
                "GET": {
                    "method": "GET",
                    "path": "/version",
                    "method_name": "version",
                    "description": "Get version",
                    "parameters": {"type": "object", "properties": {}},
                    "returns": {
                        "type": "object",
                        "properties": {
                            "version": {
                                "type": "string",
                                "description": "Version string",
                                "optional": 0,
                            }
                        },
                    },
                    "permissions": {"user": "all"},
                    "allowtoken": 1,
                    "protected": 0,
                    "unstable": 0,
                    "raw_sections": [],
                    "source": "test",
                }
            },
        }
    }

    operations = normalize_captured_endpoints(sample_capture)
    assert len(operations) == 1

    openapi = generate_openapi_schema(operations)
    assert openapi["openapi"] == "3.1.0"
    assert "/version" in openapi["paths"]
    assert "get" in openapi["paths"]["/version"]

    model_code = generate_pydantic_models_from_openapi(openapi)
    assert "class GetVersionResponse" in model_code


def test_generate_bundle_persists_artifacts(tmp_path: Path, monkeypatch):
    import proxbox_api.proxmox_codegen.pipeline as pipeline

    async def _fake_crawl(
        url=":unused:",
        worker_count=10,
        retry_count=2,
        retry_backoff_seconds=0.35,
        checkpoint_path=None,
        checkpoint_every=50,
    ):
        if checkpoint_path:
            Path(checkpoint_path).write_text("{}", encoding="utf-8")
        return {
            "source": "playwright",
            "url": url,
            "worker_count": worker_count,
            "retry_count": retry_count,
            "retry_backoff_seconds": retry_backoff_seconds,
            "checkpoint_path": checkpoint_path,
            "checkpoint_every": checkpoint_every,
            "endpoint_count": 1,
            "discovered_navigation_items": 1,
            "method_count": 1,
            "failed_endpoint_count": 0,
            "failures": {},
            "duration_seconds": 0.01,
            "endpoints": {
                "/version": {
                    "path": "/version",
                    "text": "version",
                    "depth": 0,
                    "methods": {
                        "GET": {
                            "method": "GET",
                            "path": "/version",
                            "method_name": "version",
                            "description": "Get version",
                            "parameters": {"type": "object", "properties": {}},
                            "returns": {
                                "type": "object",
                                "properties": {
                                    "version": {"type": "string", "optional": 0}
                                },
                            },
                            "permissions": {"user": "all"},
                            "allowtoken": 1,
                            "protected": 0,
                            "unstable": 0,
                            "raw_sections": ["properties: {}"],
                            "source": "viewer",
                        }
                    },
                }
            },
        }

    monkeypatch.setattr(
        pipeline,
        "crawl_proxmox_api_viewer_async",
        _fake_crawl,
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_apidoc_js",
        lambda: (
            'const apiSchema = [{"path":"/version","text":"version","leaf":1,"info":{"GET":{"name":"version","parameters":{"additionalProperties":0},"returns":{"type":"object"}}}}];'
        ),
    )

    bundle = generate_proxmox_codegen_bundle(
        output_dir=tmp_path,
        worker_count=4,
        retry_count=1,
        retry_backoff_seconds=0.1,
        checkpoint_every=10,
    )
    assert bundle.endpoint_count > 0
    assert bundle.operation_count > 0
    assert (tmp_path / "raw_capture.json").exists()
    assert (tmp_path / "openapi.json").exists()
    assert (tmp_path / "pydantic_models.py").exists()
    assert (tmp_path / "crawl_checkpoint.json").exists()
