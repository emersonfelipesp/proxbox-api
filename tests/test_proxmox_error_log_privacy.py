"""Upstream Proxmox error construction must not disclose private messages."""

from proxbox_api.exception import ProxboxException, ProxmoxAPIError, logger


def test_proxmox_error_construction_redacts_all_private_log_fields(monkeypatch):
    messages = []
    monkeypatch.setattr(logger, "debug", messages.append)
    original = RuntimeError("private-upstream-canary")
    error = ProxmoxAPIError(
        "private-message-canary", endpoint="private-endpoint-canary", original_error=original
    )
    logged = "".join(messages)
    assert "private-message-canary" not in logged
    assert "private-endpoint-canary" not in logged
    assert "private-upstream-canary" not in logged
    assert "ProxmoxAPIError" in logged
    assert error.message == "private-message-canary"
    assert error.original_error is original


def test_explicit_redaction_also_protects_base_exception_message(monkeypatch):
    messages = []
    monkeypatch.setattr(logger, "debug", messages.append)
    ProxboxException(
        "private-message-canary", detail="private-detail-canary", redact_log_details=True
    )
    logged = "".join(messages)
    assert "private-message-canary" not in logged
    assert "private-detail-canary" not in logged
    assert "ProxboxException" in logged
