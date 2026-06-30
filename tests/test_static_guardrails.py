"""Static contract tests for LLM agent safety guardrails.

Read-only file assertions — no FastAPI app startup, no database, no network.
These tests pin the presence of machine-readable LLM safety policy so accidental
deletion of guardrail sections is caught immediately by CI.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AGENTS.md guardrails presence
# ---------------------------------------------------------------------------


def test_agents_md_contains_llm_guardrails_section():
    content = _read("AGENTS.md")
    assert "LLM Agent Safety Guardrails" in content, (
        "AGENTS.md must contain '## LLM Agent Safety Guardrails' section"
    )


def test_agents_md_documents_vm_delete_route():
    content = _read("AGENTS.md")
    assert "DELETE /proxmox/{vm_type}/{vmid}" in content, (
        "AGENTS.md must document the VM delete route as requiring human confirmation"
    )


def test_agents_md_documents_snapshot_delete_route():
    content = _read("AGENTS.md")
    assert "snapshot" in content.lower() and "delete" in content.lower(), (
        "AGENTS.md must document snapshot deletion as requiring human confirmation"
    )


def test_agents_md_documents_allow_writes_trust_boundary():
    content = _read("AGENTS.md")
    assert "allow_writes" in content, (
        "AGENTS.md must document allow_writes as the trust boundary for write verbs"
    )


def test_agents_md_requires_actor_header():
    content = _read("AGENTS.md")
    assert "X-Proxbox-Actor" in content, (
        "AGENTS.md must document X-Proxbox-Actor header requirement on write routes"
    )


def test_agents_md_forbids_autonomous_allow_writes_change():
    content = _read("AGENTS.md")
    assert "Never autonomously" in content or "autonomously set" in content, (
        "AGENTS.md must explicitly prohibit autonomously changing allow_writes"
    )


# ---------------------------------------------------------------------------
# CLAUDE.md safety blockquote
# ---------------------------------------------------------------------------


def test_claude_md_contains_safety_blockquote():
    content = _read("CLAUDE.md")
    assert "LLM Agent Safety" in content, (
        "CLAUDE.md must contain an LLM Agent Safety blockquote at or near the top"
    )


def test_claude_md_references_agents_md_guardrails():
    content = _read("CLAUDE.md")
    assert "AGENTS.md" in content and "Guardrails" in content, (
        "CLAUDE.md safety blockquote must reference AGENTS.md §'LLM Agent Safety Guardrails'"
    )


# ---------------------------------------------------------------------------
# Database model: allow_writes defaults to False
# ---------------------------------------------------------------------------


def test_allow_writes_defaults_false_in_database_model():
    content = _read("proxbox_api/database.py")
    assert "allow_writes: bool = Field(default=False)" in content, (
        "ProxmoxEndpoint.allow_writes must default to False in the database model"
    )


# ---------------------------------------------------------------------------
# Transport access method: api/api_ssh, SSH-only unrepresentable
# ---------------------------------------------------------------------------


def test_access_methods_defaults_to_api_in_database_model():
    content = _read("proxbox_api/database.py")
    assert 'access_methods: str = Field(default="api")' in content, (
        "ProxmoxEndpoint.access_methods must default to 'api' (API only) for new rows"
    )


def test_access_method_enum_has_no_ssh_only_member():
    content = _read("proxbox_api/enum/proxmox.py")
    assert "class ProxmoxAccessMethod" in content, "ProxmoxAccessMethod enum must exist"
    assert 'api = "api"' in content and 'api_ssh = "api_ssh"' in content, (
        "ProxmoxAccessMethod must define exactly api and api_ssh"
    )
    # SSH-only must be unrepresentable: no bare 'ssh' member value.
    assert 'ssh = "ssh"' not in content, "ProxmoxAccessMethod must not define an SSH-only member"


def test_agents_md_documents_access_methods_boundary():
    content = _read("AGENTS.md")
    assert "access_methods" in content and "ssh_not_enabled_for_endpoint" in content, (
        "AGENTS.md must document the access_methods transport boundary and its 403 reason"
    )


# ---------------------------------------------------------------------------
# Security: no forbidden patterns in route handlers
# ---------------------------------------------------------------------------


def test_routes_contain_no_eval():
    # Security assertion: checks that the string literal "eval(" is absent from
    # route handlers. This test itself never calls eval — it scans source text
    # to ensure the forbidden pattern was never introduced.
    content = _read("proxbox_api/routes/proxmox_actions.py")
    assert "eval(" not in content, "proxmox_actions.py must not use eval()"


def test_routes_contain_no_os_system():
    # Security assertion: checks that "os.system(" is absent from route
    # handlers. This test never calls os.system — it scans source text to
    # ensure the command-injection sink was never introduced.
    content = _read("proxbox_api/routes/proxmox_actions.py")
    assert "os.system(" not in content, "proxmox_actions.py must not use os.system()"


def test_database_contains_no_eval():
    # Security assertion: checks source text for the forbidden pattern.
    content = _read("proxbox_api/database.py")
    assert "eval(" not in content, "database.py must not use eval()"
