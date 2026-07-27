"""Immutable, bounded Ceph timing configuration snapshots."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial

from proxbox_api import runtime_settings
from proxbox_api.settings_client import get_settings

_SETTINGS_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class CephTimingSettings:
    """One request/run snapshot; values never change during provider execution."""

    task_timeout: float = 300.0
    task_poll_interval: float = 1.0
    run_lease_seconds: float = 360.0


async def resolve_ceph_timing_settings() -> CephTimingSettings:
    """Fetch plugin settings off-loop once without caching availability fallbacks."""

    settings = await asyncio.to_thread(
        partial(
            get_settings,
            request_timeout_seconds=_SETTINGS_TIMEOUT_SECONDS,
            cache_fallback=False,
        )
    )
    task_timeout = runtime_settings.get_float(
        settings_key="ceph_task_timeout",
        env="PROXBOX_CEPH_TASK_TIMEOUT",
        default=300.0,
        minimum=1.0,
        maximum=3600.0,
        settings_override=settings,
    )
    task_poll_interval = runtime_settings.get_float(
        settings_key="ceph_task_poll_interval",
        env="PROXBOX_CEPH_TASK_POLL_INTERVAL",
        default=1.0,
        minimum=0.1,
        maximum=60.0,
        settings_override=settings,
    )
    return CephTimingSettings(
        task_timeout=task_timeout,
        # Environment overrides bypass NetBox cross-field validation. Keep the
        # runtime snapshot safe and useful by never sleeping past its timeout.
        task_poll_interval=min(task_poll_interval, task_timeout),
        run_lease_seconds=runtime_settings.get_float(
            settings_key="ceph_run_lease_seconds",
            env="PROXBOX_CEPH_RUN_LEASE_SECONDS",
            default=360.0,
            minimum=1.0,
            maximum=3600.0,
            settings_override=settings,
        ),
    )
