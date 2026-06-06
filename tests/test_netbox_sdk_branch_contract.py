"""Guard the netbox-sdk branch context manager contract used by proxbox-api."""

from __future__ import annotations

from netbox_sdk import Api
from netbox_sdk.client import NetBoxApiClient
from netbox_sdk.config import Config


def test_netbox_sdk_activate_branch_returns_sync_context_manager() -> None:
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

    assert hasattr(branch_scope, "__enter__")
    assert hasattr(branch_scope, "__exit__")
    assert not hasattr(branch_scope, "__aenter__")
