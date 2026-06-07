"""Guard the netbox-sdk branch context manager contract used by proxbox-api.

netbox-sdk 0.0.9 changed ``Api.activate_branch`` from a synchronous context
manager (``with``) to an asynchronous one (``async with``). proxbox-api now
enters the branch scope with ``async with`` in the cluster-sync and full-update
paths, so this guard pins the async contract: a regression back to a sync-only
context manager would break those ``async with`` call sites.
"""

from __future__ import annotations

from netbox_sdk import Api
from netbox_sdk.client import NetBoxApiClient
from netbox_sdk.config import Config


def test_netbox_sdk_activate_branch_returns_async_context_manager() -> None:
    api = Api(
        client=NetBoxApiClient(
            Config(
                base_url="https://netbox.example.test",
                token_version="v1",
                token_secret="test-token",
            )
        )
    )

    branch_scope = api.activate_branch("schema-123")

    assert hasattr(branch_scope, "__aenter__")
    assert hasattr(branch_scope, "__aexit__")
    assert not hasattr(branch_scope, "__enter__")
