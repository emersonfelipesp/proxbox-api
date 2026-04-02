# Agent Guide: Proxbox Next.js UI

## Context

This is a standalone Next.js 16+ frontend for managing NetBox and Proxmox endpoint configuration as part of the Proxbox API project.

**Parent project**: See `/root/nms/proxbox-api/CLAUDE.md` for the overall Proxbox architecture.

**This directory**: Self-contained Next.js 16.2.1 app with minimal dependencies.

## Critical Next.js Version Notice

<!-- BEGIN:nextjs-agent-rules -->
**This is NOT the Next.js you know.**

Next.js 16+ has breaking changes — APIs, conventions, and file structure may differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

## Project Structure

```
nextjs-ui/
├── AGENTS.md              ← Local index for the UI docs
├── CLAUDE.md              ← Local context (references parent AGENTS.md)
├── README.md              ← Setup and development instructions
├── app/
│   ├── page.tsx           ← Main dashboard page
│   ├── layout.tsx         ← Root layout
│   └── globals.css        ← Global styles and theme tokens
├── components/
│   └── endpoint-form.tsx  ← Reusable CRUD form component
└── lib/
    ├── api.ts             ← Backend request helpers
    └── types.ts           ← Shared frontend types
```

## Stack

- **Next.js**: 16.2.1 (App Router)
- **React**: 19.2.4
- **TypeScript**: 5.x
- **Tailwind CSS**: 4.x
- **API Backend**: Proxbox API at `http://127.0.0.1:8000`

## Key Features

This UI manages:
- **One NetBox endpoint** (`/netbox/endpoint`)
- **Many Proxmox endpoints** (`/proxmox/endpoints`)

The backend API is defined in `/root/nms/proxbox-api/proxbox_api/`.

## Development Workflow

1. **Start backend first**: Ensure Proxbox API is running at `http://127.0.0.1:8000`
2. **Install dependencies**: `npm install` (if needed)
3. **Start dev server**: `npm run dev`
4. **Access UI**: `http://localhost:3000`

## Important Conventions

- **API URL**: Configured via `NEXT_PUBLIC_PROXBOX_API_URL` in `.env.local`
- **App Router**: Use server components by default, client components only when needed
- **TypeScript**: Strict typing enabled
- **Styling**: Tailwind CSS 4.x utility classes

## Common Tasks

### Adding a new endpoint form
1. Review `components/endpoint-form.tsx` for the current controlled-form pattern
2. Follow the existing CRUD structure in `app/page.tsx`
3. Ensure TypeScript types match backend API schemas in `lib/types.ts`

### API integration
- Use `fetch()` with `NEXT_PUBLIC_PROXBOX_API_URL` from environment
- Handle errors gracefully with user-friendly messages
- Follow REST conventions matching the backend

### Styling
- Use Tailwind utility classes
- Maintain consistency with existing components
- Check `globals.css` for any custom styles

## Testing Changes

1. Run linter: `npm run lint`
2. Build check: `npm run build`
3. Manual testing: Verify CRUD operations work end-to-end with the backend

## References

- **Parent project context**: `/root/nms/proxbox-api/CLAUDE.md`
- **Backend API**: `/root/nms/proxbox-api/proxbox_api/`
- **Setup instructions**: `./README.md`
- **Next.js docs**: `node_modules/next/dist/docs/` (for version-specific guidance)
