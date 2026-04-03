"""Compatibility wrappers backed by netbox-sdk facade operations."""

from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel, ConfigDict, RootModel

from proxbox_api.netbox_sdk_helpers import ensure_record, to_dict


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "placeholder"


def _run(coro: object) -> object:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {"value": None, "error": None}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as error:
            result["error"] = error

    import threading

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if result["error"]:
        raise result["error"]
    return result["value"]


class GenericSchema(BaseModel):
    """Loose schema type used by legacy custom object models."""

    model_config = ConfigDict(extra="allow")


class NetBoxBase:
    """Global netbox-sdk facade holder for compatibility wrappers."""

    nb: object = None


class _BaseCompat:
    endpoint_getter: str = ""

    class Schema(BaseModel):
        model_config = ConfigDict(extra="allow")

    class SchemaList(RootModel[list[Schema]]):
        pass

    def __init__(self, bootstrap_placeholder: bool = False, **payload: object) -> None:
        self._record: object = None
        if bootstrap_placeholder:
            self._record = self._bootstrap_placeholder()
            return
        if payload:
            self._record = self._create_or_get(payload)

    @classmethod
    def _endpoint(cls, nb: object) -> object:
        endpoint = nb
        for piece in cls.endpoint_getter.split("."):
            endpoint = getattr(endpoint, piece)
        return endpoint

    def _nb(self) -> object:
        if NetBoxBase.nb is None:
            raise RuntimeError("NetBoxBase.nb is not initialized")
        return NetBoxBase.nb

    def _lookup(self, payload: dict[str, object]) -> dict[str, object]:
        if "slug" in payload:
            return {"slug": payload["slug"]}
        if "name" in payload:
            return {"name": payload["name"]}
        return payload

    def _create_or_get(self, payload: dict[str, object]) -> object:
        async def _op() -> object:
            endpoint = self._endpoint(self._nb())
            return await ensure_record(endpoint, self._lookup(payload), payload)

        return _run(_op())

    def all(self) -> list[dict[str, object]]:
        async def _op() -> list[dict[str, object]]:
            endpoint = self._endpoint(self._nb())
            results = []
            async for item in endpoint.all():
                results.append(to_dict(item))
            return results

        return _run(_op())

    def find(self, **kwargs: object) -> dict[str, object] | None:
        async def _op() -> dict[str, object] | None:
            endpoint = self._endpoint(self._nb())
            # Handle 'id' specially: pass as positional arg to use detail endpoint
            if "id" in kwargs:
                result = await endpoint.get(kwargs.pop("id"), **kwargs)
            else:
                result = await endpoint.get(**kwargs)
            return to_dict(result) if result else None

        return _run(_op())

    def _bootstrap_placeholder(self) -> object:
        return None

    @property
    def id(self) -> object:
        return getattr(self._record, "id", None)

    @property
    def json(self) -> dict[str, object]:
        return to_dict(self._record)

    def dict(self) -> dict[str, object]:
        return self.json

    def get(self, key: str, default: object = None) -> object:
        return self.json.get(key, default)

    def __getattr__(self, name: str) -> object:
        if self._record is None:
            raise AttributeError(name)
        return getattr(self._record, name)

    def __bool__(self) -> bool:
        return bool(self._record)


class Tags(_BaseCompat):
    endpoint_getter = "extras.tags"


class CustomField(_BaseCompat):
    endpoint_getter = "extras.custom_fields"


class ClusterType(_BaseCompat):
    endpoint_getter = "virtualization.cluster_types"


class Cluster(_BaseCompat):
    endpoint_getter = "virtualization.clusters"


class Site(_BaseCompat):
    endpoint_getter = "dcim.sites"

    def _bootstrap_placeholder(self) -> object:
        payload = {
            "name": "Proxmox Default Site",
            "slug": "proxmox-default-site",
            "status": "active",
        }
        return self._create_or_get(payload)


class DeviceRole(_BaseCompat):
    endpoint_getter = "dcim.device_roles"

    def _bootstrap_placeholder(self) -> object:
        payload = {
            "name": "Proxmox Node",
            "slug": "proxmox-node",
            "color": "00bcd4",
        }
        return self._create_or_get(payload)


class Manufacturer(_BaseCompat):
    endpoint_getter = "dcim.manufacturers"


class DeviceType(_BaseCompat):
    endpoint_getter = "dcim.device_types"

    def _bootstrap_placeholder(self) -> object:
        manufacturer = Manufacturer(name="Proxmox", slug="proxmox")
        payload = {
            "model": "Proxmox Generic Device",
            "slug": "proxmox-generic-device",
            "manufacturer": getattr(manufacturer, "id", None),
        }
        return self._create_or_get(payload)

    def _lookup(self, payload: dict[str, object]) -> dict[str, object]:
        if "model" in payload:
            return {"model": payload["model"]}
        return super()._lookup(payload)


class Device(_BaseCompat):
    endpoint_getter = "dcim.devices"

    def _lookup(self, payload: dict[str, object]) -> dict[str, object]:
        if "name" in payload:
            return {"name": payload["name"]}
        return super()._lookup(payload)


class Interface(_BaseCompat):
    endpoint_getter = "dcim.interfaces"

    def _lookup(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("device") and payload.get("name"):
            return {"device_id": payload["device"], "name": payload["name"]}
        return super()._lookup(payload)


class VMInterface(_BaseCompat):
    endpoint_getter = "virtualization.interfaces"

    def _lookup(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("virtual_machine") and payload.get("name"):
            return {
                "virtual_machine_id": payload["virtual_machine"],
                "name": payload["name"],
            }
        return super()._lookup(payload)


class IPAddress(_BaseCompat):
    endpoint_getter = "ipam.ip_addresses"

    def _lookup(self, payload: dict[str, object]) -> dict[str, object]:
        if "address" in payload:
            return {"address": payload["address"]}
        return super()._lookup(payload)


class VirtualMachine(_BaseCompat):
    endpoint_getter = "virtualization.virtual_machines"

    status_field = {
        "running": "active",
        "stopped": "offline",
        "paused": "offline",
        "active": "active",
        "online": "active",
    }

    def _lookup(self, payload: dict[str, object]) -> dict[str, object]:
        custom_fields = payload.get("custom_fields", {})
        vmid = custom_fields.get("proxmox_vm_id") if isinstance(custom_fields, dict) else None
        if vmid is not None:
            return {"cf_proxmox_vm_id": int(vmid)}
        if "name" in payload:
            return {"name": payload["name"]}
        return super()._lookup(payload)
