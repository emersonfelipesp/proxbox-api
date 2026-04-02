# nextjs-ui Directory Guide

## Purpose

Standalone Next.js frontend for managing one NetBox endpoint and many Proxmox endpoints through the proxbox-api backend.

## Current Files

- `app/page.tsx`: Client dashboard with theme toggle, endpoint summaries, toast feedback, and CRUD orchestration.
- `app/layout.tsx`: Root layout and app metadata.
- `app/globals.css`: Global styling and theme tokens.
- `components/endpoint-form.tsx`: Controlled forms for NetBox and Proxmox endpoint create/edit flows.
- `lib/api.ts`: Fetch helpers that normalize API responses and format backend error payloads.
- `lib/types.ts`: Shared endpoint and payload types.
- `README.md`: Local development instructions.

## Runtime Notes

- The API base URL comes from `NEXT_PUBLIC_PROXBOX_API_URL`, defaulting to `http://127.0.0.1:8000`.
- The UI stores the theme mode in `localStorage` and syncs it to `document.documentElement.dataset.theme`.
- The page currently manages one NetBox endpoint and multiple Proxmox endpoints using the backend REST API.

## Extension Guidance

- Keep data-fetching logic in `lib/api.ts` and presentation logic in the React components.
- Update `lib/types.ts` when backend payloads change.
- Use `npm run lint` and `npm run build` after editing this directory.
