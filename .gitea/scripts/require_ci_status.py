#!/usr/bin/env python3
"""Refuse to deploy a commit that has not passed CI.

N-MultiCloud/nmulticloud-context#204 requirement 6: *"Gate production deployment
on successful verification of the exact deployed SHA. A parallel workflow on the
same push is insufficient."*

Before this gate, ``CI`` and ``Deploy proxbox-api`` were sibling workflows on the
same ``push`` event. They raced, deploy never consulted CI, and neither a red nor
a still-running CI could stop a production rollout.

This polls the Gitea commit-status API for the **exact** SHA about to be deployed
and exits non-zero unless the required context reports ``success``.

It **fails closed**. A missing context, an unreadable API response, or a timeout
all block the deploy — the only way past is the explicit, logged
``skip_ci_gate`` workflow input.

Configured entirely through the environment so nothing reaches a shell:

======================== =========================================================
``DEPLOY_SHA``           commit the deploy will roll out (``github.sha``)
``REQUESTED_REF``        optional ``workflow_dispatch`` ref; must be a full SHA
``SKIP_CI_GATE``         ``true`` to bypass (emergency rollback), logged loudly
``REQUIRED_CI_CONTEXT``  status context that must be green
``API_BASE``             ``<server>/api/v1/repos/<owner>/<repo>``
``GITEA_API_TOKEN``      token for the status read
``CI_GATE_TIMEOUT_SECONDS`` how long to wait for a still-running CI (default 1800)
======================== =========================================================
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

FULL_SHA_LENGTH = 40
_HEX = set("0123456789abcdef")


def _is_full_sha(value: str) -> bool:
    return len(value) == FULL_SHA_LENGTH and all(c in _HEX for c in value.lower())


def latest_status_by_context(payload: object) -> dict[str, str]:
    """Collapse a status list to the newest entry per context.

    Gitea returns statuses newest-first, so the first entry seen for a context
    wins. Accepts both the bare list and the ``{"statuses": [...]}`` envelope.
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("statuses") or []
    else:
        rows = []

    latest: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        context = row.get("context")
        if not context or context in latest:
            continue
        latest[context] = str(row.get("status") or "")
    return latest


def fetch_statuses(api_base: str, sha: str, token: str) -> dict[str, str] | None:
    """Return latest-status-per-context, or ``None`` if it could not be read."""
    url = f"{api_base}/commits/{sha}/statuses?limit=100"
    request = urllib.request.Request(url)  # noqa: S310 - fixed https API base
    if token:
        request.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return latest_status_by_context(json.load(response))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as error:
        print(f"  could not read commit statuses: {error}")
        return None


def resolve_sha(deploy_sha: str, requested_ref: str) -> str:
    """Pick the commit to verify, refusing anything that is not an exact SHA.

    A ``workflow_dispatch`` may target a branch name. Verifying "main" would
    check whatever main points at *now*, which is not necessarily what gets
    deployed — precisely the drift this gate exists to prevent.
    """
    if not requested_ref:
        return deploy_sha
    if _is_full_sha(requested_ref):
        return requested_ref
    raise SystemExit(
        f"ERROR: dispatch ref {requested_ref!r} is not a full {FULL_SHA_LENGTH}-character SHA.\n"
        "       This gate verifies an exact commit; a branch name could move between\n"
        "       the check and the rollout. Re-run with the full SHA, or set\n"
        "       skip_ci_gate=true if this is a deliberate emergency deploy."
    )


def main() -> int:
    """Return 0 only when the deployed SHA has a green required CI status."""
    if os.environ.get("SKIP_CI_GATE", "").lower() == "true":
        sha = os.environ.get("DEPLOY_SHA", "<unknown>")
        print(f"::warning::CI gate explicitly skipped via skip_ci_gate for {sha}")
        return 0

    required = os.environ.get("REQUIRED_CI_CONTEXT", "").strip()
    api_base = os.environ.get("API_BASE", "").rstrip("/")
    if not required or not api_base:
        print("ERROR: REQUIRED_CI_CONTEXT and API_BASE must both be set.")
        return 1

    sha = resolve_sha(
        os.environ.get("DEPLOY_SHA", "").strip(),
        os.environ.get("REQUESTED_REF", "").strip(),
    )
    if not sha:
        print("ERROR: no SHA to verify.")
        return 1

    token = os.environ.get("GITEA_API_TOKEN", "")
    timeout = int(os.environ.get("CI_GATE_TIMEOUT_SECONDS", "1800"))

    print(f"Verifying CI for {sha}")
    print(f"Required context: {required}")

    deadline = time.monotonic() + timeout
    delay = 15

    while True:
        statuses = fetch_statuses(api_base, sha, token)
        state = statuses.get(required) if statuses is not None else None

        if state == "success":
            print(f"CI is green for {sha} - proceeding with deploy.")
            return 0
        if state in {"failure", "error"}:
            print(f"ERROR: CI reported '{state}' for {sha}. Refusing to deploy.")
            return 1
        if state == "pending":
            print(f"  CI still running for {sha}; waiting {delay}s...")
        elif statuses is not None:
            observed = ", ".join(sorted(statuses)) or "(none)"
            print(f"  '{required}' not published yet. Observed contexts: {observed}")

        if time.monotonic() >= deadline:
            print(
                f"ERROR: timed out after {timeout}s waiting for {required!r} to succeed on {sha}.\n"
                "       Failing closed - no deploy. If the context name changed, update\n"
                "       REQUIRED_CI_CONTEXT in .gitea/workflows/deploy-production.yml."
            )
            return 1

        time.sleep(delay)
        delay = min(delay * 2, 60)


if __name__ == "__main__":
    sys.exit(main())
