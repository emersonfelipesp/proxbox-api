from __future__ import annotations

import proxbox_reconcile_rs


def test_engine_version() -> None:
    assert proxbox_reconcile_rs.engine_version() == "0.1.0"
