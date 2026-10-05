"""SDK-disabled compatibility regressions for all application factories."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("native_telemetry_disabled_probe.py")


@pytest.mark.parametrize("factory_name", ("service", "firecracker", "mock"))
@pytest.mark.parametrize("provider_mode", ("explicit", "global"))
def test_sdk_disabled_preserves_selected_providers_without_application_export(
    factory_name: str, provider_mode: str, tmp_path: Path
) -> None:
    """Run the real factory and OTLP exporters in a fresh process."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("OTEL_")}
    environment.update(
        PYTHONPATH=f"{ROOT / 'proxmox-mock'}:{ROOT}",
        PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS="1",
        PROXBOX_AUTH_LOCKOUT_HMAC_KEY="unit-test-only-auth-lockout-hmac-key-0000000000000000",
        PROXBOX_DATABASE_PATH=str(tmp_path / f"{factory_name}-{provider_mode}.db"),
        PROXBOX_GENERATED_DIR=str(tmp_path / f"generated-{factory_name}-{provider_mode}"),
        PROXBOX_SKIP_NETBOX_BOOTSTRAP="1",
        PROXBOX_ENSURE_NETBOX_OBJECTS="false",
        PROXBOX_RATE_LIMIT="999999",
    )
    result = subprocess.run(
        [sys.executable, str(PROBE), factory_name, provider_mode],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"status": "passed"' in result.stdout
