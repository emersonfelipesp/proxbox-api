# Version 0.0.20

proxbox-api `0.0.20` pairs with `netbox-proxbox 0.0.24`,
`proxmox-sdk 0.0.13`, and `netbox-sdk 0.0.10`. The package supports Python
3.12 and 3.13 and certifies its NetBox integration through NetBox 4.6.6.

## Compatibility and reliability

- Adds NetBox 4.6.6 to the generated custom-field object-type and E2E
  compatibility matrices.
- Keeps the package resolver bounded to supported Python 3.12 and 3.13
  runtimes while retaining the released `netbox-sdk 0.0.10` client.
- Makes deterministic Proxmox tag styling work in FIPS environments by marking
  the non-security MD5 use explicitly.
- Generates the large E2E matrix through a tested Python helper, with pull
  requests following the same bounded untrusted matrix as ordinary pushes and
  release-only expansion reserved for published candidates.

## Release integrity

- Builds one wheel and one sdist without credentials and binds them to a
  canonical signed six-file request containing the package wheel, package
  sdist, `release-manifest.json`, `release-request.json`,
  `runner-completion-attestation.json`, and
  `runner-completion-attestation.sig`.
- Uses checksum-pinned uv in fresh per-run tool and managed-Python roots. The
  target repository cannot publish; a separately administered control plane
  verifies the policy-pinned workflow, exact first-attempt run, request,
  manifest, and artifact bytes before its isolated publisher invokes fixed,
  digest-locked publication tooling.
- Embeds a release-only full-digest Docker context with a hash-locked wheelhouse
  and canonical schema-2 inventory, allowing both the control plane and package
  deploy gateway to reject mutable or networked build inputs before publish.
- Requires authenticated Gitea CI run/job evidence for the exact canonical
  `develop` commit before a tag is accepted.
- Reuses the exact Gitea artifacts for TestPyPI, PyPI, and downstream Docker
  publication. Consumed versions are never overwritten or skipped.
- Requires exact-package production deployment and NMS-issued evidence before
  final tag promotion and GitHub Release publication.

## Upgrade

Deploy the exact `proxbox-api 0.0.20` Gitea package, verify `/health` and
`/version`, then deploy
`netbox-proxbox 0.0.24` and run the cross-stack sync smoke tests before public
promotion.
