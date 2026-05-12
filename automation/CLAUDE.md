# automation/ Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/automation/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Placeholder package for future automation workflows in `proxbox-api`. Currently contains a minimal `main.py` entry point.

## Files

| File | Role |
|------|------|
| `__init__.py` | Package marker |
| `main.py` | Placeholder entry point for automation tasks |

## Status

This package is not yet used by the application runtime or CI. It is reserved for future automation workflows such as scheduled syncs, event-driven triggers, or external system integrations. When adding new automation:

- Keep automation logic separate from the `proxbox_api` FastAPI application.
- Wire new automation tasks through `main.py` as a CLI entry point.
- Add corresponding tests in `tests/` when introducing real behavior.
