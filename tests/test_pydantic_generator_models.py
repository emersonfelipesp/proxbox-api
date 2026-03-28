from __future__ import annotations

from types import ModuleType

from proxbox_api.proxmox_codegen.pydantic_generator import (
    generate_pydantic_models_from_openapi,
)


def _load_generated_module(openapi: dict) -> ModuleType:
    code = generate_pydantic_models_from_openapi(openapi)
    module = ModuleType("tests.generated_pydantic_models")
    exec(code, module.__dict__)
    for value in module.__dict__.values():
        if (
            isinstance(value, type)
            and getattr(value, "__module__", None) == module.__name__
            and hasattr(value, "model_rebuild")
        ):
            value.model_rebuild(_types_namespace=module.__dict__)
    return module


def test_generate_pydantic_models_supports_array_scalar_and_null_responses():
    openapi = {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "test"},
        "paths": {
            "/access": {
                "get": {
                    "operationId": "get_access",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/cluster/nextid": {
                "get": {
                    "operationId": "get_cluster_nextid",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "integer",
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/access/acl": {
                "put": {
                    "operationId": "put_access_acl",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "null",
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
    }

    module = _load_generated_module(openapi)

    assert module.GetAccessResponse.model_validate(["Sys.Audit"]).root == ["Sys.Audit"]
    assert module.GetClusterNextidResponse.model_validate(101).root == 101
    assert module.PutAccessAclResponse.model_validate(None).root is None


def test_generate_pydantic_models_keeps_object_request_models_with_aliases():
    openapi = {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "test"},
        "paths": {
            "/access/acl": {
                "put": {
                    "operationId": "put_access_acl",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "groups-autocreate": {"type": "boolean"},
                                    },
                                    "required": ["path"],
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"digest": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    module = _load_generated_module(openapi)
    payload = module.PutAccessAclRequest.model_validate({"path": "/vms", "groups-autocreate": True})

    assert payload.path == "/vms"
    assert payload.groups_autocreate is True
    assert payload.model_dump(by_alias=True, exclude_none=True) == {
        "path": "/vms",
        "groups-autocreate": True,
    }


def test_generate_pydantic_models_generates_item_models_for_array_object_responses():
    openapi = {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "test"},
        "paths": {
            "/cluster/status": {
                "get": {
                    "operationId": "get_cluster_status",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "id": {"type": "string"},
                                                "name": {"type": "string"},
                                                "local": {"type": "integer"},
                                            },
                                            "required": ["id", "name"],
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    module = _load_generated_module(openapi)

    payload = module.GetClusterStatusResponse.model_validate(
        [{"id": "node/pve1", "name": "pve1", "local": 1}]
    )

    assert hasattr(module, "GetClusterStatusResponseItem")
    assert payload.root[0].id == "node/pve1"
    assert payload.root[0].name == "pve1"
    assert payload.root[0].local == 1


def test_generate_pydantic_models_renames_python_keyword_fields():
    openapi = {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "test"},
        "paths": {
            "/pci": {
                "get": {
                    "operationId": "get_pci",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "class": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }

    module = _load_generated_module(openapi)
    payload = module.GetPciResponse.model_validate({"class": "network"})

    assert payload.class_ == "network"
    assert payload.model_dump(by_alias=True, exclude_none=True) == {"class": "network"}
