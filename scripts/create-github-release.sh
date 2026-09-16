#!/usr/bin/env bash

set -euo pipefail

readonly GH_REPO="emersonfelipesp/proxbox-api"
RELEASE_HEADERS_FILE=""
RELEASE_NOTES_FILE=""

cleanup_release_files() {
  rm -f -- "${RELEASE_HEADERS_FILE}" "${RELEASE_NOTES_FILE}"
}

resolve_exact_tag_commit() {
  local tag="$1"
  local object_type
  local object_sha
  local ref_object

  if ! ref_object="$(
    gh api --method GET \
      "repos/${GH_REPO}/git/ref/tags/${tag}" \
      --jq '.object | [.type, .sha] | @tsv'
  )"; then
    echo "Failed to resolve exact GitHub ref refs/tags/${tag}." >&2
    return 1
  fi
  read -r object_type object_sha <<<"${ref_object}"

  for _depth in {1..8}; do
    case "${object_type}" in
      commit)
        if [[ "${object_sha}" =~ ^[0-9a-fA-F]{40}$ ]]; then
          printf '%s\n' "${object_sha,,}"
          return 0
        fi
        echo "GitHub returned an invalid commit object for refs/tags/${tag}." >&2
        return 1
        ;;
      tag)
        if ! ref_object="$(
          gh api --method GET \
            "repos/${GH_REPO}/git/tags/${object_sha}" \
            --jq '.object | [.type, .sha] | @tsv'
        )"; then
          echo "Failed to peel annotated GitHub tag object ${object_sha}." >&2
          return 1
        fi
        read -r object_type object_sha <<<"${ref_object}"
        ;;
      *)
        echo "GitHub tag ${tag} points to unsupported object type ${object_type:-<none>}." >&2
        return 1
        ;;
    esac
  done

  echo "GitHub tag ${tag} exceeded the eight-object peel limit." >&2
  return 1
}

fetch_approved_notes() {
  local tag="$1"
  local approved_sha="$2"
  local output_file="$3"
  local notes_path="docs/release-notes/version-${tag#v}.md"

  if ! gh api --method GET \
    --header 'Accept: application/vnd.github.raw+json' \
    "repos/${GH_REPO}/contents/${notes_path}?ref=${approved_sha}" >"${output_file}"; then
    echo "Failed to load ${notes_path} from approved GitHub commit ${approved_sha}." >&2
    return 1
  fi
  if [[ ! -s "${output_file}" ]]; then
    echo "Approved release notes ${notes_path} are empty; refusing publication." >&2
    return 1
  fi
}

require_absent_release() {
  local tag="$1"
  local response_headers="$2"
  local lookup_status

  if gh api --include --silent --method GET \
    "repos/${GH_REPO}/releases/tags/${tag}" >"${response_headers}"; then
    echo "GitHub Release ${tag} already exists in ${GH_REPO}; inspect it instead of creating another." >&2
    return 1
  fi

  lookup_status="$(
    awk '/^HTTP\// { status = $2 } END { print status }' "${response_headers}"
  )"
  if [[ "${lookup_status}" != "404" ]]; then
    echo "GitHub Release lookup failed without an explicit HTTP 404; refusing to create ${tag}." >&2
    echo "Observed HTTP status: ${lookup_status:-<none>}" >&2
    return 1
  fi
}

main() {
  if [[ "$#" -ne 2 ]]; then
    echo "Usage: $0 vX.Y.Z[.postN] <production-approved-commit-sha>" >&2
    return 2
  fi

  local tag="$1"
  local approved_sha="${2,,}"
  local github_tag_sha

  if [[ ! "${tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(\.post[0-9]+)?$ ]]; then
    echo "Refusing invalid final release tag: ${tag}" >&2
    return 2
  fi
  if [[ ! "${approved_sha}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Refusing invalid production-approved commit SHA: ${approved_sha}" >&2
    return 2
  fi

  if ! github_tag_sha="$(resolve_exact_tag_commit "${tag}")"; then
    echo "Failed to resolve the GitHub tag commit for ${tag}; refusing to create a Release." >&2
    return 4
  fi
  if [[ "${github_tag_sha}" != "${approved_sha}" ]]; then
    echo "GitHub tag ${tag} resolves to ${github_tag_sha}, not approved commit ${approved_sha}; refusing to create a Release." >&2
    return 4
  fi

  RELEASE_HEADERS_FILE="$(mktemp)"
  RELEASE_NOTES_FILE="$(mktemp)"
  trap cleanup_release_files EXIT

  fetch_approved_notes "${tag}" "${approved_sha}" "${RELEASE_NOTES_FILE}" || return 4
  require_absent_release "${tag}" "${RELEASE_HEADERS_FILE}" || return 4

  gh release create "${tag}" \
    --repo "${GH_REPO}" \
    --verify-tag \
    --target main \
    --title "${tag}" \
    --notes-file "${RELEASE_NOTES_FILE}"
}

main "$@"
