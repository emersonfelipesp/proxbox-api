"""Guards for the restored in-repository publish path.

The locked control plane is deferred until the isolated runner fleet exists, so
these assert the properties that actually hold for the workflow that ships —
specifically the two ways it could silently do the wrong thing: pin a job to a
label nobody advertises (which queues forever rather than failing), or publish
to somewhere other than the single authorised destination.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".gitea/workflows/publish-gitea.yml"

# Labels advertised by runners that exist today.
AVAILABLE_LABELS = {"mirror-host", "prod-deploy", "ci-untrusted-python312"}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_every_job_targets_a_runner_label_that_exists() -> None:
    jobs = _workflow()["jobs"]
    assert jobs, "publish workflow defines no jobs"
    unschedulable = {
        name: spec.get("runs-on")
        for name, spec in jobs.items()
        if spec.get("runs-on") not in AVAILABLE_LABELS
    }
    assert not unschedulable, (
        f"jobs pinned to labels no runner advertises: {unschedulable}. "
        "Such a job queues forever instead of failing, and the tag is consumed "
        "for nothing."
    )


def test_only_one_automatic_tag_trigger() -> None:
    """Gitea emits both `create` and `push` for a tag.

    Subscribing to both starts two immutable uploads of the same version; the
    second cannot succeed because package versions are immutable.
    """
    trigger_block = WORKFLOW.read_text(encoding="utf-8").split("jobs:", 1)[0]
    assert "push:" in trigger_block
    assert "tags:" in trigger_block
    assert "create:" not in trigger_block


def test_workflow_publishes_to_the_gitea_package_registry() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "api/packages" in text
    assert "twine upload" in text


def test_github_push_targets_only_the_authorised_destination() -> None:
    """The EdgeUno fork is read-only context and never a destination."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "edgeuno" not in text.lower(), (
        "the publish workflow references EdgeUno; it is a read-only reference "
        "fork and is never a push, tag, release, or mirror destination"
    )


def test_release_is_validated_before_publication() -> None:
    jobs = _workflow()["jobs"]
    assert "validate-version" in jobs
    assert "validate-version" in jobs["publish-gitea"]["needs"]
