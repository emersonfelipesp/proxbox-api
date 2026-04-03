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

## Verification

Run these after editing this directory:

```bash
npm run lint
npm run build
```
