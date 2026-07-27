"""Helpers for removing cloud-init secrets before logging or journaling."""

from __future__ import annotations

import copy
import re

from proxbox_api.utils.secret_keywords import SECRET_KEY_CORE, is_sensitive_key_name

# Redact credential values inside free-text strings (e.g. a stringified upstream
# error or an SSE frame) where the secret is NOT under its own dict key — the
# common case, since call sites stringify errors as ``{"e": str(error)}`` and only
# this free-text pass then runs. Covers bare (``token: x``), ``=`` separators, and
# quoted dict-repr forms (``'client_secret': 'x'``) for every keyword in
# SECRET_KEY_CORE, including compound names (``access_token=``). No leading ``\b``
# on purpose: a word boundary would NOT sit between "ci" and "password", so
# ``cipassword`` would never be matched — the exact gap that leaked the Proxmox
# cipassword into 502 bodies / SSE frames. Over-matching a compound key only
# over-redacts, which is safe.
PASSWORD_LINE_RE = re.compile(
    r"(?im)(['\"]?(?:" + SECRET_KEY_CORE + r")(?:[_-]?\d+)?['\"]?\s*[:=]\s*['\"]?)[^\r\n,}\]\"']+"
)


def _scrub_value(value: object) -> object:
    if isinstance(value, dict):
        scrubbed: dict[object, object] = {}
        for key, item in value.items():
            if is_sensitive_key_name(key):
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
