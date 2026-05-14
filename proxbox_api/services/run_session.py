"""Per-run sync session for proxbox-api reconcilers.

Issue #375 — Phase A. Collapses the per-run argument grab-bag that
``sync_cluster_individual`` and (later) other reconcilers in
``proxbox_api/services/sync/individual/`` accept into one immutable
object passed down the call stack.

Phase A pins the seven-field shape that Phases B and C will populate
with semantics:

- ``run_uuid`` is reserved for issue #367 (run-UUID stamp +
  ``delete_orphans``). Phase A keeps it ``None`` and **does not branch
  on it anywhere** in the reconciler tree; ``operation_id`` from the
  ``set_operation_id`` ContextVar remains the source of truth for the
  per-run identifier.
- ``netbox_branch`` is reserved for issue #370 (``X-NetBox-Branch``
  header). Phase A keeps it ``""`` and **does not branch on it anywhere**;
  the NetBox HTTP client middleware will read it once #370 lands.

The contract pinning these two fields up front is intentional: the AST
contract test (``tests/test_run_session_contract.py``) freezes the
seven-field set at Phase A so Phases B and C add behaviour without
changing ``model_fields``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from proxbox_api.schemas import PluginConfig


class SyncContext(BaseModel):
    """Per-run sync session bundle.

    Attributes:
        nb: The NetBox async session resolved from ``NetBoxSessionDep`` /
            ``NetBoxAsyncSessionDep``. Typed as ``object`` to avoid pulling
            the heavy session schemas into this module.
        px_sessions: The list of Proxmox sessions for this run (the value
            of ``ProxmoxSessionsDep``). Per-cluster reconcilers resolve
            the single session they need via
            ``services.sync.individual.helpers.resolve_proxmox_session``.
        tag: The ``ProxboxTagDep`` value (typed ``object`` for the same
            reason as ``nb``).
        settings: The resolved ``PluginConfig`` snapshot for this run.
        operation_id: Snapshot of ``set_operation_id`` /
            ``get_operation_id`` at construction time.
            In Phase A the ContextVar is the source of truth; this field
            is a convenience read-only copy. In Phase B (#367) the
            ownership inverts.
        run_uuid: **Phase A: unused.** Reserved for issue #367.
        netbox_branch: **Phase A: unused.** Reserved for issue #370.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    nb: object
    px_sessions: list[object]
    tag: object
    settings: PluginConfig
    operation_id: str
    run_uuid: str | None = None
    netbox_branch: str = ""
