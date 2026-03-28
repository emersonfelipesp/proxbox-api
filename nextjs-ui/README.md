# Proxbox Next.js UI

Standalone Next.js frontend for CRUD management of:

- one NetBox endpoint (`/netbox/endpoint`)
- many Proxmox endpoints (`/proxmox/endpoints`)

## Prerequisites

- Proxbox API running locally (default `http://127.0.0.1:8800`)
- Node.js 20+

## Setup

1. Copy env file:

```bash
cp .env.example .env.local
```

2. Adjust API URL if needed:

```bash
NEXT_PUBLIC_PROXBOX_API_URL=http://127.0.0.1:8800
```

3. Start development server:

```bash
npm run dev
```

4. Open `http://localhost:3000`

## Build

```bash
npm run build
npm run start
```
