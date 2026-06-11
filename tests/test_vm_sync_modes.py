"""Tests for VM and VM-template sync mode filtering.

Covers the pure helper ``_vm_resource_allowed_by_sync_modes`` and the
normalizer ``_normalize_sync_mode`` exported from
``proxbox_api.routes.virtualization.virtual_machines.sync_vm``.
"""

from __future__ import annotations

from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    _normalize_sync_mode,
    _vm_resource_allowed_by_sync_modes,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VM_RESOURCE = {"type": "qemu", "vmid": 100, "name": "my-vm", "template": 0}
TEMPLATE_RESOURCE_INT = {"type": "qemu", "vmid": 101, "name": "tpl-vm", "template": 1}
TEMPLATE_RESOURCE_STR = {"type": "qemu", "vmid": 102, "name": "tpl-str", "template": "1"}
TEMPLATE_RESOURCE_BOOL = {"type": "qemu", "vmid": 103, "name": "tpl-bool", "template": True}
TEMPLATE_RESOURCE_ZERO = {"type": "qemu", "vmid": 104, "name": "not-tpl", "template": 0}
TEMPLATE_RESOURCE_STR_ZERO = {"type": "qemu", "vmid": 105, "name": "not-tpl-str", "template": "0"}
NO_TEMPLATE_KEY = {"type": "qemu", "vmid": 106, "name": "no-key"}


# ---------------------------------------------------------------------------
# _normalize_sync_mode
# ---------------------------------------------------------------------------


def test_normalize_valid_always():
    assert _normalize_sync_mode("always", "sync_mode_vm") == "always"


def test_normalize_valid_bootstrap_only():
    assert _normalize_sync_mode("bootstrap_only", "sync_mode_vm") == "bootstrap_only"


def test_normalize_valid_disabled():
    assert _normalize_sync_mode("disabled", "sync_mode_vm_template") == "disabled"


def test_normalize_unknown_falls_back_to_always():
    # The proxbox logger has propagate=False and writes to its own stderr handler.
    # We only assert the behavioral contract: unknown modes fall back to "always".
    result = _normalize_sync_mode("invalid_mode", "sync_mode_vm")
    assert result == "always"


def test_normalize_unknown_logs_warning(monkeypatch):
    warnings_logged = []
    import proxbox_api.routes.virtualization.virtual_machines.sync_vm as sync_vm_module

    class _MockLogger:
        def warning(self, msg, *a, **kw):
            warnings_logged.append(msg % a)

        def debug(self, *a, **kw):
            pass

        def info(self, *a, **kw):
            pass

    monkeypatch.setattr(sync_vm_module, "logger", _MockLogger())
    result = sync_vm_module._normalize_sync_mode("bogus_mode", "sync_mode_vm")
    assert result == "always"
    assert any("bogus_mode" in w for w in warnings_logged)


def test_normalize_empty_string_falls_back_to_always(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        result = _normalize_sync_mode("", "sync_mode_vm")
    assert result == "always"


# ---------------------------------------------------------------------------
# _vm_resource_allowed_by_sync_modes — defaults (always + always)
# ---------------------------------------------------------------------------


def test_default_allows_regular_vm():
    assert _vm_resource_allowed_by_sync_modes(VM_RESOURCE, "always", "always") is True


def test_default_allows_template_vm():
    assert _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_INT, "always", "always") is True


def test_default_allows_no_template_key():
    assert _vm_resource_allowed_by_sync_modes(NO_TEMPLATE_KEY, "always", "always") is True


# ---------------------------------------------------------------------------
# sync_mode_vm = "disabled" — regular VMs excluded, templates unaffected
# ---------------------------------------------------------------------------


def test_vm_disabled_excludes_regular_vm():
    assert _vm_resource_allowed_by_sync_modes(VM_RESOURCE, "disabled", "always") is False


def test_vm_disabled_allows_template():
    assert _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_INT, "disabled", "always") is True


# ---------------------------------------------------------------------------
# sync_mode_vm_template = "disabled" — templates excluded, regular VMs unaffected
# ---------------------------------------------------------------------------


def test_template_disabled_excludes_template_int():
    assert _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_INT, "always", "disabled") is False


def test_template_disabled_excludes_template_str():
    assert _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_STR, "always", "disabled") is False


def test_template_disabled_excludes_template_bool():
    assert _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_BOOL, "always", "disabled") is False


def test_template_disabled_allows_regular_vm():
    assert _vm_resource_allowed_by_sync_modes(VM_RESOURCE, "always", "disabled") is True


def test_template_disabled_allows_no_template_key():
    assert _vm_resource_allowed_by_sync_modes(NO_TEMPLATE_KEY, "always", "disabled") is True


# ---------------------------------------------------------------------------
# template=0 / "0" — these are NOT templates
# ---------------------------------------------------------------------------


def test_template_zero_not_treated_as_template_when_tpl_disabled():
    """A resource with template=0 is a regular VM; should pass when only VM mode is 'always'."""
    assert _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_ZERO, "always", "disabled") is True


def test_template_str_zero_not_treated_as_template():
    assert (
        _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_STR_ZERO, "always", "disabled") is True
    )


# ---------------------------------------------------------------------------
# bootstrap_only — treated as enabled (same as always at the backend)
# ---------------------------------------------------------------------------


def test_bootstrap_only_allows_regular_vm():
    assert _vm_resource_allowed_by_sync_modes(VM_RESOURCE, "bootstrap_only", "always") is True


def test_bootstrap_only_template_mode_allows_template():
    assert (
        _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_INT, "always", "bootstrap_only")
        is True
    )


def test_both_bootstrap_only():
    assert (
        _vm_resource_allowed_by_sync_modes(VM_RESOURCE, "bootstrap_only", "bootstrap_only") is True
    )
    assert (
        _vm_resource_allowed_by_sync_modes(
            TEMPLATE_RESOURCE_BOOL, "bootstrap_only", "bootstrap_only"
        )
        is True
    )


# ---------------------------------------------------------------------------
# both disabled
# ---------------------------------------------------------------------------


def test_both_disabled_excludes_everything():
    assert _vm_resource_allowed_by_sync_modes(VM_RESOURCE, "disabled", "disabled") is False
    assert (
        _vm_resource_allowed_by_sync_modes(TEMPLATE_RESOURCE_INT, "disabled", "disabled") is False
    )
