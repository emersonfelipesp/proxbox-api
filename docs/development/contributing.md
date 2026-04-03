# Contributing

## Development workflow

1. Create a feature branch from `main`.
2. Implement changes with minimal, focused scope.
3. Add or update tests for behavior changes.
4. Run verification commands locally.
5. Open pull request with clear summary.

## Coding conventions

- Keep route handlers focused on orchestration.
- Move reusable business logic into services/utilities.
- Use typed models for request/response contracts.
- Raise `ProxboxException` for expected domain failures.

## Documentation expectations

- Update `docs/` when endpoint behavior changes.
- Keep English docs as source-of-truth.
- Update `docs/pt-BR/` for translated key pages.
- Document generated-contract changes, helper/service integration that depends on those generated models, and any route changes that affect the in-repo `CLAUDE.md` guides.

## Pull request checklist

- [ ] Tests pass (`pytest`).
- [ ] Code compiles (`python -m compileall proxbox_api`).
- [ ] Docs build passes (`mkdocs build --strict`) when docs changed.
- [ ] API behavior changes are documented.
- [ ] Generated Proxmox artifacts are regenerated when codegen behavior changes.

## Security and secrets

- Never commit real API tokens, passwords, or certificates.
- Use placeholders in docs and examples.
