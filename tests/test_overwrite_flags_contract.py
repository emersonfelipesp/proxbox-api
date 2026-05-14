"""Cross-repo drift detector for the overwrite_* flag set.

The plugin (`netbox-proxbox`) and the backend (`proxbox-api`) each carry the
same canonical 24-flag list as a single source of truth:

- Plugin: `netbox_proxbox.constants.OVERWRITE_FIELDS`
- Backend: `proxbox_api.schemas.sync.SyncOverwriteFlags.model_fields`

A copy of the canonical names + order is committed to BOTH repos as
`contracts/overwrite_flags.json`. This test asserts that the local source of
truth on this side matches the manifest exactly. The mirror repo runs the same
test against its own source of truth. Any developer who adds another flag to
either side must update both manifests; CI on both repos fails otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

from proxbox_api.schemas.sync import SyncOverwriteFlags


def _load_manifest_fields() -> tuple[str, ...]:
    manifest_path = Path(__file__).resolve().parent.parent / "contracts" / "overwrite_flags.json"
    raw = manifest_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    fields = payload["fields"]
    assert isinstance(fields, list) and all(isinstance(name, str) for name in fields)
    return tuple(fields)


def test_manifest_matches_sync_overwrite_flags_model_fields() -> None:
    """Backend source of truth must match the committed cross-repo manifest."""
    manifest_fields = _load_manifest_fields()
    schema_fields = tuple(SyncOverwriteFlags.model_fields.keys())
    assert schema_fields == manifest_fields, (
        "SyncOverwriteFlags.model_fields drifted from contracts/overwrite_flags.json. "
        "Update BOTH repo manifests (proxbox-api and netbox-proxbox) when changing flags."
    )


def test_manifest_field_count_is_canonical_25() -> None:
    """Sanity check: any change to flag count is intentional and reviewed."""
    manifest_fields = _load_manifest_fields()
    assert len(manifest_fields) == 25


def test_manifest_has_no_duplicate_fields() -> None:
    manifest_fields = _load_manifest_fields()
    assert len(manifest_fields) == len(set(manifest_fields))
