"""Helpers for removing cloud-init secrets before logging or journaling."""

from __future__ import annotations

import copy
import re

SENSITIVE_KEY_RE = re.compile(r"^(password|cipassword|secret|token)$", re.IGNORECASE)
PASSWORD_LINE_RE = re.compile(r"(?im)(\bpassword\s*:\s*)[^\r\n]+")


def _scrub_value(value: object) -> object:
    if isinstance(value, dict):
        scrubbed: dict[object, object] = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.match(str(key)):
                scrubbed[key] = "***"
            else:
                scrubbed[key] = _scrub_value(item)
        return scrubbed
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, str):
        return PASSWORD_LINE_RE.sub(r"\1***", value)
    return copy.deepcopy(value)


def scrub_cloud_init(d: dict) -> dict:
    """Return a deep-scrubbed copy of a payload that may contain cloud-init data."""
    scrubbed = _scrub_value(d)
    if isinstance(scrubbed, dict):
        return scrubbed
    return {}
