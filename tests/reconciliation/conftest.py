"""Test-only reconciliation engine selection through plugin-setting semantics."""

from __future__ import annotations

import os

import pytest

from proxbox_api import runtime_settings


@pytest.fixture(autouse=True)
def _test_reconciliation_plugin_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = os.environ.get("PROXBOX_TEST_RECONCILIATION_ENGINE")
    strict = os.environ.get("PROXBOX_TEST_RECONCILIATION_COMPARE_STRICT")
    if engine is None and strict is None:
        return
    settings: dict[str, object] = {}
    if engine is not None:
        settings["reconciliation_engine"] = engine
    if strict is not None:
        settings["reconciliation_compare_strict"] = strict.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    monkeypatch.setattr(runtime_settings, "_load_settings", lambda: settings)
