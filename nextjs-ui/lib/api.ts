import type { NetBoxEndpoint, NetBoxTokenVersion, ProxmoxEndpoint, ProxmoxEndpointPayload } from "@/lib/types"

const API_URL = process.env.NEXT_PUBLIC_PROXBOX_API_URL?.replace(/\/+$/, "") ?? "http://127.0.0.1:8000"

type ValidationErr = { loc?: unknown; msg?: string; type?: string }

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v)
}

function formatApiError(body: unknown, status: number): string {
  if (!isRecord(body)) {
    return `Request failed with HTTP ${status}`
  }

  const detail = body.detail
  if (typeof detail === "string" && detail.trim()) {
    return detail
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const lines = detail.map((item) => {
      if (!isRecord(item)) return String(item)
      const ve = item as ValidationErr
      const loc = Array.isArray(ve.loc) ? ve.loc.filter((x) => x !== "body").join(".") : ""
      const msg = typeof ve.msg === "string" ? ve.msg : JSON.stringify(item)
      return loc ? `${loc}: ${msg}` : msg
    })
    return lines.join("\n")
  }

  const message = body.message
  if (typeof message === "string" && message.trim()) {
    return message
  }

  return `Request failed with HTTP ${status}`
}

function normalizeNetBoxEndpoint(raw: NetBoxEndpoint): NetBoxEndpoint {
  const tv: NetBoxTokenVersion = raw.token_version === "v2" ? "v2" : "v1"
  return {
    ...raw,
    token_version: tv,
    token_key: raw.token_key ?? null,
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
    cache: "no-store",
  })

  if (!response.ok) {
    const body: unknown = await response.json().catch(() => ({}))
    throw new Error(formatApiError(body, response.status))
  }

  if (response.status === 204) {
    return undefined as T
  }

  return response.json() as Promise<T>
}

export async function getNetBoxEndpoint(): Promise<NetBoxEndpoint | null> {
  const endpoints = await request<NetBoxEndpoint[]>("/netbox/endpoint")
  const first = endpoints[0] ?? null
  return first ? normalizeNetBoxEndpoint(first) : null
}

export async function createNetBoxEndpoint(payload: NetBoxEndpoint): Promise<NetBoxEndpoint> {
  const created = await request<NetBoxEndpoint>("/netbox/endpoint", {
    method: "POST",
    body: JSON.stringify(payload),
  })
  return normalizeNetBoxEndpoint(created)
}

export async function updateNetBoxEndpoint(id: number, payload: NetBoxEndpoint): Promise<NetBoxEndpoint> {
  const updated = await request<NetBoxEndpoint>(`/netbox/endpoint/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  })
  return normalizeNetBoxEndpoint(updated)
}

export async function deleteNetBoxEndpoint(id: number): Promise<{ message: string }> {
  return request<{ message: string }>(`/netbox/endpoint/${id}`, {
    method: "DELETE",
  })
}

export async function listProxmoxEndpoints(): Promise<ProxmoxEndpoint[]> {
  return request<ProxmoxEndpoint[]>("/proxmox/endpoints")
}

export async function createProxmoxEndpoint(payload: ProxmoxEndpointPayload): Promise<ProxmoxEndpoint> {
  return request<ProxmoxEndpoint>("/proxmox/endpoints", {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export async function updateProxmoxEndpoint(
  id: number,
  payload: Partial<ProxmoxEndpointPayload>,
): Promise<ProxmoxEndpoint> {
  return request<ProxmoxEndpoint>(`/proxmox/endpoints/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  })
}

export async function deleteProxmoxEndpoint(id: number): Promise<{ message: string }> {
  return request<{ message: string }>(`/proxmox/endpoints/${id}`, {
    method: "DELETE",
  })
}
