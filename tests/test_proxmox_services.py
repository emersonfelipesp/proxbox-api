"""Tests for the read-only Proxmox systemd service-monitoring feature.

Covers three layers, matching ``proxbox_api/services/proxmox_services.py``,
``proxbox_api/schemas/proxmox_services.py``, and
``proxbox_api/routes/proxmox/services.py``:

- Pure parsing of captured ``systemctl show`` output (multi-unit,
  active/exited, failed with an exit code, failed on timeout, and a
  not-installed unit).
- Unit validation rejection and acceptance (regex, ``..``, length,
  configured-unit support, per-request unit cap).
- The ``GET /proxmox/services/systemd`` route contract: auth, the 4xx misuse
  contract (invalid units, every credential-resolution failure branch), the
  ``reachable=false`` monitoring result, and the 200 happy-path shape.

No test ever opens a real SSH connection: route tests monkeypatch
``_fetch_endpoint_credential`` and ``run_endpoint_command`` on the route
module's own namespace (``proxbox_api.routes.proxmox.services``), while the
one ``run_endpoint_command`` unit test swaps in a fake AsyncSSH module.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import urllib.error
from pathlib import Path

import pytest

from proxbox_api.services.proxmox_services import (
    PROXMOX_MONITORED_UNITS_DEFAULT,
    UnitValidationError,
    build_systemctl_show_command,
    parse_requested_units,
    parse_systemctl_show_output,
)
from proxbox_api.services.ssh_terminal import (
    CompletedCommand,
    SSHCommandError,
    SSHCommandTimeoutError,
    TerminalCredential,
    TerminalCredentialError,
    run_endpoint_command,
)

_FINGERPRINT = "SHA256:abcdefghijklmnopqrstuvwxyz12345678901234567"

# ---------------------------------------------------------------------------
# Captured `systemctl show -p Id -p LoadState -p ActiveState -p SubState
# -p Result -p MainPID -p ExecMainCode -p ExecMainStatus -p NRestarts
# -p ActiveEnterTimestamp -p UnitFileState -- <unit>` output, one block per
# unit state this feature must distinguish.
# ---------------------------------------------------------------------------

ACTIVE_BLOCK = """Id=pveproxy.service
LoadState=loaded
ActiveState=active
SubState=running
Result=success
MainPID=1234
ExecMainCode=0
ExecMainStatus=0
NRestarts=0
ActiveEnterTimestamp=Tue 2026-07-07 10:00:00 UTC
UnitFileState=enabled"""

EXITED_BLOCK = """Id=pve-ha-lrm.service
LoadState=loaded
ActiveState=active
SubState=exited
Result=success
MainPID=0
ExecMainCode=0
ExecMainStatus=0
NRestarts=0
ActiveEnterTimestamp=Tue 2026-07-07 09:00:00 UTC
UnitFileState=enabled"""

FAILED_EXIT_CODE_BLOCK = """Id=pvestatd.service
LoadState=loaded
ActiveState=failed
SubState=failed
Result=exit-code
MainPID=0
ExecMainCode=1
ExecMainStatus=1
NRestarts=3
ActiveEnterTimestamp=
UnitFileState=enabled"""

FAILED_TIMEOUT_BLOCK = """Id=pvescheduler.service
LoadState=loaded
ActiveState=failed
SubState=failed
Result=timeout
MainPID=0
ExecMainCode=0
ExecMainStatus=0
NRestarts=1
ActiveEnterTimestamp=
UnitFileState=enabled"""

NOT_FOUND_BLOCK = """Id=nonexistent.service
LoadState=not-found
ActiveState=inactive
SubState=dead
Result=success
MainPID=0
ExecMainCode=0
ExecMainStatus=0
NRestarts=0
ActiveEnterTimestamp=
UnitFileState=n/a"""


def _build_block(unit: str) -> str:
    """Build a generic "healthy" block for ``unit`` for structural-only tests."""
    return (
        f"Id={unit}\n"
        "LoadState=loaded\n"
        "ActiveState=active\n"
        "SubState=running\n"
        "Result=success\n"
        "MainPID=1000\n"
        "ExecMainCode=0\n"
        "ExecMainStatus=0\n"
        "NRestarts=0\n"
        "ActiveEnterTimestamp=Tue 2026-07-07 08:00:00 UTC\n"
        "UnitFileState=enabled"
    )


def _fake_credential(*, host: str = "10.0.30.50", display: str | None = None) -> TerminalCredential:
    return TerminalCredential(
        target_type="endpoint",
        target_id=7,
        host=host,
        port=22,
        username="root",
        known_host_fingerprint=_FINGERPRINT,
        password="super-secret-password",
        display=display or f"root@{host}:22",
    )


def _eligible_endpoint_state(
    *,
    enabled: bool = True,
    service_monitoring_enabled: bool = True,
    service_monitoring_eligible: bool = True,
    service_monitoring_units: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": 7,
        "enabled": enabled,
        "service_monitoring_enabled": service_monitoring_enabled,
        "service_monitoring_eligible": service_monitoring_eligible,
        "service_monitoring_units": service_monitoring_units or [],
        "allow_writes": True,
        "access_methods": "api_ssh",
        "has_ssh_terminal_credentials": True,
        "effective_rpc_enabled": True,
    }


def _allow_service_monitoring(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: dict[str, object] | None = None,
) -> None:
    endpoint_state = state or _eligible_endpoint_state()

    def _fake_fetch_state(_netbox_session, endpoint_id, *, timeout=10.0):  # noqa: ARG001
        assert endpoint_id >= 1
        return endpoint_state

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_service_monitoring_state",
        _fake_fetch_state,
    )


# ---------------------------------------------------------------------------
# parse_systemctl_show_output
# ---------------------------------------------------------------------------


def test_parse_systemctl_show_output_single_active_unit() -> None:
    records = parse_systemctl_show_output(ACTIVE_BLOCK, ["pveproxy.service"])

    assert len(records) == 1
    record = records[0]
    assert record["unit"] == "pveproxy.service"
    assert record["id"] == "pveproxy.service"
    assert record["load_state"] == "loaded"
    assert record["active_state"] == "active"
    assert record["sub_state"] == "running"
    assert record["result"] == "success"
    assert record["main_pid"] == 1234
    assert record["exec_main_code"] == 0
    assert record["exec_main_status"] == 0
    assert record["n_restarts"] == 0
    assert record["active_enter_timestamp"] == "Tue 2026-07-07 10:00:00 UTC"
    assert record["unit_file_state"] == "enabled"


def test_parse_systemctl_show_output_exited_unit_is_not_conflated_with_running() -> None:
    records = parse_systemctl_show_output(EXITED_BLOCK, ["pve-ha-lrm.service"])

    record = records[0]
    # active/exited is a legitimate, healthy terminal state for oneshot-style
    # units -- must never be reported the same as active/running.
    assert record["active_state"] == "active"
    assert record["sub_state"] == "exited"
    assert record["result"] == "success"


def test_parse_systemctl_show_output_failed_unit_reports_exit_code_and_restarts() -> None:
    records = parse_systemctl_show_output(FAILED_EXIT_CODE_BLOCK, ["pvestatd.service"])

    record = records[0]
    assert record["active_state"] == "failed"
    assert record["sub_state"] == "failed"
    assert record["result"] == "exit-code"
    assert record["n_restarts"] == 3
    assert record["exec_main_status"] == 1
    # An empty timestamp string is preserved verbatim, never coerced.
    assert record["active_enter_timestamp"] == ""


def test_parse_systemctl_show_output_failed_unit_reports_timeout_result() -> None:
    records = parse_systemctl_show_output(FAILED_TIMEOUT_BLOCK, ["pvescheduler.service"])

    record = records[0]
    assert record["active_state"] == "failed"
    # Result is passed through verbatim -- the parser never enumerates or
    # hardcodes specific systemd Result values.
    assert record["result"] == "timeout"
    assert record["n_restarts"] == 1


def test_parse_systemctl_show_output_missing_unit_reports_not_found_load_state() -> None:
    records = parse_systemctl_show_output(NOT_FOUND_BLOCK, ["nonexistent.service"])

    record = records[0]
    assert record["load_state"] == "not-found"
    assert record["active_state"] == "inactive"
    assert record["sub_state"] == "dead"


def test_parse_systemctl_show_output_multi_unit_pairs_blocks_positionally() -> None:
    raw_output = "\n\n".join(
        [ACTIVE_BLOCK, EXITED_BLOCK, FAILED_EXIT_CODE_BLOCK, FAILED_TIMEOUT_BLOCK, NOT_FOUND_BLOCK]
    )
    units = [
        "pveproxy.service",
        "pve-ha-lrm.service",
        "pvestatd.service",
        "pvescheduler.service",
        "nonexistent.service",
    ]

    records = parse_systemctl_show_output(raw_output, units)

    # Pairing is positional (request order), not keyed off the parsed `Id=`
    # value -- a syntactically valid but uninstalled unit still returns a
    # well-formed block (`LoadState=not-found`), so a keyed lookup would be
    # no more reliable than trusting systemd's block order.
    assert [r["unit"] for r in records] == units
    assert [r["active_state"] for r in records] == [
        "active",
        "active",
        "failed",
        "failed",
        "inactive",
    ]
    assert [r["sub_state"] for r in records] == [
        "running",
        "exited",
        "failed",
        "failed",
        "dead",
    ]
    assert [r["result"] for r in records] == [
        "success",
        "success",
        "exit-code",
        "timeout",
        "success",
    ]


def test_parse_systemctl_show_output_trailing_blank_lines_do_not_create_phantom_blocks() -> None:
    raw_output = ACTIVE_BLOCK + "\n\n\n"

    records = parse_systemctl_show_output(raw_output, ["pveproxy.service"])

    assert len(records) == 1


def test_parse_systemctl_show_output_short_output_yields_fewer_records_than_requested() -> None:
    # A partial/truncated result must not raise -- this is a read-only
    # monitoring path, so a partial result beats a hard failure.
    records = parse_systemctl_show_output(ACTIVE_BLOCK, ["pveproxy.service", "corosync.service"])

    assert len(records) == 1
    assert records[0]["unit"] == "pveproxy.service"


# ---------------------------------------------------------------------------
# build_systemctl_show_command
# ---------------------------------------------------------------------------


def test_build_systemctl_show_command_has_fixed_properties_and_quoted_units() -> None:
    command = build_systemctl_show_command(["pveproxy.service", "corosync.service"])

    assert command.startswith(
        "systemctl show --no-pager -p Id -p LoadState -p ActiveState -p SubState "
        "-p Result -p MainPID -p ExecMainCode -p ExecMainStatus -p NRestarts "
        "-p ActiveEnterTimestamp -p UnitFileState --"
    )
    assert command.endswith("-- pveproxy.service corosync.service")
    assert "sudo" not in command


def test_build_systemctl_show_command_shlex_quote_neutralizes_shell_metacharacters() -> None:
    # parse_requested_units would reject this before it ever reaches here;
    # this proves build_systemctl_show_command stays safe as defense in depth
    # even if a future caller skipped validation.
    dangerous = "svc; rm -rf /"

    command = build_systemctl_show_command([dangerous])

    # Round-tripping through shlex.split (POSIX shell word-splitting rules)
    # proves the dangerous token comes back out as a single argv entry, never
    # as raw shell syntax that would run "rm -rf /" as a second command.
    tokens = shlex.split(command)
    assert tokens[-1] == dangerous
    assert "rm" not in tokens


# ---------------------------------------------------------------------------
# parse_requested_units (allowlist + validation)
# ---------------------------------------------------------------------------


def test_parse_requested_units_none_returns_sorted_default_allowlist() -> None:
    assert parse_requested_units(None) == sorted(PROXMOX_MONITORED_UNITS_DEFAULT)


def test_parse_requested_units_empty_string_returns_sorted_default_allowlist() -> None:
    assert parse_requested_units("   ") == sorted(PROXMOX_MONITORED_UNITS_DEFAULT)


def test_parse_requested_units_valid_comma_separated_returned_in_order() -> None:
    result = parse_requested_units("pveproxy.service, corosync.service")

    assert result == ["pveproxy.service", "corosync.service"]


def test_parse_requested_units_ignores_empty_entries_between_commas() -> None:
    result = parse_requested_units("pveproxy.service,,corosync.service,")

    assert result == ["pveproxy.service", "corosync.service"]


def test_parse_requested_units_accepts_configured_unit_outside_default_set() -> None:
    result = parse_requested_units("sshd.service")

    assert result == ["sshd.service"]


def test_parse_requested_units_accepts_unit_name_up_to_100_characters() -> None:
    unit = "a" * 92 + ".service"

    assert len(unit) == 100
    assert parse_requested_units(unit) == [unit]


def test_parse_requested_units_rejects_invalid_characters() -> None:
    with pytest.raises(UnitValidationError, match="invalid characters"):
        parse_requested_units("pveproxy.service; rm -rf /")


def test_parse_requested_units_rejects_leading_hyphen() -> None:
    with pytest.raises(UnitValidationError, match="invalid characters"):
        parse_requested_units("-pveproxy.service")


def test_parse_requested_units_rejects_path_traversal() -> None:
    with pytest.raises(UnitValidationError, match=r"\.\."):
        parse_requested_units("../../etc/passwd")


def test_parse_requested_units_rejects_overlong_unit_name() -> None:
    with pytest.raises(UnitValidationError, match="exceeds"):
        parse_requested_units("a" * 93 + ".service")


def test_parse_requested_units_rejects_too_many_units() -> None:
    units = ",".join(["pveproxy.service"] * 33)

    with pytest.raises(UnitValidationError, match="At most"):
        parse_requested_units(units)


# ---------------------------------------------------------------------------
# GET /proxmox/services/systemd route contract
# ---------------------------------------------------------------------------


def test_get_systemd_services_requires_auth(test_client) -> None:
    response = test_client.get("/proxmox/services/systemd", params={"endpoint_id": 1})

    assert response.status_code == 401


def test_get_systemd_services_rejects_invalid_units_before_touching_netbox(
    monkeypatch, auth_test_client
) -> None:
    def _unexpected_call(*_args, **_kwargs):
        raise AssertionError("must not touch NetBox for an invalid unit request")

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_service_monitoring_state",
        _unexpected_call,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_credential",
        _unexpected_call,
    )

    response = auth_test_client.get(
        "/proxmox/services/systemd",
        params={"endpoint_id": 1, "units": "pveproxy.service; rm -rf /"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_units"


def test_get_systemd_services_rejects_endpoint_id_below_one(auth_test_client) -> None:
    response = auth_test_client.get("/proxmox/services/systemd", params={"endpoint_id": 0})

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        (
            _eligible_endpoint_state(enabled=False),
            "service_monitoring_endpoint_disabled",
        ),
        (
            _eligible_endpoint_state(
                service_monitoring_enabled=False,
                service_monitoring_eligible=True,
            ),
            "service_monitoring_disabled",
        ),
        (
            _eligible_endpoint_state(service_monitoring_eligible=False),
            "service_monitoring_ineligible",
        ),
    ],
)
def test_get_systemd_services_rejects_unauthorized_or_ineligible_endpoint_before_credentials(
    monkeypatch,
    auth_test_client,
    state,
    expected_reason,
) -> None:
    _allow_service_monitoring(monkeypatch, state=state)

    def _unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("must not fetch SSH credentials for an ineligible endpoint")

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_credential",
        _unexpected_fetch,
    )

    response = auth_test_client.get(
        "/proxmox/services/systemd",
        params={"endpoint_id": 7, "units": "pveproxy.service"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == expected_reason


@pytest.mark.parametrize(
    ("cause", "expected_status", "expected_reason"),
    [
        (
            urllib.error.HTTPError("http://netbox/x", 404, "Not Found", None, None),
            404,
            "ssh_credential_not_found",
        ),
        (
            urllib.error.HTTPError("http://netbox/x", 403, "Forbidden", None, None),
            403,
            "ssh_not_enabled_for_endpoint",
        ),
        (
            urllib.error.HTTPError("http://netbox/x", 422, "Unprocessable", None, None),
            422,
            "invalid_endpoint_ssh_config",
        ),
        (
            urllib.error.HTTPError("http://netbox/x", 500, "Boom", None, None),
            502,
            "netbox_credential_fetch_failed",
        ),
        (
            urllib.error.URLError("connection refused"),
            502,
            "netbox_unreachable",
        ),
        (
            json.JSONDecodeError("Expecting value", "", 0),
            502,
            "invalid_netbox_response",
        ),
        (
            ValueError("missing host"),
            422,
            "incomplete_ssh_credential",
        ),
        (
            None,
            502,
            "credential_fetch_failed",
        ),
    ],
)
def test_get_systemd_services_credential_error_mapping(
    monkeypatch, auth_test_client, cause, expected_status, expected_reason
) -> None:
    _allow_service_monitoring(monkeypatch)

    def _raise(_netbox_session, _endpoint_id, _host):
        exc = TerminalCredentialError("could not resolve SSH credential")
        if cause is not None:
            exc.__cause__ = cause
        raise exc

    monkeypatch.setattr("proxbox_api.routes.proxmox.services._fetch_endpoint_credential", _raise)

    response = auth_test_client.get("/proxmox/services/systemd", params={"endpoint_id": 7})

    assert response.status_code == expected_status
    assert response.json()["detail"]["reason"] == expected_reason


def test_get_systemd_services_unreachable_endpoint_returns_200_reachable_false(
    monkeypatch, auth_test_client
) -> None:
    credential = _fake_credential()
    _allow_service_monitoring(monkeypatch)

    def _fake_fetch(_netbox_session, _endpoint_id, _host):
        return credential

    async def _fake_run(cred, _command, *, timeout=10.0):  # noqa: ARG001
        raise SSHCommandError(f"Could not reach {cred.display}: connection timed out")

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_credential", _fake_fetch
    )
    monkeypatch.setattr("proxbox_api.routes.proxmox.services.run_endpoint_command", _fake_run)

    response = auth_test_client.get(
        "/proxmox/services/systemd",
        params={"endpoint_id": 7, "units": "pveproxy.service"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reachable"] is False
    assert payload["services"] == []
    assert payload["error"]["reason"] == "ssh_unreachable"
    assert payload["host"] == "10.0.30.50"
    assert "super-secret-password" not in response.text


def test_get_systemd_services_reachable_endpoint_returns_parsed_services(
    monkeypatch, auth_test_client
) -> None:
    credential = _fake_credential()
    _allow_service_monitoring(monkeypatch)

    def _fake_fetch(_netbox_session, _endpoint_id, _host):
        return credential

    async def _fake_run(_cred, command, *, timeout=10.0):  # noqa: ARG001
        assert "systemctl show --no-pager" in command
        assert "pveproxy.service" in command
        return CompletedCommand(command=command, stdout=ACTIVE_BLOCK, stderr="", exit_status=0)

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_credential", _fake_fetch
    )
    monkeypatch.setattr("proxbox_api.routes.proxmox.services.run_endpoint_command", _fake_run)

    response = auth_test_client.get(
        "/proxmox/services/systemd",
        params={"endpoint_id": 7, "units": "pveproxy.service"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reachable"] is True
    assert payload["error"] is None
    assert payload["endpoint_id"] == 7
    assert payload["host"] == "10.0.30.50"
    assert len(payload["services"]) == 1
    service = payload["services"][0]
    assert service["unit"] == "pveproxy.service"
    assert service["active_state"] == "active"
    assert service["sub_state"] == "running"
    assert service["main_pid"] == 1234
    assert "super-secret-password" not in response.text


def test_get_systemd_services_nonzero_systemctl_exit_returns_command_failed(
    monkeypatch, auth_test_client
) -> None:
    credential = _fake_credential()
    _allow_service_monitoring(monkeypatch)

    def _fake_fetch(_netbox_session, _endpoint_id, _host):
        return credential

    async def _fake_run(_cred, command, *, timeout=10.0):  # noqa: ARG001
        return CompletedCommand(
            command=command,
            stdout=ACTIVE_BLOCK,
            stderr="System has not been booted with systemd as init system",
            exit_status=1,
        )

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_credential", _fake_fetch
    )
    monkeypatch.setattr("proxbox_api.routes.proxmox.services.run_endpoint_command", _fake_run)

    response = auth_test_client.get(
        "/proxmox/services/systemd",
        params={"endpoint_id": 7, "units": "pveproxy.service"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reachable"] is True
    assert payload["services"] == []
    assert payload["error"]["reason"] == "command_failed"
    assert "exited with status 1" in payload["error"]["detail"]
    assert "System has not been booted" in payload["error"]["detail"]


def test_get_systemd_services_command_timeout_returns_stable_error(
    monkeypatch, auth_test_client
) -> None:
    credential = _fake_credential()
    _allow_service_monitoring(monkeypatch)

    def _fake_fetch(_netbox_session, _endpoint_id, _host):
        return credential

    async def _fake_run(_cred, _command, *, timeout=10.0):  # noqa: ARG001
        raise SSHCommandTimeoutError("SSH command timed out on root@10.0.30.50:22 after 10.0s")

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_credential", _fake_fetch
    )
    monkeypatch.setattr("proxbox_api.routes.proxmox.services.run_endpoint_command", _fake_run)

    response = auth_test_client.get(
        "/proxmox/services/systemd",
        params={"endpoint_id": 7, "units": "pveproxy.service"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reachable"] is True
    assert payload["services"] == []
    assert payload["error"]["reason"] == "command_timeout"
    assert "timed out" in payload["error"]["detail"]


def test_get_systemd_services_default_units_used_when_units_param_omitted(
    monkeypatch, auth_test_client
) -> None:
    credential = _fake_credential(host="10.0.30.60")
    expected_units = sorted(PROXMOX_MONITORED_UNITS_DEFAULT)
    _allow_service_monitoring(monkeypatch)

    def _fake_fetch(_netbox_session, _endpoint_id, _host):
        return credential

    async def _fake_run(_cred, command, *, timeout=10.0):  # noqa: ARG001
        raw_output = "\n\n".join(_build_block(unit) for unit in expected_units)
        return CompletedCommand(command=command, stdout=raw_output, stderr="", exit_status=0)

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_credential", _fake_fetch
    )
    monkeypatch.setattr("proxbox_api.routes.proxmox.services.run_endpoint_command", _fake_run)

    response = auth_test_client.get("/proxmox/services/systemd", params={"endpoint_id": 9})

    assert response.status_code == 200
    payload = response.json()
    assert [service["unit"] for service in payload["services"]] == expected_units


def test_get_systemd_services_uses_netbox_configured_units_up_to_100_chars(
    monkeypatch, auth_test_client
) -> None:
    long_unit = "a" * 92 + ".service"
    credential = _fake_credential(host="10.0.30.70")
    _allow_service_monitoring(
        monkeypatch,
        state=_eligible_endpoint_state(service_monitoring_units=[long_unit]),
    )

    def _fake_fetch(_netbox_session, _endpoint_id, _host):
        return credential

    async def _fake_run(_cred, command, *, timeout=10.0):  # noqa: ARG001
        tokens = shlex.split(command)
        assert tokens[-1] == long_unit
        return CompletedCommand(
            command=command, stdout=_build_block(long_unit), stderr="", exit_status=0
        )

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.services._fetch_endpoint_credential", _fake_fetch
    )
    monkeypatch.setattr("proxbox_api.routes.proxmox.services.run_endpoint_command", _fake_run)

    response = auth_test_client.get("/proxmox/services/systemd", params={"endpoint_id": 7})

    assert response.status_code == 200
    payload = response.json()
    assert [service["unit"] for service in payload["services"]] == [long_unit]


@pytest.mark.asyncio
async def test_run_endpoint_command_times_out_remote_command_and_closes_connection(
    monkeypatch,
) -> None:
    credential = _fake_credential()

    class _FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            self.closed = True

        async def run(self, _command, *, check=False):  # noqa: ARG002
            await asyncio.sleep(1)

    fake_conn = _FakeConnection()

    class _FakeAsyncSSH:
        class SSHClient:
            pass

        class Error(Exception):
            pass

        async def connect(self, *_args, **_kwargs):
            return fake_conn

    monkeypatch.setattr(
        "proxbox_api.services.ssh_terminal._load_asyncssh",
        lambda: _FakeAsyncSSH(),
    )

    with pytest.raises(SSHCommandTimeoutError, match="timed out"):
        await run_endpoint_command(credential, "systemctl show -- test.service", timeout=0.01)

    assert fake_conn.closed is True


# ---------------------------------------------------------------------------
# Static guardrail: no shell interpolation anywhere in the new modules.
# ---------------------------------------------------------------------------


def test_proxmox_services_modules_never_use_shell_interpolation() -> None:
    # Static guardrail only: these are substring checks against the *source
    # text* of the two implementation files (proving neither one calls
    # eval()/exec()/os.system()/subprocess with shell=True) -- this test
    # itself never calls eval()/exec() on anything.
    service_source = Path("proxbox_api/services/proxmox_services.py").read_text()
    route_source = Path("proxbox_api/routes/proxmox/services.py").read_text()

    for source in (service_source, route_source):
        assert "shell=True" not in source
        assert "os.system" not in source
        assert "subprocess" not in source
        assert "eval(" not in source
        assert "exec(" not in source
