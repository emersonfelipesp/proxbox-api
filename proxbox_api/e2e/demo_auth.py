"""Async Playwright-based authentication for NetBox demo instances.

This module provides functionality to authenticate with NetBox demo instances
using Playwright browser automation. It is adapted from the netbox-sdk package
but converted to async and adapted for proxbox-api's testing needs.

Usage:
    config = await bootstrap_demo_profile(
        username="test_user",
        password="secure_password",
        headless=True,
    )
    session = await create_netbox_demo_session(config)
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import string
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from netbox_sdk.config import Config
    from playwright.async_api import Page

DEMO_BASE_URL = os.getenv("PROXBOX_E2E_DEMO_URL", "https://demo.netbox.dev")
DEMO_CREATE_USER_URL = f"{DEMO_BASE_URL}/plugins/demo/login/"
LOGIN_URL = f"{DEMO_BASE_URL}/login/"
TOKENS_URL = f"{DEMO_BASE_URL}/user/api-tokens/"


class DemoToken(BaseModel):
    """Represents a NetBox demo token."""

    version: str
    key: str | None
    secret: str


class DemoAuthError(Exception):
    """Base exception for demo auth failures."""

    pass


class PlaywrightNotInstalledError(DemoAuthError):
    """Raised when Playwright is not installed."""

    pass


class XServerRequiredError(DemoAuthError):
    """Raised when headed mode is requested but no X server is available."""

    pass


class BrowserDependencyError(DemoAuthError):
    """Raised when browser dependencies are missing."""

    pass


class DemoAuthTimeoutError(DemoAuthError):
    """Raised when demo auth times out."""

    pass


class DemoLoginError(DemoAuthError):
    """Raised when demo login fails."""

    pass


class DemoTokenCreationError(DemoAuthError):
    """Raised when token creation fails."""

    pass


def demo_auth_required(func):
    """Decorator that ensures Playwright is installed before executing."""

    async def wrapper(*args, **kwargs):
        if importlib.util.find_spec("playwright") is None:
            raise PlaywrightNotInstalledError(
                "Playwright is required for demo authentication. Install it with:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )
        return await func(*args, **kwargs)

    return wrapper


def generate_password(length: int = 32) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_username(prefix: str = "proxbox_e2e") -> str:
    """Generate a unique username for e2e testing."""
    timestamp = secrets.token_hex(4)
    return f"{prefix}_{timestamp}"


@demo_auth_required
async def provision_demo_token(
    *,
    username: str,
    password: str,
    headless: bool = True,
    token_name: str = "proxbox-e2e",
    timeout: float = 60.0,
) -> DemoToken:
    """Provision a demo token by automating the NetBox demo login flow.

    Args:
        username: The demo username to use.
        password: The demo password to use.
        headless: Whether to run browser in headless mode.
        token_name: Name for the API token.
        timeout: Browser operation timeout in seconds.

    Returns:
        DemoToken with version, key, and secret.

    Raises:
        PlaywrightNotInstalledError: If Playwright is not installed.
        XServerRequiredError: If headed mode requested without X server.
        BrowserDependencyError: If browser dependencies are missing.
        DemoAuthTimeoutError: If operations time out.
        DemoLoginError: If login fails.
        DemoTokenCreationError: If token creation fails.
    """
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            page = await browser.new_page()
            try:
                user_created = await _create_demo_user(page, username, password, timeout)
                if not user_created:
                    print(f"Demo user '{username}' already exists. Proceeding to login.")

                await _login(page, username, password, timeout)
                token_value = await _create_token(page, token_name, timeout)
            finally:
                await browser.close()

    except PlaywrightTimeoutError as exc:
        raise DemoAuthTimeoutError(
            "Timed out while automating demo.netbox.dev. "
            "Verify credentials and ensure Playwright browsers are installed with "
            "`playwright install chromium`."
        ) from exc
    except PlaywrightError as exc:
        error_text = str(exc)
        if "Missing X server or $DISPLAY" in error_text or "headed browser" in error_text:
            raise XServerRequiredError(
                "Playwright was started in headed mode, but no X server is available.\n"
                "Use headless mode, or run the command under xvfb.\n"
                "Example: xvfb-run pytest tests/e2e/"
            ) from exc
        if (
            "error while loading shared libraries" in error_text
            or "BrowserType.launch" in error_text
        ):
            raise BrowserDependencyError(
                "Playwright Chromium could not start because system libraries are missing.\n"
                "Install browser dependencies with:\n"
                "  playwright install --with-deps chromium"
            ) from exc
        raise DemoAuthError(f"Playwright error: {error_text}") from exc
    except Exception as exc:  # noqa: BLE001
        raise DemoAuthError(f"Failed to automate demo.netbox.dev login: {str(exc)}") from exc

    return _parse_v1_token(token_value)


async def _create_demo_user(page: "Page", username: str, password: str, timeout: float) -> bool:
    """Create a demo user on the NetBox demo instance.

    Args:
        page: Playwright page instance.
        username: Username to create.
        password: Password for the user.
        timeout: Operation timeout in seconds.

    Returns:
        True if user was created, False if user already exists.
    """
    await page.goto(DEMO_CREATE_USER_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
    await page.get_by_label("Username").fill(username)
    await page.get_by_label("Password").fill(password)
    await page.get_by_role("button", name="Create & Sign In").click()
    await page.wait_for_load_state("domcontentloaded")

    if _is_existing_demo_user_error(page, username=username):
        return False

    await page.wait_for_load_state("networkidle", timeout=timeout * 1000)
    return True


async def _login(page: "Page", username: str, password: str, timeout: float) -> None:
    """Log in to the NetBox demo instance.

    Args:
        page: Playwright page instance.
        username: Username to log in with.
        password: Password for the user.
        timeout: Operation timeout in seconds.

    Raises:
        DemoLoginError: If login fails.
    """
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout * 1000)

    if "/login/" not in page.url:
        return

    await page.get_by_label("Username").fill(username)
    await page.get_by_label("Password").fill(password)
    await page.get_by_role("button", name="Sign In").click()
    await page.wait_for_url(f"{DEMO_BASE_URL}/**", timeout=timeout * 1000)

    if "/login/" in page.url:
        raise DemoLoginError("Demo login failed. Check the provided username and password.")


async def _create_token(page: "Page", token_name: str, timeout: float) -> str:
    """Create an API token on the NetBox demo instance.

    Args:
        page: Playwright page instance.
        token_name: Name/description for the token.
        timeout: Operation timeout in seconds.

    Returns:
        The token value.

    Raises:
        DemoTokenCreationError: If token creation fails.
    """
    await page.goto(TOKENS_URL, wait_until="domcontentloaded", timeout=timeout * 1000)

    add_link = page.get_by_role("link", name="Add a Token")
    add_button = page.get_by_role("button", name="Add a Token")

    if await add_link.count() > 0:
        await add_link.first.click()
    else:
        await add_button.first.click()

    version_field = page.locator("#id_version")
    if await version_field.count() > 0:
        await version_field.select_option("1")

    token_input = page.locator("#id_token")
    await token_input.wait_for(timeout=timeout * 1000)
    token_value = (await token_input.input_value()).strip()

    if not token_value:
        raise DemoTokenCreationError("Demo token form did not provide a v1 token value.")

    description_field = page.locator("#id_description")
    if await description_field.count() > 0:
        await description_field.fill(token_name)

    create_button = page.get_by_role("button", name="Create")
    await create_button.first.click()

    try:
        await page.wait_for_url(f"{TOKENS_URL}**", timeout=10000)
    except Exception as exc:  # noqa: BLE001
        error_text = _extract_page_error(page)
        if error_text:
            raise DemoTokenCreationError(
                f"NetBox demo rejected token creation: {error_text}"
            ) from exc
        raise DemoTokenCreationError(
            "Timed out waiting for NetBox demo to finish token creation."
        ) from exc

    return token_value


async def create_demo_user(username: str, password: str, headless: bool = True) -> bool:
    """Convenience function to create a demo user.

    Args:
        username: Username to create.
        password: Password for the user.
        headless: Whether to run browser in headless mode.

    Returns:
        True if user was created, False if user already exists.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        page = await browser.new_page()
        try:
            return await _create_demo_user(page, username, password, timeout=60.0)
        finally:
            await browser.close()


async def login(username: str, password: str, headless: bool = True) -> None:
    """Convenience function to log in to demo.

    Args:
        username: Username to log in with.
        password: Password for the user.
        headless: Whether to run browser in headless mode.

    Raises:
        DemoLoginError: If login fails.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        page = await browser.new_page()
        try:
            await _login(page, username, password, timeout=60.0)
        finally:
            await browser.close()


def _extract_page_error(page: "Page") -> str:
    """Extract error text from page content."""
    body_text = page.locator("body").inner_text()
    if "Error" in body_text:
        lines = [line.strip() for line in body_text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if line == "Error" and index + 1 < len(lines):
                return lines[index + 1]
    return ""


def _is_existing_demo_user_error(page: "Page", username: str) -> bool:
    """Check if the page shows an existing user error."""
    body_text = page.locator("body").inner_text().lower()
    return (
        "duplicate key value violates unique constraint" in body_text
        and f"(username)=({username.lower()}) already exists" in body_text
    )


def _parse_v1_token(token_value: str) -> DemoToken:
    """Parse a v1 token from the token input value.

    Args:
        token_value: The raw token value from the form.

    Returns:
        DemoToken with v1 version.

    Raises:
        DemoTokenCreationError: If token format is unexpected.
    """
    stripped = token_value.strip()
    if len(stripped) < 40:
        raise DemoTokenCreationError(
            f"Unexpected v1 token format returned by demo.netbox.dev. Token: {stripped[:20]}..."
        )
    return DemoToken(version="v1", key=None, secret=stripped)


async def bootstrap_demo_profile(
    *,
    username: str,
    password: str,
    timeout: float = 60.0,
    headless: bool = True,
    token_name: str = "proxbox-e2e",
) -> "Config":
    """Bootstrap a demo profile by provisioning a token and creating a Config.

    Args:
        username: The demo username to use.
        password: The demo password to use.
        timeout: Browser operation timeout in seconds.
        headless: Whether to run browser in headless mode.
        token_name: Name for the API token.

    Returns:
        Config object ready for NetBox session creation.
    """
    from netbox_sdk.config import Config, normalize_base_url

    token = await provision_demo_token(
        username=username,
        password=password,
        headless=headless,
        token_name=token_name,
        timeout=timeout,
    )

    return Config(
        base_url=normalize_base_url(DEMO_BASE_URL),
        token_version=token.version,
        token_key=token.key,
        token_secret=token.secret,
        timeout=timeout,
        demo_username=username,
        demo_password=password,
    )


async def refresh_demo_profile(
    existing: "Config",
    *,
    headless: bool = True,
    token_name: str = "proxbox-e2e",
) -> "Config":
    """Re-run demo bootstrap using saved demo credentials.

    Args:
        existing: Existing Config with demo credentials.
        headless: Whether to run browser in headless mode.
        token_name: Name for the API token.

    Returns:
        New Config with refreshed token.

    Raises:
        DemoAuthError: If demo credentials are not available.
    """
    if not existing.demo_username or not existing.demo_password:
        raise DemoAuthError("Saved demo credentials are not available for token refresh.")

    refreshed = await bootstrap_demo_profile(
        username=existing.demo_username,
        password=existing.demo_password,
        timeout=existing.timeout,
        headless=headless,
        token_name=token_name,
    )
    refreshed.demo_username = existing.demo_username
    refreshed.demo_password = existing.demo_password
    return refreshed
