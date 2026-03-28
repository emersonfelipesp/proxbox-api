"use client"

import { useMemo, useState } from "react"

import type { NetBoxEndpoint, ProxmoxEndpoint, ProxmoxEndpointPayload } from "@/lib/types"

type FormMode = "create" | "edit"

type NetBoxFormProps = {
  mode: FormMode
  initial?: NetBoxEndpoint | null
  submitting?: boolean
  onSubmit: (payload: NetBoxEndpoint) => Promise<void>
  onCancel?: () => void
}

type ProxmoxFormProps = {
  mode: FormMode
  initial?: ProxmoxEndpoint | null
  submitting?: boolean
  onSubmit: (payload: ProxmoxEndpointPayload) => Promise<void>
  onCancel?: () => void
}

const labelClass = "text-sm font-semibold text-[var(--panel-foreground)]"
const inputClass =
  "w-full rounded-xl border border-[var(--border)] bg-[var(--field-bg)] px-3 py-2 text-sm outline-none transition focus:border-[var(--accent)]"

export function NetBoxEndpointForm({ mode, initial, submitting = false, onSubmit, onCancel }: NetBoxFormProps) {
  const [name, setName] = useState(initial?.name ?? "")
  const [ipAddress, setIpAddress] = useState(initial?.ip_address ?? "")
  const [domain, setDomain] = useState(initial?.domain ?? "")
  const [port, setPort] = useState(initial?.port ?? 443)
  const [token, setToken] = useState(initial?.token ?? "")
  const [verifySsl, setVerifySsl] = useState(initial?.verify_ssl ?? true)
  const [error, setError] = useState<string | null>(null)

  const title = mode === "create" ? "Create NetBox endpoint" : "Update NetBox endpoint"
  const submitLabel = mode === "create" ? "Create endpoint" : "Save changes"

  const canSubmit = useMemo(() => {
    return Boolean(name.trim() && ipAddress.trim() && domain.trim() && token.trim())
  }, [name, ipAddress, domain, token])

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)

    if (!canSubmit) {
      setError("Fill all required fields.")
      return
    }

    await onSubmit({
      id: initial?.id,
      name: name.trim(),
      ip_address: ipAddress.trim(),
      domain: domain.trim(),
      port,
      token: token.trim(),
      verify_ssl: verifySsl,
    })
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      <h3 className="text-base font-semibold text-[var(--panel-foreground)]">{title}</h3>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label className="space-y-1">
          <span className={labelClass}>Name *</span>
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} placeholder="netbox-primary" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>IP Address *</span>
          <input className={inputClass} value={ipAddress} onChange={(e) => setIpAddress(e.target.value)} placeholder="10.0.30.235" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>Domain *</span>
          <input className={inputClass} value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="netbox.example.com" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>Port</span>
          <input className={inputClass} type="number" min={1} max={65535} value={port} onChange={(e) => setPort(Number(e.target.value) || 443)} />
        </label>
      </div>
      <label className="space-y-1 block">
        <span className={labelClass}>Token *</span>
        <input className={inputClass} type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="NetBox API token" />
      </label>
      <label className="flex items-center gap-2 text-sm text-[var(--panel-foreground)]">
        <input type="checkbox" checked={verifySsl} onChange={(e) => setVerifySsl(e.target.checked)} />
        Verify SSL certificate
      </label>
      {error ? <p className="text-sm text-[var(--danger)]">{error}</p> : null}
      <div className="flex flex-wrap gap-2">
        <button
          disabled={submitting || !canSubmit}
          className="rounded-xl bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "Saving..." : submitLabel}
        </button>
        {onCancel ? (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-xl border border-[var(--border)] px-4 py-2 text-sm font-semibold text-[var(--panel-foreground)]"
          >
            Cancel
          </button>
        ) : null}
      </div>
    </form>
  )
}

export function ProxmoxEndpointForm({ mode, initial, submitting = false, onSubmit, onCancel }: ProxmoxFormProps) {
  const [name, setName] = useState(initial?.name ?? "")
  const [ipAddress, setIpAddress] = useState(initial?.ip_address ?? "")
  const [domain, setDomain] = useState(initial?.domain ?? "")
  const [port, setPort] = useState(initial?.port ?? 8006)
  const [username, setUsername] = useState(initial?.username ?? "")
  const [password, setPassword] = useState(initial?.password ?? "")
  const [verifySsl, setVerifySsl] = useState(initial?.verify_ssl ?? true)
  const [tokenName, setTokenName] = useState(initial?.token_name ?? "")
  const [tokenValue, setTokenValue] = useState(initial?.token_value ?? "")
  const [error, setError] = useState<string | null>(null)

  const title = mode === "create" ? "Add Proxmox endpoint" : "Edit Proxmox endpoint"
  const submitLabel = mode === "create" ? "Add endpoint" : "Save changes"

  const hasPassword = Boolean(password.trim())
  const hasTokenPair = Boolean(tokenName.trim() && tokenValue.trim())
  const hasPartialToken = Boolean(tokenName.trim() || tokenValue.trim()) && !hasTokenPair
  const canSubmit = Boolean(name.trim() && ipAddress.trim() && username.trim() && (hasPassword || hasTokenPair) && !hasPartialToken)

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)

    if (!canSubmit) {
      if (hasPartialToken) {
        setError("Provide token name and token value together.")
      } else {
        setError("Fill required fields and provide password or token credentials.")
      }
      return
    }

    await onSubmit({
      name: name.trim(),
      ip_address: ipAddress.trim(),
      domain: domain.trim() || null,
      port,
      username: username.trim(),
      password: password.trim() || null,
      verify_ssl: verifySsl,
      token_name: tokenName.trim() || null,
      token_value: tokenValue.trim() || null,
    })
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      <h3 className="text-base font-semibold text-[var(--panel-foreground)]">{title}</h3>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label className="space-y-1">
          <span className={labelClass}>Name *</span>
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} placeholder="cluster-sp" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>IP Address *</span>
          <input className={inputClass} value={ipAddress} onChange={(e) => setIpAddress(e.target.value)} placeholder="10.0.30.240" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>Domain</span>
          <input className={inputClass} value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="proxmox.example.com" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>Port</span>
          <input className={inputClass} type="number" min={1} max={65535} value={port} onChange={(e) => setPort(Number(e.target.value) || 8006)} />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>Username *</span>
          <input className={inputClass} value={username} onChange={(e) => setUsername(e.target.value)} placeholder="root@pam" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>Password</span>
          <input className={inputClass} type="password" value={password ?? ""} onChange={(e) => setPassword(e.target.value)} placeholder="Optional when using token" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>Token Name</span>
          <input className={inputClass} value={tokenName ?? ""} onChange={(e) => setTokenName(e.target.value)} placeholder="api-token" />
        </label>
        <label className="space-y-1">
          <span className={labelClass}>Token Value</span>
          <input className={inputClass} type="password" value={tokenValue ?? ""} onChange={(e) => setTokenValue(e.target.value)} placeholder="xxxxxxxx-xxxx-xxxx" />
        </label>
      </div>
      <label className="flex items-center gap-2 text-sm text-[var(--panel-foreground)]">
        <input type="checkbox" checked={verifySsl} onChange={(e) => setVerifySsl(e.target.checked)} />
        Verify SSL certificate
      </label>
      {error ? <p className="text-sm text-[var(--danger)]">{error}</p> : null}
      <div className="flex flex-wrap gap-2">
        <button
          disabled={submitting || !canSubmit}
          className="rounded-xl bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "Saving..." : submitLabel}
        </button>
        {onCancel ? (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-xl border border-[var(--border)] px-4 py-2 text-sm font-semibold text-[var(--panel-foreground)]"
          >
            Cancel
          </button>
        ) : null}
      </div>
    </form>
  )
}
