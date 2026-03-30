"""Package utilities: re-export helpers from the legacy flat ``utils.py`` module."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_LEGACY_PATH = Path(__file__).resolve().parent.parent / "utils.py"
_spec = importlib.util.spec_from_file_location(
    "proxbox_api._legacy_utils_module",
    _LEGACY_PATH,
)
_legacy = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(_legacy)

return_status_html = _legacy.return_status_html

__all__ = ["return_status_html"]
