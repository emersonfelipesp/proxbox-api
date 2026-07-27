"""Tests for the intent-dispatch journal scrubber.

`routes/intent/dispatchers/common.py` writes error detail into NetBox journal
comments; it must recognise the same compound / multi-category secret keys as the
shared scrubber (issue #272), not a narrow hardcoded set.
"""

from __future__ import annotations

from proxbox_api.routes.intent.dispatchers.common import (
    collect_sensitive_values,
    scrub_message,
)


def test_collect_sensitive_values_covers_compound_and_category_keys():
    payload = {
        "api_key": "AK-SEKRET",
        "secret_key": "SK-SEKRET",
        "client_secret": "CS-SEKRET",
        "private_key": "PK-SEKRET",
        "cipassword": "CI-SEKRET",
        "passphrase": "PP-SEKRET",
        "node": "pve1",
    }
    found = collect_sensitive_values(payload)
    for secret in ("AK-SEKRET", "SK-SEKRET", "CS-SEKRET", "PK-SEKRET", "CI-SEKRET", "PP-SEKRET"):
        assert secret in found, secret
    assert "pve1" not in found


def test_scrub_message_redacts_payload_secret_by_cross_reference():
    # A secret echoed in a free-text upstream error is scrubbed via the payload
    # cross-reference even when it is not in key=value shape in the message.
    msg = "node pve1 rejected parameter value 'AK-SEKRET' as invalid"
    scrubbed = scrub_message(msg, {"api_key": "AK-SEKRET", "node": "pve1"})
    assert "AK-SEKRET" not in scrubbed
    assert "***" in scrubbed
    assert "pve1" in scrubbed  # non-secret text preserved


def test_scrub_message_redacts_cipassword_and_categories_in_text():
    for text, leaked in [
        ("cipassword: CI-SEKRET", "CI-SEKRET"),
        ("token=TK-SEKRET", "TK-SEKRET"),
        ("client_secret=CS-SEKRET", "CS-SEKRET"),
    ]:
        scrubbed = scrub_message(text)
        assert leaked not in scrubbed, text
        assert "***" in scrubbed, text
