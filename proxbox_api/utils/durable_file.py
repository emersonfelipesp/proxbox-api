"""Small helpers for durably publishing security-sensitive local files."""

from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes before a caller publishes durable state."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
