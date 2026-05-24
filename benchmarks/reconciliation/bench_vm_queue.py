"""Benchmark VM reconciliation queue engines."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.reconciliation.generate_vm_snapshot import build_vm_dataset
from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.services.sync.reconciliation.rust_bridge import (
    _input_adapter,
    _rust_build,
    build_bridge_input,
)
from proxbox_api.services.sync.reconciliation.types import PreparedVMState
from proxbox_api.services.sync.reconciliation.vm_queue import (
    _adapt_to_dataclasses,
    build_vm_operation_queue_python,
)


def main() -> None:
    """Run the benchmark and print a Markdown timing table."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--pathological", action="store_true")
    args = parser.parse_args()

    rows = []
    for size in args.sizes:
        data = build_vm_dataset(size, size, pathological=args.pathological)
        prepared_vms = [_prepared_state_from_fixture(item) for item in data["prepared_vms"]]
        snapshot = data["netbox_snapshot"]
        flags = data["flags"]

        py_ms = _measure_ms(
            lambda: build_vm_operation_queue_python(prepared_vms, snapshot, **flags),
            repeat=args.repeat,
        )

        bridge_payload = build_bridge_input(
            prepared_vms=prepared_vms,
            netbox_snapshot=snapshot,
            flags=flags,
        )
        encode_ms = _measure_ms(
            lambda: _input_adapter.dump_json(bridge_payload), repeat=args.repeat
        )

        rust_parse_diff_serialize_ms: float | None = None
        decode_ms: float | None = None
        adapter_ms: float | None = None
        full_rust_ms: float | None = None

        if _rust_build is not None:
            input_bytes = _input_adapter.dump_json(bridge_payload)
            rust_parse_diff_serialize_ms = _measure_ms(
                lambda: _rust_build(input_bytes), repeat=args.repeat
            )
            output_bytes = _rust_build(input_bytes)
            decode_ms = _measure_ms(lambda: json.loads(output_bytes), repeat=args.repeat)
            raw_ops = json.loads(output_bytes)
            adapter_ms = _measure_ms(
                lambda: _adapt_to_dataclasses(raw_ops, prepared_vms),
                repeat=args.repeat,
            )
            full_rust_ms = _measure_ms(
                lambda: _run_full_rust_path(prepared_vms, snapshot, flags),
                repeat=args.repeat,
            )

        rows.append(
            {
                "size": size,
                "snapshot": len(snapshot),
                "python_ms": py_ms,
                "encode_ms": encode_ms,
                "rust_native_ms": rust_parse_diff_serialize_ms,
                "decode_ms": decode_ms,
                "adapter_ms": adapter_ms,
                "full_rust_ms": full_rust_ms,
                "speedup": py_ms / full_rust_ms if full_rust_ms else None,
            }
        )

    _print_markdown(rows, rust_available=_rust_build is not None)


def _prepared_state_from_fixture(data: dict[str, Any]) -> PreparedVMState:
    return PreparedVMState(
        cluster_name=data["cluster_name"],
        resource=data["resource"],
        vm_config=data.get("vm_config") or {},
        vm_config_obj=ProxmoxVmConfigInput.model_validate(data.get("vm_config") or {}),
        desired_payload=data["desired_payload"],
        lookup=data.get("lookup") or {},
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        vm_type=data["vm_type"],
    )


def _run_full_rust_path(
    prepared_vms: list[PreparedVMState],
    snapshot: list[dict[str, Any]],
    flags: dict[str, bool],
) -> object:
    if _rust_build is None:
        raise RuntimeError("proxbox-reconcile-rs is not installed")
    payload = build_bridge_input(
        prepared_vms=prepared_vms,
        netbox_snapshot=snapshot,
        flags=flags,
    )
    input_bytes = _input_adapter.dump_json(payload)
    output_bytes = _rust_build(input_bytes)
    raw_ops = json.loads(output_bytes)
    return _adapt_to_dataclasses(raw_ops, prepared_vms)


def _measure_ms(callback: Callable[[], object], *, repeat: int) -> float:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        callback()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def _format_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _format_speedup(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}x"


def _print_markdown(rows: list[dict[str, float | int | None]], *, rust_available: bool) -> None:
    print("# VM Reconciliation Benchmark")
    print()
    print(f"Rust native package installed: {'yes' if rust_available else 'no'}")
    print()
    print(
        "| Prepared | Snapshot | Python diff ms | Pydantic encode ms | "
        "Rust native ms | JSON decode ms | Adapter ms | Full Rust ms | Speedup |"
    )
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        print(
            f"| {row['size']} | {row['snapshot']} | {_format_ms(row['python_ms'])} | "
            f"{_format_ms(row['encode_ms'])} | {_format_ms(row['rust_native_ms'])} | "
            f"{_format_ms(row['decode_ms'])} | {_format_ms(row['adapter_ms'])} | "
            f"{_format_ms(row['full_rust_ms'])} | {_format_speedup(row['speedup'])} |"
        )


if __name__ == "__main__":
    main()
