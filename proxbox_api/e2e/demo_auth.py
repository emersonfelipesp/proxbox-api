"""Utility functions for e2e testing.

Provides helper functions for generating test credentials and passwords.
"""

from __future__ import annotations

import secrets
import string


def generate_username(prefix: str = "proxbox_e2e") -> str:
    """Generate a random username with the given prefix.

    Args:
        prefix: Prefix for the username (default: proxbox_e2e).

    Returns:
        Generated username string.
    """
    suffix = secrets.token_hex(4)
    return f"{prefix}_{suffix}"


def generate_password(length: int = 32) -> str:
    """Generate a random secure password.

    Generates a password with a mix of uppercase, lowercase, and digits.

    Args:
        length: Length of the password (default: 32).

    Returns:
        Generated password string.
    """
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.isupper() for c in password)
            and any(c.islower() for c in password)
            and any(c.isdigit() for c in password)
        ):
            return password
