"""Result carriers for sync stages that can finish degraded.

A stage that drops individual VMs (for example because their Proxmox ownership
cannot be resolved) still completes for the rest. It reports the dropped VMs as
structured ``warnings`` and marks the outcome ``degraded`` so the orchestrating
plugin and the full-update aggregate can surface them instead of hiding them.
"""

from __future__ import annotations

from collections.abc import Iterable

StageWarning = dict[str, object]


class WarningList(list):
    """List result carrying optional warnings for callers that can surface them."""

    def __init__(
        self,
        values: Iterable[object] | None = None,
        *,
        warnings: list[StageWarning] | None = None,
    ) -> None:
        super().__init__(values or [])
        self.warnings: list[StageWarning] = warnings or []

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)


def result_warnings(value: object) -> list[StageWarning]:
    """Read warnings from a stage result, whether a list carrier or a dict payload."""

    warnings = (
        value.get("warnings") if isinstance(value, dict) else getattr(value, "warnings", None)
    )
    if isinstance(warnings, list):
        return [item for item in warnings if isinstance(item, dict)]
    return []


def attach_skips_to_dict(
    result: dict[str, object],
    skipped: list[StageWarning],
) -> dict[str, object]:
    """Record dropped VMs on a dict stage result and mark it degraded."""

    if skipped:
        result["degraded"] = True
        result["warnings"] = [*result_warnings(result), *skipped]
    return result


def attach_skips_to_list(values: list, skipped: list[StageWarning]) -> list:
    """Record dropped VMs on a list stage result; return it unchanged when none."""

    if not skipped:
        return values
    return WarningList(values, warnings=[*result_warnings(values), *skipped])


def response_with_stage_warnings(
    result: list,
    *,
    result_key: str,
) -> list | dict[str, object]:
    """Wrap a warned list result for REST, keeping a plain list when it is clean."""

    warnings = result_warnings(result)
    if not warnings:
        return result
    return {
        result_key: list(result),
        "count": len(result),
        "warnings": warnings,
        "degraded": True,
    }


def degraded_suffix(skipped: list[StageWarning]) -> str:
    """Text appended to a phase summary when VMs were dropped."""

    if not skipped:
        return ""
    return f" (degraded: {len(skipped)} VM(s) skipped)"


def require_no_dropped_vms(result: object) -> object:
    """Fail closed when a stage that must be strict nonetheless dropped a VM.

    A stage that is lenient by default can still run on behalf of a caller that
    already validated a single VM strictly. If it reports a dropped VM anyway
    (for example because ownership changed between two reads), that caller must
    not report success, so the drop is raised as the same typed gateway failure a
    strict selection produces.
    """

    from proxbox_api.exception import ProxboxException

    dropped = [warning for warning in result_warnings(result) if "netbox_vm_id" in warning]
    if dropped:
        raise ProxboxException(
            message="Unable to resolve explicitly selected VM ownership",
            detail=str(dropped[0].get("reason") or "selected VM ownership changed during sync"),
            http_status_code=502,
        )
    return result
