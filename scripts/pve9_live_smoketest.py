"""Live PVE 9.1.1 smoke-test for the issue #417 auth fixes.

Runs scenarios against the real Proxmox VE 9.1.1 host so we can confirm the
three on-our-side regressions are gone:

1. Token auth, valid credentials — expect Authentication: Success, version 9.x.
2. Token auth, deliberately broken secret — expect the new structured detail
   (PVE upstream error text), not "Unknown error."; and zero
   "Unclosed client session" lines from aiohttp.

The host secret is read from environment variables that the wrapper exports
from .hosts-env. The secret is never echoed, logged, or written to disk.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import warnings
from typing import Any

LOG_BUFFER: list[str] = []


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            self.handleError(record)


def _install_log_capture() -> None:
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    handler = _BufferHandler()
    handler.setFormatter(fmt)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logging.captureWarnings(True)
    warnings.simplefilter("always", ResourceWarning)


def _scrub(value: str) -> str:
    secret = os.environ.get("PROXMOX_SECRET", "")
    return value.replace(secret, "<redacted>") if secret else value


def _build_cluster_config(*, sabotage_token: bool) -> dict[str, Any]:
    host = os.environ["PROXMOX_HOST"]
    user = os.environ["PROXMOX_USER"]
    token_name = os.environ["PROXMOX_TOKEN_ID"]
    token_value = os.environ["PROXMOX_SECRET"]
    if sabotage_token:
        token_value = "00000000-0000-0000-0000-000000000000"
    return {
        "name": "pve9-live",
        "ip_address": host,
        "domain": host,
        "http_port": 8006,
        "user": user,
        "password": None,
        "token": {"name": token_name, "value": token_value},
        "ssl": False,
        "timeout": 10,
    }


async def _run_scenario(name: str, *, sabotage_token: bool = False) -> dict[str, Any]:
    from proxbox_api.session.proxmox_core import ProxmoxSession

    cluster_config = _build_cluster_config(sabotage_token=sabotage_token)

    result: dict[str, Any] = {
        "scenario": name,
        "ok": False,
        "version": None,
        "auth_method": None,
        "detail": None,
        "exception_type": None,
    }
    session = None
    before_logs = len(LOG_BUFFER)
    try:
        session = await ProxmoxSession.create(cluster_config)
        result["ok"] = True
        result["version"] = session.version
        result["auth_method"] = (
            "token" if cluster_config["token"]["name"] and cluster_config["token"]["value"] else "password"
        )
    except Exception as exc:
        result["exception_type"] = type(exc).__name__
        result["detail"] = _scrub(str(exc))[:800]
    finally:
        if session is not None:
            try:
                await session.aclose()
            except Exception:
                pass

    new_lines = LOG_BUFFER[before_logs:]
    result["unclosed_session_log_lines"] = sum(
        1 for line in new_lines if "Unclosed client session" in line
    )
    result["unclosed_connector_log_lines"] = sum(
        1 for line in new_lines if "Unclosed connector" in line
    )
    return result


async def main() -> int:
    _install_log_capture()
    scenarios = [
        ("token_valid", False),
        ("token_sabotaged", True),
    ]
    print("=" * 60)
    print(f"PVE host: {os.environ.get('PROXMOX_HOST')}   user: {os.environ.get('PROXMOX_USER')}")
    print(f"Token ID: {os.environ.get('PROXMOX_TOKEN_ID')}   secret: <redacted>")
    print("=" * 60)
    overall_ok = True
    for sname, sabotage in scenarios:
        res = await _run_scenario(sname, sabotage_token=sabotage)
        print(f"\n--- scenario: {sname} ---")
        for k, v in res.items():
            if k == "version" and isinstance(v, dict):
                trimmed = {kk: v.get(kk) for kk in ("version", "release", "repoid") if kk in v}
                print(f"  {k}: {trimmed}")
            else:
                print(f"  {k}: {v}")
        if sname == "token_valid":
            ver = res["version"]
            ver_str = ""
            if isinstance(ver, dict):
                ver_str = str(ver.get("version", ""))
            elif ver is not None:
                ver_str = str(ver)
            overall_ok &= bool(res["ok"]) and ver_str.startswith("9.")
        if sname == "token_sabotaged":
            overall_ok &= (
                not res["ok"]
                and "Unknown error" not in (res["detail"] or "")
                and res["unclosed_session_log_lines"] == 0
                and res["unclosed_connector_log_lines"] == 0
            )
    print("\n" + "=" * 60)
    print(f"OVERALL: {'PASS' if overall_ok else 'FAIL'}")
    print("=" * 60)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
