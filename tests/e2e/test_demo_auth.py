"""E2E tests for utility functions.

Tests the helper functions for e2e testing.
"""

from __future__ import annotations

from proxbox_api.e2e.demo_auth import generate_password, generate_username


class TestE2EHelpers:
    """Tests for e2e helper functions."""

    def test_generate_username_format(self):
        """Test username generation produces valid format."""
        username = generate_username()
        assert username.startswith("proxbox_e2e_")
        assert len(username) > len("proxbox_e2e_")

    def test_generate_username_custom_prefix(self):
        """Test username generation with custom prefix."""
        username = generate_username(prefix="test_prefix")
        assert username.startswith("test_prefix_")
        assert len(username) > len("test_prefix_")

    def test_generate_password_length(self):
        """Test password generation produces adequate length."""
        password = generate_password()
        assert len(password) == 32
        assert any(c.isupper() for c in password)
        assert any(c.islower() for c in password)
        assert any(c.isdigit() for c in password)

    def test_generate_password_custom_length(self):
        """Test password generation with custom length."""
        password = generate_password(length=16)
        assert len(password) == 16

    def test_generate_password_contains_required_chars(self):
        """Test password contains uppercase, lowercase, and digits."""
        password = generate_password(length=64)
        assert any(c.isupper() for c in password)
        assert any(c.islower() for c in password)
        assert any(c.isdigit() for c in password)
