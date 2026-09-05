# Version 0.0.20

proxbox-api `0.0.20` pairs with `netbox-proxbox 0.0.24`,
`proxmox-sdk 0.0.13`, and `netbox-sdk 0.0.13`. The package supports Python
3.12 and 3.13 and certifies its NetBox integration through NetBox 4.6.6.

## Compatibility and reliability

- Adds NetBox 4.6.6 to the generated custom-field object-type and E2E
  compatibility matrices.
- Keeps the package resolver bounded to supported Python 3.12 and 3.13
  runtimes while upgrading to `netbox-sdk 0.0.13`. The service explicitly
  selects the NetBox 4.6 schema used by its certified deployment matrix, so the
  SDK's new NetBox 4.7 fallback default does not change live client behavior.
- Makes deterministic Proxmox tag styling work in FIPS environments by marking
  the non-security MD5 use explicitly.
- Generates the large E2E matrix through a tested Python helper, with pull
  requests following the same bounded untrusted matrix as ordinary pushes and
  release-only expansion reserved for published candidates.

## Release integrity

- Builds one wheel and one sdist from the exact tagged commit and publishes them
  to the Gitea Package Registry, which remains the artifact of record.
- Validates the tag against a strict version pattern before anything is built,
  and verifies the package is present in the registry after upload.
- Subscribes to the tag `push` event only. Gitea emits both `create` and `push`
  for a tag, and subscribing to both would start two immutable uploads for one
  version.
- Creates the public GitHub Release for final tags only, so a release candidate
  never reaches PyPI as though it were final.
- Builds the public-index distributions from the same tagged commit, so what
  reaches TestPyPI and PyPI corresponds to the published source rather than a
  separately fetched copy. Uploads never use `--skip-existing`, so a failure
  always advances to a new immutable version rather than silently succeeding.

### Known limitation: publication hardening is deferred

This release publishes through in-repository jobs on the existing shared
runners — the same path used by `0.0.19` and every currently published version.
The locked release control plane developed during this cycle is **not** active,
because the isolated runner fleet it requires does not exist yet: no runner
advertises `ci-release-proxbox-api`, and the control repository has no runners.

Specifically, this version does **not** yet have: credential-free target builds,
an exact-byte sealed handoff to a separately administered isolated publisher,
supervisor-signed runner attestation, or egress-denied build isolation. Those
controls are implemented and reviewed in-tree and re-land once the isolated
runners are provisioned.

This is a deliberate, tracked deferral rather than a regression: `0.0.20` ships
at the same publication posture as every release before it.

## Upgrade

Deploy the exact `proxbox-api 0.0.20` Gitea package, verify `/health` and
`/version`, then deploy
`netbox-proxbox 0.0.24` and run the cross-stack sync smoke tests before public
promotion.
