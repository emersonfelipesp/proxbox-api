"""Benchmark VM reconciliation queue engines."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.reconciliation.generate_vm_snapshot import build_vm_dataset
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxVirtualMachineCreateBody,
    ProxmoxVmConfigInput,
)
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
from proxbox_api.services.sync.vmid_helpers import extract_proxmox_endpoint_id


def main() -> None:
    """Run the benchmark and print a Markdown timing table."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--pathological", action="store_true")
    args = parser.parse_args()
    if args.repeat <= 0 or any(size <= 0 for size in args.sizes):
        parser.error("--repeat and every --sizes value must be positive")

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
        py_cpu_ms = _measure_cpu_ms(
            lambda: build_vm_operation_queue_python(prepared_vms, snapshot, **flags),
            repeat=args.repeat,
        )
        py_peak_kib = _measure_peak_kib(
            lambda: build_vm_operation_queue_python(prepared_vms, snapshot, **flags)
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

        rust_build = _rust_build
        if rust_build is not None:
            input_bytes = _input_adapter.dump_json(bridge_payload)
            rust_parse_diff_serialize_ms = _measure_ms(
                lambda: rust_build(input_bytes), repeat=args.repeat
            )
            output_bytes = rust_build(input_bytes)
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
                "python_cpu_ms": py_cpu_ms,
                "python_peak_kib": py_peak_kib,
                "encode_ms": encode_ms,
                "rust_native_ms": rust_parse_diff_serialize_ms,
                "decode_ms": decode_ms,
                "adapter_ms": adapter_ms,
                "full_rust_ms": full_rust_ms,
                "speedup": py_ms / full_rust_ms if full_rust_ms else None,
            }
        )

    _print_markdown(
        rows,
        rust_available=_rust_build is not None,
        repeat=args.repeat,
        pathological=args.pathological,
    )


def _prepared_state_from_fixture(data: dict[str, Any]) -> PreparedVMState:
    sync_state_fields = _validated_sync_state_fields(data)
    return PreparedVMState(
        cluster_name=data["cluster_name"],
        resource=data["resource"],
        vm_config=data.get("vm_config") or {},
        vm_config_obj=ProxmoxVmConfigInput.model_validate(data.get("vm_config") or {}),
        desired_payload=data["desired_payload"],
        lookup=data.get("lookup") or {},
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        vm_type=data["vm_type"],
        sync_state_fields=sync_state_fields,
        desired_state=NetBoxVirtualMachineCreateBody.model_validate(data["desired_payload"]),
    )


def _validated_sync_state_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Require the endpoint-first identity that the benchmark claims to measure."""

    sync_state_fields = data["sync_state_fields"]
    if not isinstance(sync_state_fields, dict):
        raise TypeError("sync_state_fields must be an object")
    endpoint_id = extract_proxmox_endpoint_id(sync_state_fields)
    vmid = sync_state_fields.get("proxmox_vm_id")
    vm_type = sync_state_fields.get("proxmox_vm_type")
    if endpoint_id is None or endpoint_id <= 0:
        raise ValueError("sync_state_fields requires a positive proxmox_endpoint_id")
    if not isinstance(vmid, int) or isinstance(vmid, bool) or vmid <= 0:
        raise ValueError("sync_state_fields requires a positive integer proxmox_vm_id")
    if vm_type not in {"qemu", "lxc"}:
        raise ValueError("sync_state_fields requires proxmox_vm_type qemu or lxc")
    return sync_state_fields


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


def _measure_cpu_ms(callback: Callable[[], object], *, repeat: int) -> float:
    samples = []
    for _ in range(repeat):
        start = time.process_time()
        callback()
        samples.append((time.process_time() - start) * 1000)
    return statistics.median(samples)


def _measure_peak_kib(callback: Callable[[], object]) -> float:
    tracemalloc.start()
    try:
        callback()
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak_bytes / 1024


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return "unavailable"
    commit = completed.stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    dirty = status.returncode != 0 or bool(status.stdout.strip())
    return f"{commit}{'-dirty' if dirty else ''}"


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _print_metadata(*, repeat: int, pathological: bool) -> None:
    print("## Reproducibility Metadata")
    print()
    print(f"- Commit: `{_git_commit()}`")
    print(f"- Python: `{platform.python_version()}`")
    print(f"- Platform: `{platform.platform()}`")
    print(f"- Pydantic: `{_package_version('pydantic')}`")
    print(f"- proxbox-api: `{_package_version('proxbox-api')}`")
    measured_paths = (
        "Python planner and full Rust bridge" if _rust_build is not None else "Python planner"
    )
    print(f"- Measured paths: `{measured_paths}`")
    print(f"- Repeat count: `{repeat}`")
    print(f"- Pathological workload: `{pathological}`")
    print()


def _format_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _format_speedup(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}x"


def _print_markdown(
    rows: list[dict[str, float | int | None]],
    *,
    rust_available: bool,
    repeat: int,
    pathological: bool,
) -> None:
    print("# VM Reconciliation Benchmark")
    print("\nSynthetic planner-only evidence; this is not a production sync benchmark.")
    print()
    _print_metadata(repeat=repeat, pathological=pathological)
    print(f"Rust native package installed: {'yes' if rust_available else 'no'}")
    print()
    print(
        "| Prepared | Snapshot | Python wall ms | Python CPU ms | Python peak KiB | Pydantic encode ms | "
        "Rust native ms | JSON decode ms | Adapter ms | Full Rust ms | Speedup |"
    )
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        print(
            f"| {row['size']} | {row['snapshot']} | {_format_ms(row['python_ms'])} | "
            f"{_format_ms(row['python_cpu_ms'])} | {_format_ms(row['python_peak_kib'])} | "
            f"{_format_ms(row['encode_ms'])} | {_format_ms(row['rust_native_ms'])} | "
            f"{_format_ms(row['decode_ms'])} | {_format_ms(row['adapter_ms'])} | "
            f"{_format_ms(row['full_rust_ms'])} | {_format_speedup(row['speedup'])} |"
        )


if __name__ == "__main__":
    main()
