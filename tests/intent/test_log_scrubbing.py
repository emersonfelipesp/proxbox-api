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
