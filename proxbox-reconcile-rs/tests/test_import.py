from __future__ import annotations

import proxbox_reconcile_rs


def test_engine_version() -> None:
    assert proxbox_reconcile_rs.engine_version() == "0.1.0"


def test_build_vm_operation_queue_json_is_exported() -> None:
    assert callable(proxbox_reconcile_rs.build_vm_operation_queue_json)
