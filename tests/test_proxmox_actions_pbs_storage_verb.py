"""Tests for the typed, create-only PBS storage operational verb."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.routes import proxmox_actions
from proxbox_api.routes.proxmox import runtime_generated
from proxbox_api.services.idempotency import get_idempotency_cache
from proxbox_api.services.proxmox_helpers import create_pbs_storage

pytestmark = pytest.mark.asyncio

SECRET = "synthetic-token-secret"
FINGERPRINT = ":".join(["AA"] * 32)


class _GateSession:
    def __init__(self, endpoint: ProxmoxEndpoint | None) -> None:
        self.endpoint = endpoint

    async def get(self, model: object, object_id: int) -> ProxmoxEndpoint | None:
        if model is ProxmoxEndpoint and self.endpoint is not None and object_id == self.endpoint.id:
            return self.endpoint
        return None


class _StorageAPI:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.error = error

    async def post(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class _ProxmoxAPI:
    def __init__(self, error: Exception | None = None) -> None:
        self.storage = _StorageAPI(error)

    @property
    def session(self) -> _ProxmoxAPI:
        return self


@pytest.fixture(autouse=True)
def _no_finalize_pause(monkeypatch):
    monkeypatch.setattr(proxmox_actions, "_PBS_FINALIZE_BACKOFF_SECONDS", (0.0, 0.0))


@pytest.fixture(autouse=True)
async def _clear_cache():
    await get_idempotency_cache().clear()
    yield
    await get_idempotency_cache().clear()


@pytest.fixture
def endpoint() -> ProxmoxEndpoint:
    return ProxmoxEndpoint(
        id=73,
        name="pve-prod (nb:419)",
        ip_address="10.0.0.10",
        port=8006,
        username="root@pam",
        verify_ssl=False,
        allow_writes=True,
    )


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "storage": "pbs-main",
        "server": "pbs.example.test",
        "port": 8007,
        "datastore": "backups",
        "namespace": "tenant-a/production",
        "username": "backup@pbs!proxbox",
        "password": SECRET,
        "fingerprint": FINGERPRINT,
        "nodes": ["pve-b", "pve-a"],
    }
    payload.update(changes)
    return payload


def _config(**changes: object) -> dict[str, object]:
    config: dict[str, object] = {
        "type": "pbs",
        "server": "pbs.example.test",
        "port": 8007,
        "datastore": "backups",
        "namespace": "tenant-a/production",
        "username": "backup@pbs!proxbox",
        "fingerprint": FINGERPRINT.lower(),
        "content": "backup",
        "prune-backups": "keep-all=1",
        "nodes": "pve-a,pve-b",
    }
    config.update(changes)
    return config


def _body(response) -> dict[str, object]:
    return json.loads(response.body)


@contextmanager
def _patched(existing: dict[str, object] | None = None, create_error: Exception | None = None):
    journal = {"id": 91, "url": "/api/extras/journal-entries/91/"}
    mocks = {
        "nb": AsyncMock(return_value=object()),
        "open": AsyncMock(return_value=object()),
        "existing": AsyncMock(return_value=existing),
        "create": AsyncMock(return_value=None, side_effect=create_error),
        "write": AsyncMock(return_value=journal),
        "update": AsyncMock(return_value=journal),
    }
    with ExitStack() as stack:
        stack.enter_context(patch.object(proxmox_actions, "get_netbox_async_session", mocks["nb"]))
        stack.enter_context(patch.object(proxmox_actions, "_open_proxmox_session", mocks["open"]))
        stack.enter_context(
            patch.object(proxmox_actions, "_existing_pbs_storage", mocks["existing"])
        )
        stack.enter_context(patch.object(proxmox_actions, "create_pbs_storage", mocks["create"]))
        stack.enter_context(
            patch.object(proxmox_actions, "write_verb_journal_entry", mocks["write"])
        )
        stack.enter_context(
            patch.object(proxmox_actions, "update_verb_journal_entry", mocks["update"])
        )
        yield mocks


async def _call(
    endpoint: ProxmoxEndpoint | None,
    payload: object,
    *,
    endpoint_id: int | None = 73,
    idempotency_key: str | None = None,
):
    return await proxmox_actions._handle_pbs_storage_create(
        _GateSession(endpoint),  # type: ignore[arg-type]
        endpoint_id,
        idempotency_key,
        "operator@example.test",
        payload,
    )


@pytest.mark.parametrize(
    ("gated_endpoint", "endpoint_id", "reason"),
    [
        (None, None, "endpoint_id_required"),
        (None, 999, "endpoint_not_found"),
        (
            ProxmoxEndpoint(
                id=73,
                name="pve-prod (nb:419)",
                ip_address="10.0.0.10",
                port=8006,
                username="root@pam",
                verify_ssl=False,
                allow_writes=False,
            ),
            73,
            "writes_disabled_for_endpoint",
        ),
    ],
)
async def test_gate_precedes_body_validation(gated_endpoint, endpoint_id, reason):
    response = await _call(gated_endpoint, {"password": SECRET}, endpoint_id=endpoint_id)
    assert response.status_code == 403
    assert _body(response)["reason"] == reason
    assert SECRET not in response.body.decode()


@pytest.mark.parametrize(
    "changes",
    [
        {"storage": "bad storage"},
        {"fingerprint": "not-a-fingerprint"},
        {"port": 65536},
        {"password": None},
        {"unknown": SECRET},
    ],
)
async def test_validation_is_secret_safe(endpoint, changes):
    with _patched() as mocks:
        response = await _call(endpoint, _payload(**changes))
    assert response.status_code == 422
    assert SECRET not in response.body.decode()
    mocks["create"].assert_not_awaited()
    mocks["open"].assert_not_awaited()
    # An invalid body is audited with one terminal entry; nothing is dispatched.
    mocks["write"].assert_awaited_once()
    mocks["update"].assert_not_awaited()
    assert mocks["write"].call_args.kwargs["kind"] == "warning"
    assert SECRET not in mocks["write"].call_args.kwargs["comments"]


async def test_secret_string_does_not_dump_or_render_plaintext():
    model = proxmox_actions.PBSStorageCreateRequest.model_validate(_payload())
    assert SECRET not in repr(model)
    assert SECRET not in str(model)
    assert SECRET not in repr(model.model_dump())


@pytest.mark.parametrize("optional", [{}, {"namespace": None, "nodes": None}])
async def test_create_posts_exact_pbs_fields(endpoint, optional):
    payload = _payload(**optional)
    with _patched() as mocks:
        response = await _call(endpoint, payload)
    assert response.status_code == 200
    assert _body(response)["result"] == "created"
    kwargs = mocks["create"].call_args.kwargs
    assert kwargs["storage"] == "pbs-main"
    assert kwargs["password"] == SECRET
    assert "type" not in kwargs and "content" not in kwargs
    mocks["create"].assert_awaited_once()
    assert mocks["write"].call_args.kwargs["assigned_object_type"] == (
        "netbox_proxbox.proxmoxendpoint"
    )
    assert mocks["write"].call_args.kwargs["assigned_object_id"] == 419


async def test_helper_posts_fixed_type_content_and_optional_fields():
    proxmox = _ProxmoxAPI()
    await create_pbs_storage(
        proxmox,  # type: ignore[arg-type]
        storage="pbs-main",
        server="pbs.example.test",
        port=8007,
        datastore="backups",
        username="backup@pbs!proxbox",
        password=SECRET,
        fingerprint=FINGERPRINT,
        namespace="tenant-a",
        nodes=["pve-a", "pve-b"],
    )
    assert proxmox.storage.calls == [
        {
            "storage": "pbs-main",
            "type": "pbs",
            "content": "backup",
            "prune-backups": "keep-all=1",
            "server": "pbs.example.test",
            "port": 8007,
            "datastore": "backups",
            "username": "backup@pbs!proxbox",
            "password": SECRET,
            "fingerprint": FINGERPRINT,
            "namespace": "tenant-a",
            "nodes": "pve-a,pve-b",
        }
    ]


async def test_helper_does_not_retain_secret_bearing_error():
    proxmox = _ProxmoxAPI(RuntimeError(f"upstream echoed {SECRET}"))
    with pytest.raises(ProxmoxAPIError) as caught:
        await create_pbs_storage(
            proxmox,  # type: ignore[arg-type]
            storage="pbs-main",
            server="pbs.example.test",
            port=8007,
            datastore="backups",
            username="backup@pbs!proxbox",
            password=SECRET,
            fingerprint=FINGERPRINT,
        )
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.python_exception is None


async def test_equal_existing_storage_is_already_present(endpoint):
    with _patched(_config()) as mocks:
        response = await _call(endpoint, _payload())
    assert response.status_code == 200
    assert _body(response)["result"] == "already_present"
    mocks["create"].assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "nfs"),
        ("server", "other.example.test"),
        ("port", 8008),
        ("datastore", "other"),
        ("namespace", "other"),
        ("username", "other@pbs!token"),
        ("fingerprint", ":".join(["BB"] * 32)),
        ("content", "images"),
        ("prune-backups", "keep-last=2"),
        ("nodes", "pve-c"),
    ],
)
async def test_existing_difference_returns_field_name_only(endpoint, field, value):
    with _patched(_config(**{field: value})) as mocks:
        response = await _call(endpoint, _payload())
    data = _body(response)
    assert response.status_code == 409
    assert data["reason"] == "storage_conflict"
    assert data["differing_fields"] == [field]
    assert str(value) not in response.body.decode()
    mocks["create"].assert_not_awaited()


async def test_dispatch_failure_is_scrubbed_everywhere(endpoint, caplog):
    error = ProxmoxAPIError(f"upstream echoed {SECRET}")
    with _patched(create_error=error) as mocks:
        response = await _call(endpoint, _payload(), idempotency_key="retry-key")
    assert response.status_code == 502
    assert _body(response)["reason"] == "proxmox_dispatch_failed"
    assert SECRET not in response.body.decode()
    assert SECRET not in caplog.text
    for call in mocks["update"].call_args_list:
        assert SECRET not in call.kwargs["comments"]
    cache = get_idempotency_cache()
    assert SECRET not in repr(cache._entries)  # noqa: SLF001


async def test_lost_answer_reads_once_and_reports_created(endpoint):
    timeout = ProxmoxAPIError("safe")
    timeout.__cause__ = TimeoutError()
    with _patched(create_error=timeout) as mocks:
        mocks["existing"].side_effect = [None, _config()]
        response = await _call(endpoint, _payload())
    assert response.status_code == 200
    assert _body(response)["result"] == "created"
    mocks["create"].assert_awaited_once()
    assert mocks["existing"].await_count == 2


async def test_lost_answer_absent_is_failure_without_retry(endpoint):
    timeout = ProxmoxAPIError("safe")
    timeout.__cause__ = TimeoutError()
    with _patched(create_error=timeout) as mocks:
        response = await _call(endpoint, _payload())
    assert response.status_code == 502
    mocks["create"].assert_awaited_once()
    assert mocks["existing"].await_count == 2


async def test_idempotency_replays_without_second_post_or_journal(endpoint):
    with _patched() as mocks:
        first = await _call(endpoint, _payload(), idempotency_key="same-key")
        second = await _call(endpoint, _payload(), idempotency_key="same-key")
    assert first.body == second.body
    mocks["create"].assert_awaited_once()
    mocks["write"].assert_awaited_once()


async def test_failed_writeahead_blocks_proxmox(endpoint):
    with _patched() as mocks:
        mocks["write"].side_effect = RuntimeError("NetBox unavailable")
        response = await _call(endpoint, _payload())
    assert response.status_code == 502
    assert _body(response)["reason"] == "netbox_journal_write_failed"
    mocks["open"].assert_not_awaited()
    mocks["create"].assert_not_awaited()


async def test_route_is_registered_without_replacing_generated_guard():
    routes = {
        (method, route.path)
        for route in proxmox_actions.router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("POST", "/storage/pbs") in routes


async def test_generated_storage_post_remains_forbidden():
    endpoint = runtime_generated._build_generated_endpoint(
        openapi_path="/storage",
        method="POST",
        operation={},
        request_model=None,
        response_model=None,
    )
    with pytest.raises(ProxboxException) as caught:
        await endpoint()
    assert caught.value.http_status_code == 403
    assert caught.value.detail == "Use a typed, audited RPC procedure for mutation operations."


async def test_model_rejects_plain_secret_omission():
    payload = _payload()
    del payload["password"]
    with pytest.raises(ValidationError):
        proxmox_actions.PBSStorageCreateRequest.model_validate(payload)


async def test_a_storage_without_an_explicit_retention_conflicts(endpoint):
    config = _config()
    del config["prune-backups"]
    with _patched(config) as mocks:
        response = await _call(endpoint, _payload())
    assert response.status_code == 409
    assert _body(response)["differing_fields"] == ["prune-backups"]
    mocks["create"].assert_not_awaited()


async def test_a_proxmox_refusal_names_status_and_parameters_without_values():
    from proxmox_sdk.sdk.exceptions import ResourceException

    refusal = ResourceException(
        status_code=400,
        status_message="Parameter verification failed",
        content=f"password {SECRET}",
        errors={"fingerprint": f"value {SECRET} is wrong", "server": "unreachable"},
    )
    proxmox = _ProxmoxAPI(refusal)
    with pytest.raises(ProxmoxAPIError) as caught:
        await create_pbs_storage(
            proxmox,  # type: ignore[arg-type]
            storage="pbs-main",
            server="pbs.example.test",
            port=8007,
            datastore="backups",
            username="backup@pbs!proxbox",
            password=SECRET,
            fingerprint=FINGERPRINT,
        )
    message = str(caught.value.message)
    assert "HTTP 400" in message and "fingerprint, server" in message
    assert SECRET not in message and SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.python_exception is None


async def test_a_dispatch_detail_keeps_the_proxmox_reason_without_the_secret(endpoint):
    error = ProxmoxAPIError(f"Proxmox refused the PBS storage (HTTP 403) {SECRET}")
    with _patched(create_error=error):
        response = await _call(endpoint, _payload())
    detail = _body(response)["detail"]
    assert response.status_code == 502 and "HTTP 403" in detail
    assert SECRET not in response.body.decode()


async def test_validation_names_the_invalid_fields_without_values(endpoint):
    with _patched() as mocks:
        response = await _call(endpoint, _payload(fingerprint=f"zz-{SECRET}", port=0))
    data = _body(response)
    assert response.status_code == 422
    assert data["reason"] == "request_validation_failed"
    assert "fingerprint" in data["detail"] and "port" in data["detail"]
    assert SECRET not in response.body.decode()
    mocks["create"].assert_not_awaited()


async def test_the_netbox_pbs_request_body_is_accepted(endpoint):
    # netbox-pbs StorageRegistration.request(): upper-case colon fingerprint, nodes as a list,
    # no namespace key for the root namespace, and no type, content or retention.
    payload = {
        "storage": "pbs-vpve",
        "server": "10.0.30.156",
        "port": 8007,
        "datastore": "datastore",
        "username": "backup@pbs!netbox",
        "password": SECRET,
        "fingerprint": ":".join(["AB"] * 32),
        "nodes": ["vpve"],
    }
    with _patched() as mocks:
        response = await _call(endpoint, payload)
    assert response.status_code == 200 and _body(response)["result"] == "created"
    kwargs = mocks["create"].call_args.kwargs
    assert kwargs["fingerprint"] == ":".join(["ab"] * 32)
    assert kwargs["namespace"] is None and kwargs["nodes"] == ["vpve"]


async def test_an_unexpected_key_never_reflects_its_name(endpoint):
    payload = _payload(**{SECRET: "x", "other": 1})
    with _patched() as mocks:
        response = await _call(endpoint, payload, idempotency_key="extra-key")
    data = _body(response)
    assert response.status_code == 422
    assert data["detail"] == "The PBS storage request body is invalid: unexpected field."
    assert SECRET not in response.body.decode()
    assert SECRET not in mocks["write"].call_args.kwargs["comments"]
    assert SECRET not in repr(get_idempotency_cache()._entries)  # noqa: SLF001
    mocks["create"].assert_not_awaited()


async def test_a_body_that_is_not_an_object_is_refused_without_reflection(endpoint):
    with _patched():
        response = await _call(endpoint, [SECRET])
    assert response.status_code == 422
    assert _body(response)["detail"] == "The PBS storage request body is invalid: body."
    assert SECRET not in response.body.decode()


async def test_a_cancelled_request_still_finalizes_and_caches_after_the_write(endpoint):
    import asyncio

    release = asyncio.Event()
    patched_update: list[str] = []

    async def slow_update(_nb, *, journal_entry_id, kind, comments):
        await release.wait()
        patched_update.append(comments)
        return {"id": journal_entry_id, "url": "/api/extras/journal-entries/91/"}

    with _patched() as mocks:
        mocks["update"].side_effect = slow_update
        request = asyncio.create_task(_call(endpoint, _payload(), idempotency_key="cancel-key"))
        for _ in range(50):
            if mocks["create"].await_count:
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        mocks["create"].assert_awaited_once()
        assert patched_update and "- result: created" in patched_update[0]
        cache = get_idempotency_cache()
        cached = [entry for key, entry in cache._entries.items() if key.key == "cancel-key"]  # noqa: SLF001
        assert cached and cached[0].response["result"] == "created"
        replay = await _call(endpoint, _payload(), idempotency_key="cancel-key")
        assert replay.status_code == 200
        mocks["create"].assert_awaited_once()


async def test_a_failed_finalization_is_retried_on_replay_without_a_second_write(endpoint):
    journal = {"id": 91, "url": "/api/extras/journal-entries/91/"}
    with _patched() as mocks:
        down = RuntimeError("NetBox is restarting")
        mocks["update"].side_effect = [down, down, down, journal]
        first = await _call(endpoint, _payload(), idempotency_key="finalize-key")
        assert first.status_code == 502
        assert _body(first)["journal_finalized"] is False
        assert _body(first)["result"] == "created"
        second = await _call(endpoint, _payload(), idempotency_key="finalize-key")
        assert second.status_code == 200
        data = _body(second)
        assert data["result"] == "created"
        assert "journal_finalized" not in data and "reason" not in data
        assert mocks["update"].await_count == 4
        mocks["create"].assert_awaited_once()
        mocks["write"].assert_awaited_once()
        first_comments = mocks["update"].await_args_list[0].kwargs["comments"]
        assert mocks["update"].await_args_list[3].kwargs["comments"] == first_comments


async def test_a_transient_journal_failure_is_retried_within_the_request(endpoint):
    journal = {"id": 91, "url": "/api/extras/journal-entries/91/"}
    with _patched() as mocks:
        mocks["update"].side_effect = [RuntimeError("NetBox is restarting"), journal]
        response = await _call(endpoint, _payload())
    assert response.status_code == 200
    data = _body(response)
    assert data["result"] == "created" and "journal_finalized" not in data
    assert mocks["update"].await_count == 2
    mocks["create"].assert_awaited_once()
