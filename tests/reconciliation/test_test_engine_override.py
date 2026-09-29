"""Test-only plugin-setting override contract for strict compare runs."""

import os

from proxbox_api.services.sync.reconciliation.vm_queue import (
    _reconciliation_compare_strict,
    _reconciliation_engine,
)


def test_test_override_uses_plugin_setting_resolution() -> None:
    if os.environ.get("PROXBOX_TEST_RECONCILIATION_ENGINE") is None:
        assert _reconciliation_engine() == "python"
        assert _reconciliation_compare_strict() is False
        return

    assert _reconciliation_engine() == "compare"
    assert _reconciliation_compare_strict() is True
