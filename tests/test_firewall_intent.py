"""Tests for firewall intent plan/apply helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from proxbox_api.routes.intent.dispatchers.common import IntentEndpointContext
from proxbox_api.routes.intent.schemas import ApplyDiff, FirewallIntentPayload, IntentDiff
from proxbox_api.schemas.firewall import FirewallWriteResponse
from proxbox_api.services import firewall_intent


def test_firewall_plan_diff_returns_warning_verdict():
    diff = IntentDiff(
        op="create",
        kind="firewall",
        netbox_id=123,
        type="firewall.rule.create",
    )

    verdict = firewall_intent.plan_firewall_diff(diff)

    assert verdict.verdict == "warning"
    assert verdict.reason == "firewall_plan_preview"
    assert "Firewall action" in verdict.message


@pytest.mark.asyncio
async def test_apply_firewall_diff_dispatches_write_and_returns_upid(monkeypatch):
    calls = []

    async def _write(**kwargs):
        calls.append(kwargs)
        return FirewallWriteResponse(
            status="pushed",
            endpoint_id=1,
            actor="alice",
            path=kwargs["path"],
            proxmox_task_id="UPID:pve:1",
        )

    monkeypatch.setattr(firewall_intent, "_firewall_write", _write)
    diff = ApplyDiff(
        op="create",
        kind="firewall",
        netbox_id=123,
        payload=FirewallIntentPayload(
            action="firewall.rule.create",
            zone="node",
            node="pve-a",
            body={"type": "in", "action": "ACCEPT"},
        ),
    )

    result = await firewall_intent.apply_firewall_diff(
        diff=diff,
        endpoint_context=IntentEndpointContext(
            session=SimpleNamespace(),
            endpoint_id=1,
            netbox_id=123,
        ),
        actor="alice",
        run_uuid="run-1",
    )

    assert calls[0]["method"] == "post"
    assert calls[0]["path"] == "nodes/pve-a/firewall/rules"
    assert calls[0]["payload"] == {"type": "in", "action": "ACCEPT"}
    assert result.status == "succeeded"
    assert result.proxmox_upid == "UPID:pve:1"


@pytest.mark.asyncio
async def test_apply_firewall_diff_surfaces_write_gate_json(monkeypatch):
    async def _write(**_kwargs):
        return JSONResponse(
            status_code=403,
            content={
                "reason": "writes_disabled_for_endpoint",
                "detail": "writes are disabled",
            },
        )

    monkeypatch.setattr(firewall_intent, "_firewall_write", _write)
    diff = ApplyDiff(
        op="update",
        kind="firewall",
        netbox_id=123,
        payload=FirewallIntentPayload(
            action="firewall.options.update",
            zone="datacenter",
            body={"enable": True},
        ),
    )

    result = await firewall_intent.apply_firewall_diff(
        diff=diff,
        endpoint_context=IntentEndpointContext(session=SimpleNamespace(), endpoint_id=1),
        actor="alice",
        run_uuid="run-1",
    )

    assert result.status == "failed"
    assert result.reason == "writes_disabled_for_endpoint"
    assert result.message == "writes are disabled"


@pytest.mark.asyncio
async def test_apply_firewall_diff_maps_vnet_unsupported_to_skipped(monkeypatch):
    async def _write(**kwargs):
        return FirewallWriteResponse(
            status="skipped",
            endpoint_id=1,
            actor="alice",
            path=kwargs["path"],
            reason="vnet_firewall_not_supported",
            detail="Upstream Proxmox returned HTTP 501.",
        )

    monkeypatch.setattr(firewall_intent, "_firewall_write", _write)
    diff = ApplyDiff(
        op="create",
        kind="firewall",
        netbox_id=123,
        payload=FirewallIntentPayload(
            action="firewall.rule.create",
            zone="vnet",
            vnet="tenant-vnet",
            body={"type": "forward", "action": "ACCEPT"},
        ),
    )

    result = await firewall_intent.apply_firewall_diff(
        diff=diff,
        endpoint_context=IntentEndpointContext(session=SimpleNamespace(), endpoint_id=1),
        actor="alice",
        run_uuid="run-1",
    )

    assert result.status == "skipped"
    assert result.reason == "vnet_firewall_not_supported"
