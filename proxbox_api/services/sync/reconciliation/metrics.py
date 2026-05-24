"""Lightweight metrics for reconciliation engine observability."""

from __future__ import annotations

_reconcile_mismatch_total = 0


def increment_reconciliation_mismatch_total() -> None:
    """Record one Rust/Python reconciliation output mismatch."""

    global _reconcile_mismatch_total
    _reconcile_mismatch_total += 1


def reset_reconciliation_metrics() -> None:
    """Reset reconciliation metrics for tests."""

    global _reconcile_mismatch_total
    _reconcile_mismatch_total = 0


def get_reconciliation_metrics() -> dict[str, int]:
    """Return reconciliation metrics using the public metric names."""

    return {"proxbox_reconcile_mismatch_total": _reconcile_mismatch_total}


def get_reconciliation_prometheus_metrics() -> str:
    """Return reconciliation metrics in Prometheus text exposition format."""

    metrics = get_reconciliation_metrics()
    lines = [
        "# HELP proxbox_reconcile_mismatch_total Total Rust/Python reconciliation mismatches",
        "# TYPE proxbox_reconcile_mismatch_total counter",
        f"proxbox_reconcile_mismatch_total {metrics['proxbox_reconcile_mismatch_total']}",
    ]
    return "\n".join(lines) + "\n"
