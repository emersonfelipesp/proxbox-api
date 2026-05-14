"""AST/`model_fields` contract for :class:`SyncContext`.

Issue #375 — Phase A. Pins the seven-field shape (five active + two
forward-compat) so Phases B and C add behaviour without breaking
``model_fields``. Signature drift fails CI here before it can ripple
through the reconcilers.

Roadmap row #18 ("AST contract test pins the new ProxmoxSession-style
dataclass shape; signature drift fails CI") is implemented by this
file.
"""

from __future__ import annotations

from proxbox_api.services.run_session import SyncContext


def test_sync_context_field_set_is_pinned() -> None:
    """All seven fields must be present, no more, no less."""
    assert set(SyncContext.model_fields) == {
        "nb",
        "px_sessions",
        "tag",
        "settings",
        "operation_id",
        "run_uuid",
        "netbox_branch",
    }


def test_sync_context_forward_compat_defaults() -> None:
    """``run_uuid`` and ``netbox_branch`` must keep their Phase A defaults
    so callers never have to pass them until Phases B and C light up."""
    assert SyncContext.model_fields["run_uuid"].default is None
    assert SyncContext.model_fields["netbox_branch"].default == ""


def test_sync_context_is_frozen() -> None:
    """SyncContext must be immutable; reconcilers can't smuggle state in."""
    assert SyncContext.model_config.get("frozen") is True


def test_sync_context_allows_arbitrary_types() -> None:
    """``nb``, ``tag``, and ``px_sessions`` entries are externally-typed
    (NetBox async session, ``ProxboxTagDep``, Proxmox sessions); we don't
    pull their schemas in here, so the Pydantic config must allow them."""
    assert SyncContext.model_config.get("arbitrary_types_allowed") is True
