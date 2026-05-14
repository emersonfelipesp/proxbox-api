"""NetBox→Proxmox intent endpoints.

Sub-PR D (#381) introduces the ``/intent/plan`` validator. Later sub-PRs
hang ``/intent/apply`` (F/G) and ``/intent/deletion-requests`` (I) off the
same router.

Every write route under this package MUST call ``_gate(session, endpoint_id)``
from ``proxbox_api.routes.proxmox_actions`` so the ``allow_writes`` toggle
on ``ProxmoxEndpoint`` keeps gating writes uniformly.
"""

from __future__ import annotations

from proxbox_api.routes.intent.apply import router as apply_router
from proxbox_api.routes.intent.plan import router
from proxbox_api.routes.intent.schemas import (
    ApplyDiff,
    ApplyRequest,
    ApplyResponse,
    ApplyResultItem,
    LXCIntentPayload,
    VMIntentPayload,
)

router.include_router(apply_router)

__all__ = [
    "ApplyDiff",
    "ApplyRequest",
    "ApplyResponse",
    "ApplyResultItem",
    "LXCIntentPayload",
    "VMIntentPayload",
    "router",
]
