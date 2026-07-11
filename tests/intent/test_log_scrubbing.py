"""Cloud-init log scrubbing tests."""

from __future__ import annotations

from proxbox_api.services.verb_dispatch import write_verb_journal_entry
from proxbox_api.utils.log_scrubbing import scrub_cloud_init


def test_scrub_cloud_init_recurses_and_scrubs_password_lines():
    payload = {
        "cloud_init": {
            "cipassword": "ci-secret",
            "user_data": "#cloud-config\npassword: plain-secret\nchpasswd: { expire: False }\n",
            "nested": {
                "password": "nested-secret",
                "items": [{"secret": "list-secret"}, {"token": "list-token"}],
            },
        }
    }

    scrubbed = scrub_cloud_init(payload)

    assert scrubbed is not payload
    assert payload["cloud_init"]["cipassword"] == "ci-secret"
    assert scrubbed["cloud_init"]["cipassword"] == "***"
    assert scrubbed["cloud_init"]["nested"]["password"] == "***"
    assert scrubbed["cloud_init"]["nested"]["items"] == [{"secret": "***"}, {"token": "***"}]
    assert "password: ***" in scrubbed["cloud_init"]["user_data"]
    assert "plain-secret" not in repr(scrubbed)
    assert "nested-secret" not in repr(scrubbed)
    assert "list-secret" not in repr(scrubbed)
    assert "list-token" not in repr(scrubbed)


def test_scrub_cloud_init_redacts_cipassword_in_error_strings():
    # Regression: provision.py / provision_stream.py stringify the upstream
    # error BEFORE scrubbing, so only the string path runs. The old
    # `\bpassword` regex never matched `cipassword` (no boundary between "ci"
    # and "password"), leaking the Proxmox cipassword into 502 bodies and SSE
    # frames. Cover the bare, dict-repr, and `=` forms.
    cases = [
        "HTTP 400 Bad Request: cipassword: s3cret-pw is not acceptable",
        "HTTP 400 Bad Request: {'cipassword': 's3cret-pw'}",
        'proxmox error: "cipassword"="s3cret-pw"',
        "password: s3cret-pw",
    ]
    for text in cases:
        scrubbed = scrub_cloud_init({"error": text})
        assert "s3cret-pw" not in scrubbed["error"], text
        assert "***" in scrubbed["error"], text


async def test_write_verb_journal_entry_scrubs_comments_at_write_boundary(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_rest_create_async(nb, path, payload):
        del nb
        captured["path"] = path
        captured["payload"] = payload
        return {"id": 1, **payload}

    monkeypatch.setattr(
        "proxbox_api.services.verb_dispatch.rest_create_async",
        fake_rest_create_async,
    )

    result = await write_verb_journal_entry(
        object(),
        netbox_vm_id=101,
        kind="warning",
        comments=(
            "cloud_init:\n"
            "  user_data: |\n"
            "    password: boundary-secret\n"
            "- error_detail: password: second-boundary-secret\n"
        ),
    )

    journal_payload = captured["payload"]
    assert isinstance(journal_payload, dict)
    assert captured["path"] == "/api/extras/journal-entries/"
    assert result is not None
    assert "boundary-secret" not in journal_payload["comments"]
    assert "second-boundary-secret" not in journal_payload["comments"]
    assert "password: ***" in journal_payload["comments"]
