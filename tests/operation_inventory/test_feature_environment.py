"""Feature selection must never leak out of collection into the host process."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from proxbox_api.operation_inventory import collection
from proxbox_api.operation_inventory.inputs import load_inputs
from proxbox_api.routes.proxmox import runtime_generated

ROOT = Path(__file__).absolute().parents[2]


def test_selected_features_restores_the_previous_value(monkeypatch):
    monkeypatch.setenv("PROXBOX_FEATURES", "core")
    with collection.selected_features("core,pbs"):
        assert os.environ["PROXBOX_FEATURES"] == "core,pbs"
    assert os.environ["PROXBOX_FEATURES"] == "core"


def test_selected_features_restores_an_absent_variable(monkeypatch):
    monkeypatch.delenv("PROXBOX_FEATURES", raising=False)
    with collection.selected_features("core,ceph"):
        assert os.environ["PROXBOX_FEATURES"] == "core,ceph"
    assert "PROXBOX_FEATURES" not in os.environ


def test_selected_features_restores_after_a_failure(monkeypatch):
    monkeypatch.setenv("PROXBOX_FEATURES", "core")
    with pytest.raises(RuntimeError, match="composition failed"):
        with collection.selected_features("core,pdm"):
            raise RuntimeError("composition failed")
    assert os.environ["PROXBOX_FEATURES"] == "core"


def test_collect_mode_restores_the_previous_value_after_a_failure(monkeypatch):
    inputs = load_inputs(ROOT)
    monkeypatch.setenv("PROXBOX_FEATURES", "core")

    def refuse(*args, **kwargs):
        raise RuntimeError("route registration failed")

    monkeypatch.setattr(runtime_generated, "register_generated_proxmox_routes", refuse)
    with pytest.raises(RuntimeError, match="route registration failed"):
        collection._collect_mode(ROOT, inputs.modes[8], {}, {}, {})
    assert os.environ["PROXBOX_FEATURES"] == "core"
