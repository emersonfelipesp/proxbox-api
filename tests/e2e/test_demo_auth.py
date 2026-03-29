"""E2E tests for NetBox demo authentication.

Tests the Playwright-based authentication flow for NetBox demo instances.
"""

from __future__ import annotations

import pytest

from proxbox_api.e2e.demo_auth import (
    DemoAuthError,
    DemoLoginError,
    PlaywrightNotInstalledError,
    create_demo_user,
    demo_auth_required,
    generate_password,
    generate_username,
    login,
    provision_demo_token,
    refresh_demo_profile,
)
from proxbox_api.e2e.fixtures.test_data import get_e2e_credentials, get_e2e_demo_config


@pytest.mark.asyncio
class TestDemoAuthHelpers:
    """Tests for demo auth helper functions."""

    def test_generate_username_format(self):
        """Test username generation produces valid format."""
        username = generate_username()
        assert username.startswith("proxbox_e2e_")
        assert len(username) > len("proxbox_e2e_")

    def test_generate_password_length(self):
        """Test password generation produces adequate length."""
        password = generate_password()
        assert len(password) == 32
        assert any(c.isupper() for c in password)
        assert any(c.islower() for c in password)
        assert any(c.isdigit() for c in password)

    def test_get_e2e_credentials_returns_tuple(self):
        """Test credential retrieval returns username and password."""
        username, password = get_e2e_credentials()
        assert isinstance(username, str)
        assert isinstance(password, str)
        assert len(username) > 0
        assert len(password) > 0

    def test_get_e2e_demo_config_returns_dict(self):
        """Test demo config retrieval returns expected structure."""
        config = get_e2e_demo_config()
        assert isinstance(config, dict)
        assert "demo_url" in config
        assert "timeout" in config
        assert "headless" in config
        assert config["demo_url"] == "https://demo.netbox.dev"


@pytest.mark.asyncio
class TestDemoAuthIntegration:
    """Integration tests for demo auth with NetBox demo.

    These tests require a live NetBox demo instance and Playwright.
    They are marked with @pytest.mark.e2e and can be skipped in CI.
    """

    @pytest.fixture
    def test_credentials(self):
        """Get test credentials."""
        return get_e2e_credentials()

    @pytest.fixture
    def demo_config(self):
        """Get demo config."""
        return get_e2e_demo_config()

    async def test_create_demo_user(self, test_credentials, demo_config):
        """Test creating a demo user.

        This test verifies that we can navigate to the demo login page
        and create a new user account.
        """
        username, password = test_credentials

        user_created = await create_demo_user(
            username=username,
            password=password,
            headless=demo_config["headless"],
        )

        assert isinstance(user_created, bool)

    async def test_login_existing_user(self, test_credentials, demo_config):
        """Test logging in with existing credentials.

        This test verifies that we can log in with credentials
        that were previously created.
        """
        username, password = test_credentials

        try:
            await login(
                username=username,
                password=password,
                headless=demo_config["headless"],
            )
        except DemoLoginError:
            pytest.fail("Failed to login with existing credentials")

    async def test_provision_demo_token(self, test_credentials, demo_config):
        """Test provisioning a demo token.

        This is the main auth flow test that verifies we can:
        1. Navigate to demo login
        2. Create user or login
        3. Navigate to tokens page
        4. Create a v1 token
        5. Return the token value
        """
        username, password = test_credentials

        token = await provision_demo_token(
            username=username,
            password=password,
            headless=demo_config["headless"],
            timeout=demo_config["timeout"],
            token_name="proxbox-e2e-test",
        )

        assert token.version == "v1"
        assert token.secret is not None
        assert len(token.secret) > 40

    async def test_bootstrap_demo_profile(self, test_credentials, demo_config):
        """Test full profile bootstrap.

        This test verifies that bootstrap_demo_profile returns
        a valid Config object with all necessary fields.
        """
        from proxbox_api.e2e.demo_auth import bootstrap_demo_profile

        username, password = test_credentials

        config = await bootstrap_demo_profile(
            username=username,
            password=password,
            timeout=demo_config["timeout"],
            headless=demo_config["headless"],
            token_name="proxbox-e2e-bootstrap",
        )

        assert config.base_url == "https://demo.netbox.dev"
        assert config.token_version == "v1"
        assert config.demo_username == username
        assert config.demo_password == password
        assert config.token_secret is not None


@pytest.mark.asyncio
class TestDemoAuthErrorHandling:
    """Tests for demo auth error handling."""

    async def test_playwright_not_installed_error(self):
        """Test that proper error is raised when Playwright missing."""

        @demo_auth_required
        async def _test():
            pass

        import sys

        original_modules = dict(sys.modules)

        try:
            for mod_name in list(sys.modules.keys()):
                if "playwright" in mod_name:
                    del sys.modules[mod_name]

            from importlib import reload

            import proxbox_api.e2e.demo_auth as demo_auth_module

            reload(demo_auth_module)

            with pytest.raises((PlaywrightNotInstalledError, ModuleNotFoundError)):
                await demo_auth_module.provision_demo_token(
                    username="test",
                    password="test",
                )
        finally:
            for mod_name in list(sys.modules.keys()):
                if "playwright" in mod_name and mod_name not in original_modules:
                    del sys.modules[mod_name]


@pytest.mark.asyncio
class TestTokenRefresh:
    """Tests for token refresh functionality."""

    @pytest.fixture
    def test_credentials(self):
        """Get test credentials."""
        return get_e2e_credentials()

    async def test_refresh_demo_profile(self, test_credentials, demo_config):
        """Test refreshing demo profile with existing credentials.

        This test verifies that refresh_demo_profile can use
        saved credentials from an existing Config to get a new token.
        """
        from proxbox_api.e2e.demo_auth import (
            bootstrap_demo_profile,
            refresh_demo_profile,
        )

        username, password = test_credentials

        initial_config = await bootstrap_demo_profile(
            username=username,
            password=password,
            timeout=demo_config["timeout"],
            headless=demo_config["headless"],
            token_name="proxbox-e2e-refresh",
        )

        refreshed_config = await refresh_demo_profile(
            initial_config,
            headless=demo_config["headless"],
            token_name="proxbox-e2e-refresh-2",
        )

        assert refreshed_config.token_version == "v1"
        assert refreshed_config.demo_username == username
        assert refreshed_config.demo_password == password
        assert refreshed_config.token_secret is not None

    async def test_refresh_without_credentials_raises(self):
        """Test that refresh without credentials raises error."""
        from netbox_sdk.config import Config

        empty_config = Config(
            base_url="https://demo.netbox.dev",
            token_version="v1",
            token_secret="dummy",
        )

        with pytest.raises(DemoAuthError, match="credentials are not available"):
            await refresh_demo_profile(empty_config)
