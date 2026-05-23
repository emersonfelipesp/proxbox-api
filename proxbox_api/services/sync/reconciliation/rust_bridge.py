"""Rust reconciliation bridge placeholder.

The native bridge is introduced after the Python queue contract and service
seam are stable.
"""

from __future__ import annotations


def rust_available() -> bool:
    """Return whether the optional Rust reconciliation extension is importable."""

    return False
