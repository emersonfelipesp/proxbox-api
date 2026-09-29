"""Contracts for the reconciliation benchmark harness."""

import subprocess

import pytest

from benchmarks.reconciliation import bench_vm_queue
from benchmarks.reconciliation.bench_vm_queue import _prepared_state_from_fixture
from benchmarks.reconciliation.generate_vm_snapshot import build_vm_dataset


def test_benchmark_prepared_state_preserves_endpoint_identity() -> None:
    fixture = build_vm_dataset(1)["prepared_vms"][0]

    prepared = _prepared_state_from_fixture(fixture)

    assert prepared.sync_state_fields == fixture["sync_state_fields"]
    assert prepared.sync_state_fields["proxmox_endpoint_id"] == 500
    assert prepared.desired_state is not None
    assert prepared.desired_state.name == fixture["desired_payload"]["name"]


@pytest.mark.parametrize(
    "sync_state_fields",
    [
        None,
        {},
        {"proxmox_endpoint_id": 0, "proxmox_vm_id": 1000, "proxmox_vm_type": "qemu"},
        {"proxmox_endpoint_id": 500, "proxmox_vm_id": "bad", "proxmox_vm_type": "qemu"},
        {"proxmox_endpoint_id": 500, "proxmox_vm_id": 1000, "proxmox_vm_type": "bad"},
    ],
)
def test_benchmark_prepared_state_rejects_invalid_endpoint_identity(
    sync_state_fields: object,
) -> None:
    fixture = build_vm_dataset(1)["prepared_vms"][0]
    fixture["sync_state_fields"] = sync_state_fields

    with pytest.raises((TypeError, ValueError, KeyError)):
        _prepared_state_from_fixture(fixture)


def test_git_commit_marks_staged_and_untracked_changes_dirty(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "benchmark@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Benchmark Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    monkeypatch.setattr(bench_vm_queue, "REPO_ROOT", tmp_path)

    assert not bench_vm_queue._git_commit().endswith("-dirty")
    tracked.write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    assert bench_vm_queue._git_commit().endswith("-dirty")
    subprocess.run(["git", "restore", "--staged", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "restore", "tracked.txt"], cwd=tmp_path, check=True)
    (tmp_path / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    assert bench_vm_queue._git_commit().endswith("-dirty")
