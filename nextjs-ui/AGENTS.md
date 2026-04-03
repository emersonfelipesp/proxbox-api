# Agent Guide: Proxbox Next.js UI

## Context

This is the standalone Next.js 16.2.1 frontend for managing NetBox and Proxmox endpoint configuration as part of the `proxbox-api` project.

**Parent project**: see `/root/nms/proxbox-api/CLAUDE.md` for the backend architecture and repo-wide rules.

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

- Next.js 16.2.1 with the App Router
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

## Verification

Run these checks after editing this directory:

```bash
npm run lint
npm run build
```

## References

- Backend architecture: `/root/nms/proxbox-api/CLAUDE.md`
- Backend package: `/root/nms/proxbox-api/proxbox_api/`
- Setup instructions: `./README.md`
- Version-specific Next.js docs: `node_modules/next/dist/docs/`
