"""Single source of truth for sensitive key-name matching in secret scrubbers.

The ZFS error scrubber (``proxbox_api/services/zfs.py``), the cloud-provisioning
log scrubber (``proxbox_api/utils/log_scrubbing.py``) and the intent-dispatch
journal scrubber (``proxbox_api/routes/intent/dispatchers/common.py``) all redact
``key = value`` / ``"key": value`` pairs whose *key* names a credential. Keeping
the keyword set here means coverage stays aligned across every scrubber instead
of drifting between parallel regexes.

The bugs this closes: matching only a bare, standalone keyword let compound key
names leak (``secret_key=…``, ``access_token=…``, ``client_secret=…``); and a
naive suffix wildcard over-redacted benign compounds (``token_count``,
``password_policy``). Bare single-word stems therefore accept a *prefix* only,
while genuine two-word secret names (the ``*_key`` family, ``client_secret``,
``token_value``) are an explicit allow-list so structural keys such as
``sort_key``/``primary_key``/``foreign_key`` are never matched.
"""

from __future__ import annotations

import re

# Bare single-word secret stems. In the identifier matcher these may carry a
# PREFIX component (``access_token``, ``my_password``) but NOT a trailing suffix,
# so benign compounds (``token_count``, ``password_policy``, ``secret_santa``)
# are left alone. ``(?:ci)?password`` also covers Proxmox ``cipassword`` (no
# leading ``\b`` — a word boundary would not sit between "ci" and "password").
# ``pveapitoken`` is Proxmox's own no-separator canonical credential name.
_BARE_STEMS = (
    r"(?:ci)?password|passphrase|passwd|pass"
    r"|secret|token|authorization|ticket|cookie|csrfpreventiontoken|pveapitoken"
)
# Compound secret names (the whole two-word name is the secret). The ``*_key``
# family is an explicit ALLOW-LIST so benign structural keys (``sort_key``,
# ``primary_key``, ``foreign_key``) are never matched. The optional ``[_-]?``
# also catches camelCase forms (``apiKey``, ``privateKey``, ``accessKey``).
_COMPOUND = (
    r"client[_-]?secret|token[_-]?value"
    r"|(?:access|secret|api|private|ssh|signing|encryption|master|swift)[_-]?key"
)

# Core alternation for free-text substring scrubbing (log_scrubbing.PASSWORD_LINE_RE).
SECRET_KEY_CORE = _COMPOUND + r"|" + _BARE_STEMS

# Identifier matcher for anchored / word-boundary contexts (zfs key=value + JSON
# key, and dict-key matching). Compounds may carry prefix AND suffix components;
# bare stems carry a prefix and an optional trailing digit run — so rotated /
# versioned names (``access_token2``, ``secret_key_2``, ``api_key2``) match —
# but no word suffix, so ``token_count`` / ``password_policy`` stay unmatched.
SECRET_KEY_IDENT = (
    r"(?:(?:[A-Za-z0-9]+[_-])*(?:" + _COMPOUND + r")(?:[_-][A-Za-z0-9]+|\d+)*)"
    r"|(?:(?:[A-Za-z0-9]+[_-])*(?:" + _BARE_STEMS + r")(?:[_-]?\d+)?)"
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_IDENT_ANCHORED = re.compile(r"(?i)^(?:" + SECRET_KEY_IDENT + r")$")


def is_sensitive_key_name(key: object) -> bool:
    """Whether a dict/mapping key names a credential.

    Matches the shared identifier set, and additionally normalizes camelCase /
    PascalCase names (``PVEAPIToken`` -> ``pveapi_token``, ``apiToken`` ->
    ``api_token``) so no-separator canonical credential names are caught.
    """
    text = str(key)
    if _IDENT_ANCHORED.match(text):
        return True
    return bool(_IDENT_ANCHORED.match(_CAMEL_BOUNDARY.sub("_", text)))


__all__ = ("SECRET_KEY_CORE", "SECRET_KEY_IDENT", "is_sensitive_key_name")
