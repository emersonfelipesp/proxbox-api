# Unreleased

## Security and compatibility

- Default to process-pinned `rpc_only` execution policy, denying unrestricted
  SSH tickets, terminal consumption, console capabilities, and actorless
  synchronization WebSockets before effectful dependencies run.
- Retain explicit `legacy` compatibility with authentication before provider
  acquisition, owned interactive resources, cancellation-safe cleanup, and
  fail-closed checks after blocked operations.
- Add an authenticated local execution-policy status endpoint and a shared
  deployment generation. Local boundary readiness does not certify fleet
  cutover or complete RPC coverage; compatible peer and caller enforcement
  remains a deployment prerequisite.
