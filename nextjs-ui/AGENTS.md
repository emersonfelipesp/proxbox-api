# Agent Guide: Proxbox Next.js UI

## Workspace Context

This file lives at `<repository-root>/nextjs-ui/AGENTS.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Context

This is the standalone Next.js 16.3.4 frontend for managing NetBox and Proxmox endpoint configuration as part of the `proxbox-api` project.

**Parent project**: see `<repository-root>/CLAUDE.md` for the backend architecture and repo-wide rules.

## Critical Next.js Notice

<!-- BEGIN:nextjs-agent-rules -->
Next.js 16+ differs from older App Router examples. Before changing code, check the versioned guidance under `node_modules/next/dist/docs/` and follow the repo's current scripts and conventions.
<!-- END:nextjs-agent-rules -->

## Project Structure

```
nextjs-ui/
├── AGENTS.md
├── CLAUDE.md
├── README.md
├── app/
│   ├── page.tsx
│   ├── layout.tsx
│   └── globals.css
├── components/
│   └── endpoint-form.tsx
└── lib/
    ├── api.ts
    └── types.ts
```

## Stack

- Next.js 16.3.4 with the App Router
- React 19.2.4
- TypeScript 5.x
- Tailwind CSS 4.x

## What This UI Owns

- One NetBox endpoint at `/netbox/endpoint`
- Many Proxmox endpoints at `/proxmox/endpoints`
- Local theme state and endpoint CRUD orchestration in `app/page.tsx`

## Working Rules

1. Start the backend first at the configured API URL.
2. Keep fetch and response normalization in `lib/api.ts`.
3. Keep shared endpoint types in `lib/types.ts`.
4. Keep UI state and presentation logic in the React components.
5. Reuse the controlled-form pattern in `components/endpoint-form.tsx` before adding new patterns.

## Development Workflow

1. Install dependencies with `npm install` if needed.
2. Run the app with `npm run dev`.
3. Verify the UI at `http://localhost:3000`.

## Dependency Security

- Keep Next.js and `eslint-config-next` aligned on the same patched release. The current supported version is 16.3.4.
- Regenerate `package-lock.json` with npm after every dependency change. Do not use cross-major overrides to suppress audit findings.
- Run `npm ci`, `npm audit --audit-level=low`, `npm run lint`, and `npm run build` after changing the dependency graph.
- The repository regression suite verifies the minimum secure dependency resolutions in `tests/test_dependency_security.py`.

## Verification

Run these checks after editing this directory:

```bash
npm run lint
npm run build
```

## References

- Backend architecture: `<repository-root>/CLAUDE.md`
- Backend package: `<repository-root>/proxbox_api/`
- Setup instructions: `./README.md`
- Version-specific Next.js docs: `node_modules/next/dist/docs/`

## Merged Guidance (from former CLAUDE.md)

# nextjs-ui Directory Guide

## Purpose

Standalone Next.js frontend for managing one NetBox endpoint and multiple Proxmox endpoints through the `proxbox-api` backend.

## Current Files

- `app/page.tsx`: client dashboard with endpoint summaries, CRUD orchestration, theme handling, and toast feedback.
- `app/layout.tsx`: root layout and metadata.
- `app/globals.css`: global styling and theme tokens.
- `components/endpoint-form.tsx`: controlled forms for NetBox and Proxmox endpoint create and edit flows.
- `lib/api.ts`: fetch helpers that normalize backend responses and error payloads.
- `lib/types.ts`: shared endpoint and payload types.
- `README.md`: local setup and runtime instructions.
- `.env.example`: example environment variables for local configuration.
- `eslint.config.mjs`, `next.config.ts`, `postcss.config.mjs`: project tooling configuration.

## Runtime Notes

- The API base URL comes from `NEXT_PUBLIC_PROXBOX_API_URL` and defaults to `http://127.0.0.1:8000`.
- The theme mode is stored in `localStorage` and reflected on `document.documentElement.dataset.theme`.
- The page talks to the backend REST API for one NetBox endpoint and many Proxmox endpoints.
- Backend payload shape changes should be reflected in both `lib/types.ts` and `lib/api.ts`.

## Extension Guidance

- Keep data fetching in `lib/api.ts` and presentation logic in the React components.
- Keep the form component controlled and reuse the existing CRUD flow before adding new UI state.
- Update `lib/types.ts` when backend contracts change.
- Keep styling in `app/globals.css` aligned with the current design tokens and theme behavior.
