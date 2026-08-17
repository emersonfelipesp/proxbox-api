#!/usr/bin/env python3
"""Bind release authorization to authenticated Gitea CI run and job records."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_ORIGIN = "https://git.nmulti.cloud/api/v1"
HTML_ORIGIN = "https://git.nmulti.cloud"
GITHUB_API_ORIGIN = "https://api.github.com"
GITHUB_HTML_ORIGIN = "https://github.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SHA_RE = re.compile(r"^[a-f0-9]{40}$")
FIRST_JOB_ATTEMPT = 1
FIRST_RUN_ATTEMPT_ENCODINGS = frozenset({0, 1})


class CIGateError(ValueError):
    """Authenticated Gitea evidence violates the release CI contract."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _request_json(path: str, *, token: str) -> Any:
    if not token:
        raise CIGateError("Gitea API token is unavailable")
    if not path.startswith("/repos/") or ".." in path:
        raise CIGateError("Gitea API path is invalid")
    request = urllib.request.Request(
        f"{API_ORIGIN}{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"token {token}",
            "User-Agent": "release-ci-gate/1",
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            if response.status != 200:
                raise CIGateError(f"Gitea returned HTTP {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CIGateError("Gitea CI evidence request failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CIGateError("Gitea CI evidence exceeds its size bound")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CIGateError("Gitea CI evidence is not valid JSON") from exc


def _request_github_json(path: str) -> Any:
    if not path.startswith("/repos/emersonfelipesp/proxbox-api/") or ".." in path:
        raise CIGateError("GitHub API path is invalid")
    request = urllib.request.Request(
        f"{GITHUB_API_ORIGIN}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "release-ci-gate/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            if response.status != 200:
                raise CIGateError(f"GitHub returned HTTP {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CIGateError("GitHub CI evidence request failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CIGateError("GitHub CI evidence exceeds its size bound")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CIGateError("GitHub CI evidence is not valid JSON") from exc


def _validate_github_offline_image(
    *, source_sha: str, trusted_actor: str, required_job: str
) -> dict[str, int]:
    query = urllib.parse.urlencode(
        {
            "branch": "develop",
            "event": "push",
            "head_sha": source_sha,
            "page": 1,
            "per_page": 100,
        }
    )
    payload = _request_github_json(f"/repos/emersonfelipesp/proxbox-api/actions/runs?{query}")
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    total_count = payload.get("total_count") if isinstance(payload, dict) else None
    if (
        not isinstance(runs, list)
        or isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count != len(runs)
        or total_count > 100
    ):
        raise CIGateError("GitHub Actions run inventory is incomplete")
    candidates: list[dict[str, Any]] = []
    for run in runs:
        actor = run.get("actor") if isinstance(run, dict) else None
        repository = run.get("repository") if isinstance(run, dict) else None
        head_repository = run.get("head_repository") if isinstance(run, dict) else None
        run_id = run.get("id") if isinstance(run, dict) else None
        run_attempt = run.get("run_attempt") if isinstance(run, dict) else None
        if (
            isinstance(run, dict)
            and isinstance(run_id, int)
            and not isinstance(run_id, bool)
            and run_id > 0
            and run.get("path") == ".github/workflows/ci.yml"
            and run.get("event") == "push"
            and run.get("head_branch") == "develop"
            and run.get("head_sha") == source_sha
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and isinstance(run_attempt, int)
            and not isinstance(run_attempt, bool)
            and run_attempt == 1
            and isinstance(actor, dict)
            and actor.get("login") == trusted_actor
            and isinstance(repository, dict)
            and repository.get("full_name") == "emersonfelipesp/proxbox-api"
            and isinstance(head_repository, dict)
            and head_repository.get("full_name") == "emersonfelipesp/proxbox-api"
        ):
            candidates.append(run)
    if not candidates:
        raise CIGateError("Required GitHub Actions workflow run is missing")
    latest = max(candidates, key=lambda row: int(row["id"]))
    run_id = int(latest["id"])
    jobs_payload = _request_github_json(
        f"/repos/emersonfelipesp/proxbox-api/actions/runs/{run_id}/jobs?per_page=100&page=1"
    )
    jobs = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    jobs_total = jobs_payload.get("total_count") if isinstance(jobs_payload, dict) else None
    if (
        not isinstance(jobs, list)
        or isinstance(jobs_total, bool)
        or not isinstance(jobs_total, int)
        or jobs_total != len(jobs)
        or jobs_total > 100
    ):
        raise CIGateError("GitHub Actions job inventory is incomplete")
    matches = [job for job in jobs if isinstance(job, dict) and job.get("name") == required_job]
    if len(matches) != 1:
        raise CIGateError("Required GitHub Actions job is missing or ambiguous")
    job = matches[0]
    job_id = job.get("id")
    labels = job.get("labels")
    if (
        isinstance(job_id, bool)
        or not isinstance(job_id, int)
        or job_id <= 0
        or job.get("run_id") != run_id
        or job.get("run_attempt") != 1
        or isinstance(job.get("run_attempt"), bool)
        or job.get("head_sha") != source_sha
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or not isinstance(labels, list)
        or "ubuntu-latest" not in labels
        or job.get("runner_group_name") != "GitHub Actions"
        or job.get("html_url")
        != f"{GITHUB_HTML_ORIGIN}/emersonfelipesp/proxbox-api/actions/runs/{run_id}/job/{job_id}"
    ):
        raise CIGateError("Required GitHub Actions job identity is invalid")
    return {"job_id": job_id, "run_attempt": 1, "run_id": run_id}


def _expected_job_name(context: str) -> str:
    prefix, suffix = "CI / ", " (push)"
    if not context.startswith(prefix) or not context.endswith(suffix):
        raise CIGateError(f"Unsupported release CI context: {context!r}")
    name = context[len(prefix) : -len(suffix)]
    if not name:
        raise CIGateError("Release CI job name is empty")
    return name


def _latest_workflow_run(payload: object, *, source_sha: str, trusted_actor: str) -> dict[str, Any]:
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    total_count = payload.get("total_count") if isinstance(payload, dict) else None
    if (
        not isinstance(runs, list)
        or not runs
        or isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count != len(runs)
        or total_count > 100
    ):
        raise CIGateError("Gitea Actions run inventory is incomplete")
    candidates: list[dict[str, Any]] = []
    observed_ids: set[int] = set()
    for run in runs:
        run_id = run.get("id") if isinstance(run, dict) else None
        if (
            not isinstance(run, dict)
            or isinstance(run_id, bool)
            or not isinstance(run_id, int)
            or run_id <= 0
            or run_id in observed_ids
        ):
            raise CIGateError("Gitea Actions run inventory is malformed")
        observed_ids.add(run_id)
        if (
            run.get("path") == "ci.yml@refs/heads/develop"
            and run.get("head_sha") == source_sha
            and run.get("head_branch") == "develop"
            and run.get("event") == "push"
        ):
            candidates.append(run)
    if not candidates:
        raise CIGateError("Required Gitea Actions workflow run is missing")
    latest = max(candidates, key=lambda row: int(row["id"]))
    _validate_run(
        latest,
        context="latest required workflow run",
        run_id=int(latest["id"]),
        source_sha=source_sha,
        trusted_actor=trusted_actor,
    )
    return latest


def _required_jobs(payload: object, *, required_contexts: list[str]) -> dict[str, dict[str, Any]]:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    total_count = payload.get("total_count") if isinstance(payload, dict) else None
    if (
        not isinstance(jobs, list)
        or not jobs
        or isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count != len(jobs)
        or total_count > 100
    ):
        raise CIGateError("Gitea Actions job inventory is incomplete")
    expected = {_expected_job_name(context): context for context in required_contexts}
    selected: dict[str, dict[str, Any]] = {}
    observed_ids: set[int] = set()
    for job in jobs:
        job_id = job.get("id") if isinstance(job, dict) else None
        if (
            not isinstance(job, dict)
            or isinstance(job_id, bool)
            or not isinstance(job_id, int)
            or job_id <= 0
            or job_id in observed_ids
        ):
            raise CIGateError("Gitea Actions job inventory is malformed")
        observed_ids.add(job_id)
        name = job.get("name")
        if name in expected:
            if name in selected:
                raise CIGateError("Required Gitea Actions job is ambiguous")
            selected[str(name)] = job
    if set(selected) != set(expected):
        raise CIGateError("Required Gitea Actions job is missing")
    return {expected[name]: job for name, job in selected.items()}


def _validate_run(
    run: object,
    *,
    context: str,
    run_id: int,
    source_sha: str,
    trusted_actor: str,
) -> None:
    actor = run.get("actor") if isinstance(run, dict) else None
    expected = {
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_sha": source_sha,
        "head_branch": "develop",
        "path": "ci.yml@refs/heads/develop",
    }
    if not isinstance(run, dict) or any(run.get(key) != value for key, value in expected.items()):
        raise CIGateError(f"Gitea CI run does not match the release source: {context}")
    observed_run_id = run.get("id")
    if (
        isinstance(observed_run_id, bool)
        or not isinstance(observed_run_id, int)
        or observed_run_id != run_id
    ):
        raise CIGateError(f"Gitea CI run does not match the release source: {context}")
    if not isinstance(actor, dict) or actor.get("login") != trusted_actor:
        raise CIGateError(f"Gitea CI run actor is not trusted: {context}")
    run_attempt = run.get("run_attempt")
    if (
        isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt not in FIRST_RUN_ATTEMPT_ENCODINGS
    ):
        raise CIGateError(f"Gitea CI run attempt is invalid: {context}")


def _validate_job(
    job: object,
    *,
    context: str,
    owner: str,
    repository: str,
    run_id: int,
    job_id: int,
    source_sha: str,
) -> None:
    expected = {
        "name": _expected_job_name(context),
        "status": "completed",
        "conclusion": "success",
        "head_sha": source_sha,
    }
    if not isinstance(job, dict) or any(job.get(key) != value for key, value in expected.items()):
        raise CIGateError(f"Gitea CI job does not match its status: {context}")
    strict_integer_fields = {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": FIRST_JOB_ATTEMPT,
    }
    if any(
        isinstance(job.get(key), bool) or not isinstance(job.get(key), int) or job.get(key) != value
        for key, value in strict_integer_fields.items()
    ):
        raise CIGateError(f"Gitea CI job does not match its status: {context}")
    labels = job.get("labels")
    runner_name = job.get("runner_name")
    if (
        labels != ["ci-untrusted-python312"]
        or not isinstance(runner_name, str)
        or not runner_name.startswith("ci-untrusted-")
    ):
        raise CIGateError(f"Gitea CI job did not use the trusted CI runner class: {context}")
    expected_target = f"{HTML_ORIGIN}/{owner}/{repository}/actions/runs/{run_id}/jobs/{job_id}"
    if job.get("html_url") != expected_target:
        raise CIGateError(f"Gitea CI job URL does not match its status: {context}")


def validate_ci_gate(
    *,
    owner: str,
    repository: str,
    source_sha: str,
    required_contexts: list[str],
    trusted_actor: str,
    token: str,
    github_required_job: str | None = None,
) -> dict[str, dict[str, int]]:
    """Require the latest authenticated Actions run and its trusted CI jobs."""
    if SHA_RE.fullmatch(source_sha) is None:
        raise CIGateError("Release source SHA must be canonical lowercase 40-hex")
    if not required_contexts or len(required_contexts) != len(set(required_contexts)):
        raise CIGateError("Required CI contexts must be a non-empty unique set")
    query = urllib.parse.urlencode(
        {
            "branch": "develop",
            "event": "push",
            "head_sha": source_sha,
            "limit": 100,
            "page": 1,
        }
    )
    latest_run = _latest_workflow_run(
        _request_json(f"/repos/{owner}/{repository}/actions/runs?{query}", token=token),
        source_sha=source_sha,
        trusted_actor=trusted_actor,
    )
    run_id = int(latest_run["id"])
    jobs = _required_jobs(
        _request_json(f"/repos/{owner}/{repository}/actions/runs/{run_id}/jobs", token=token),
        required_contexts=required_contexts,
    )

    evidence: dict[str, dict[str, int]] = {}
    for context in required_contexts:
        job = jobs[context]
        job_id = int(job["id"])
        _validate_job(
            job,
            context=context,
            owner=owner,
            repository=repository,
            run_id=run_id,
            job_id=job_id,
            source_sha=source_sha,
        )
        evidence[context] = {
            "job_id": job_id,
            "run_attempt": FIRST_JOB_ATTEMPT,
            "run_id": run_id,
        }
    if github_required_job is not None:
        if github_required_job != "Build extracted offline release sdist":
            raise CIGateError("Required GitHub Actions job is not allowlisted")
        evidence[f"GitHub CI / {github_required_job} (push)"] = _validate_github_offline_image(
            source_sha=source_sha,
            trusted_actor=trusted_actor,
            required_job=github_required_job,
        )
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--required-context", action="append", required=True)
    parser.add_argument("--trusted-actor", required=True)
    parser.add_argument("--github-required-job", required=True)
    args = parser.parse_args()
    evidence = validate_ci_gate(
        owner=args.owner,
        repository=args.repository,
        source_sha=args.source_sha,
        required_contexts=args.required_context,
        trusted_actor=args.trusted_actor,
        token=os.getenv("GITEA_API_TOKEN", ""),
        github_required_job=args.github_required_job,
    )
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
