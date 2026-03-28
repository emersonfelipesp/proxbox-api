"use client"

import { useCallback, useEffect, useMemo, useState } from "react"

import {
  createNetBoxEndpoint,
  createProxmoxEndpoint,
  deleteNetBoxEndpoint,
  deleteProxmoxEndpoint,
  getNetBoxEndpoint,
  listProxmoxEndpoints,
  updateNetBoxEndpoint,
  updateProxmoxEndpoint,
} from "@/lib/api"
import { NetBoxEndpointForm, ProxmoxEndpointForm } from "@/components/endpoint-form"
import type { NetBoxEndpoint, ProxmoxEndpoint, ProxmoxEndpointPayload } from "@/lib/types"

type ToastKind = "success" | "error" | "info"

interface ToastMessage {
  kind: ToastKind
  message: string
}

export default function Home() {
  const [loading, setLoading] = useState(true)
  const [savingNetBox, setSavingNetBox] = useState(false)
  const [savingProxmox, setSavingProxmox] = useState(false)
  const [netboxEndpoint, setNetboxEndpoint] = useState<NetBoxEndpoint | null>(null)
  const [proxmoxEndpoints, setProxmoxEndpoints] = useState<ProxmoxEndpoint[]>([])
  const [editingProxmox, setEditingProxmox] = useState<ProxmoxEndpoint | null>(null)
  const [toast, setToast] = useState<ToastMessage | null>(null)

  const proxmoxCountLabel = useMemo(() => {
    const count = proxmoxEndpoints.length
    return `${count} endpoint${count === 1 ? "" : "s"}`
  }, [proxmoxEndpoints.length])

  function pushToast(kind: ToastKind, message: string) {
    setToast({ kind, message })
    window.setTimeout(() => {
      setToast((current) => (current?.message === message ? null : current))
    }, 3000)
  }

  const refresh = useCallback(async () => {
    try {
      setLoading(true)
      const [nb, px] = await Promise.all([getNetBoxEndpoint(), listProxmoxEndpoints()])
      setNetboxEndpoint(nb)
      setProxmoxEndpoints(px)
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to load endpoint data"
      pushToast("error", message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  async function handleNetBoxSubmit(payload: NetBoxEndpoint) {
    setSavingNetBox(true)
    try {
      if (netboxEndpoint?.id) {
        await updateNetBoxEndpoint(netboxEndpoint.id, payload)
        pushToast("success", "NetBox endpoint updated")
      } else {
        await createNetBoxEndpoint(payload)
        pushToast("success", "NetBox endpoint created")
      }
      await refresh()
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to save NetBox endpoint"
      pushToast("error", message)
    } finally {
      setSavingNetBox(false)
    }
  }

  async function handleDeleteNetBox() {
    if (!netboxEndpoint?.id) return
    if (!window.confirm("Delete the NetBox endpoint?")) return

    setSavingNetBox(true)
    try {
      await deleteNetBoxEndpoint(netboxEndpoint.id)
      pushToast("success", "NetBox endpoint deleted")
      await refresh()
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to delete NetBox endpoint"
      pushToast("error", message)
    } finally {
      setSavingNetBox(false)
    }
  }

  async function handleProxmoxCreate(payload: ProxmoxEndpointPayload) {
    setSavingProxmox(true)
    try {
      await createProxmoxEndpoint(payload)
      pushToast("success", `Proxmox endpoint ${payload.name} created`)
      await refresh()
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to create Proxmox endpoint"
      pushToast("error", message)
    } finally {
      setSavingProxmox(false)
    }
  }

  async function handleProxmoxUpdate(payload: ProxmoxEndpointPayload) {
    if (!editingProxmox?.id) return

    setSavingProxmox(true)
    try {
      await updateProxmoxEndpoint(editingProxmox.id, payload)
      setEditingProxmox(null)
      pushToast("success", `Proxmox endpoint ${payload.name} updated`)
      await refresh()
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to update Proxmox endpoint"
      pushToast("error", message)
    } finally {
      setSavingProxmox(false)
    }
  }

  async function handleProxmoxDelete(endpoint: ProxmoxEndpoint) {
    if (!endpoint.id) return
    if (!window.confirm(`Delete Proxmox endpoint ${endpoint.name}?`)) return

    setSavingProxmox(true)
    try {
      await deleteProxmoxEndpoint(endpoint.id)
      if (editingProxmox?.id === endpoint.id) {
        setEditingProxmox(null)
      }
      pushToast("success", `Proxmox endpoint ${endpoint.name} deleted`)
      await refresh()
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to delete Proxmox endpoint"
      pushToast("error", message)
    } finally {
      setSavingProxmox(false)
    }
  }

  return (
    <div className="min-h-screen bg-[var(--app-bg)] text-[var(--app-foreground)]">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-8 md:px-8">
        <header className="rounded-3xl border border-[var(--border)] bg-[var(--hero-bg)] px-6 py-6 shadow-[0_20px_70px_-45px_rgba(0,0,0,0.45)]">
          <p className="text-xs uppercase tracking-[0.24em] text-[var(--muted)]">Proxbox UI</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight md:text-4xl">Endpoint Control Plane</h1>
          <p className="mt-2 max-w-3xl text-sm text-[var(--muted)] md:text-base">
            Manage one NetBox endpoint and multiple Proxmox endpoints from a single interface.
          </p>
          {toast ? (
            <p
              className={`mt-4 inline-flex rounded-lg px-3 py-2 text-sm font-medium ${
                toast.kind === "success"
                  ? "bg-[var(--success-bg)] text-[var(--success)]"
                  : toast.kind === "error"
                    ? "bg-[var(--danger-bg)] text-[var(--danger)]"
                    : "bg-[var(--info-bg)] text-[var(--info)]"
              }`}
            >
              {toast.message}
            </p>
          ) : null}
        </header>

        {loading ? (
          <div className="rounded-3xl border border-[var(--border)] bg-[var(--panel-bg)] p-8 text-sm text-[var(--muted)]">
            Loading endpoint data...
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1.2fr_1fr]">
            <section className="space-y-6">
              <div className="rounded-3xl border border-[var(--border)] bg-[var(--panel-bg)] p-5">
                <div className="mb-3 flex items-center justify-between">
                  <div>
                    <h2 className="text-xl font-semibold text-[var(--panel-foreground)]">NetBox Endpoint</h2>
                    <p className="text-sm text-[var(--muted)]">Unique record, used for all NetBox operations.</p>
                  </div>
                  <span className="rounded-full bg-[var(--chip-bg)] px-3 py-1 text-xs font-semibold text-[var(--chip-foreground)]">
                    {netboxEndpoint ? "Configured" : "Not configured"}
                  </span>
                </div>
                <NetBoxEndpointForm
                  mode={netboxEndpoint ? "edit" : "create"}
                  initial={netboxEndpoint}
                  submitting={savingNetBox}
                  onSubmit={handleNetBoxSubmit}
                />
                {netboxEndpoint?.id ? (
                  <button
                    type="button"
                    onClick={handleDeleteNetBox}
                    className="mt-3 rounded-xl border border-[var(--danger)]/40 px-4 py-2 text-sm font-semibold text-[var(--danger)]"
                  >
                    Delete NetBox endpoint
                  </button>
                ) : null}
              </div>

              <div className="rounded-3xl border border-[var(--border)] bg-[var(--panel-bg)] p-5">
                <div className="mb-3 flex items-center justify-between">
                  <div>
                    <h2 className="text-xl font-semibold text-[var(--panel-foreground)]">Proxmox Endpoints</h2>
                    <p className="text-sm text-[var(--muted)]">Multiple records supported for many clusters.</p>
                  </div>
                  <span className="rounded-full bg-[var(--chip-bg)] px-3 py-1 text-xs font-semibold text-[var(--chip-foreground)]">
                    {proxmoxCountLabel}
                  </span>
                </div>
                <ProxmoxEndpointForm
                  mode={editingProxmox ? "edit" : "create"}
                  initial={editingProxmox}
                  submitting={savingProxmox}
                  onSubmit={editingProxmox ? handleProxmoxUpdate : handleProxmoxCreate}
                  onCancel={editingProxmox ? () => setEditingProxmox(null) : undefined}
                />
              </div>
            </section>

            <aside className="rounded-3xl border border-[var(--border)] bg-[var(--panel-bg)] p-5">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-xl font-semibold text-[var(--panel-foreground)]">Registered Proxmox</h2>
                <button
                  type="button"
                  onClick={() => void refresh()}
                  className="rounded-lg border border-[var(--border)] px-3 py-1 text-xs font-semibold text-[var(--panel-foreground)]"
                >
                  Refresh
                </button>
              </div>
              {proxmoxEndpoints.length === 0 ? (
                <p className="rounded-xl border border-dashed border-[var(--border)] bg-[var(--field-bg)] p-4 text-sm text-[var(--muted)]">
                  No Proxmox endpoints yet.
                </p>
              ) : (
                <div className="space-y-3">
                  {proxmoxEndpoints.map((endpoint) => (
                    <article key={endpoint.id} className="rounded-2xl border border-[var(--border)] bg-[var(--field-bg)] p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-semibold text-[var(--panel-foreground)]">{endpoint.name}</p>
                          <p className="text-xs text-[var(--muted)]">
                            {endpoint.domain || endpoint.ip_address}:{endpoint.port}
                          </p>
                          <p className="mt-1 text-xs text-[var(--muted)]">User: {endpoint.username}</p>
                        </div>
                        <span
                          className={`rounded-full px-2 py-1 text-[10px] font-semibold ${
                            endpoint.verify_ssl
                              ? "bg-[var(--success-bg)] text-[var(--success)]"
                              : "bg-[var(--danger-bg)] text-[var(--danger)]"
                          }`}
                        >
                          {endpoint.verify_ssl ? "SSL on" : "SSL off"}
                        </span>
                      </div>
                      <div className="mt-3 flex gap-2">
                        <button
                          type="button"
                          onClick={() => setEditingProxmox(endpoint)}
                          className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-semibold text-[var(--panel-foreground)]"
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleProxmoxDelete(endpoint)}
                          className="rounded-lg border border-[var(--danger)]/40 px-3 py-1.5 text-xs font-semibold text-[var(--danger)]"
                        >
                          Delete
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </aside>
          </div>
        )}
      </div>
    </div>
  )
}
