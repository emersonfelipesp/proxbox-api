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
  onSubmit: (payload: ProxmoxEndpointPayload | Partial<ProxmoxEndpointPayload>) => Promise<void>
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
  const [tokenVersion, setTokenVersion] = useState(initial?.token_version ?? "v1")
  const [tokenKey, setTokenKey] = useState(initial?.token_key ?? "")
  const [token, setToken] = useState(initial?.token ?? "")
  const [verifySsl, setVerifySsl] = useState(initial?.verify_ssl ?? true)
  const [error, setError] = useState<string | null>(null)

  const title = mode === "create" ? "Create NetBox endpoint" : "Update NetBox endpoint"
  const submitLabel = mode === "create" ? "Create endpoint" : "Save changes"

  const hasV1Token = Boolean(token.trim())
  const hasV2Pair = Boolean(tokenKey.trim() && token.trim())
  const hasPartialV2 = Boolean(tokenVersion === "v2" && (tokenKey.trim() || token.trim()) && !hasV2Pair)

  const canSubmit = useMemo(() => {
    const core = Boolean(name.trim() && ipAddress.trim() && domain.trim())
    if (!core) return false
    if (tokenVersion === "v1") return hasV1Token
    return hasV2Pair && !hasPartialV2
  }, [name, ipAddress, domain, tokenVersion, hasV1Token, hasV2Pair, hasPartialV2])

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)

    if (!canSubmit) {
      if (hasPartialV2) {
        setError("Provide both token key and token secret for API token v2.")
      } else {
        setError("Fill all required fields.")
      }
      return
    }

    await onSubmit({
      id: initial?.id,
      name: name.trim(),
      ip_address: ipAddress.trim(),
      domain: domain.trim(),
      port,
      token_version: tokenVersion,
      token_key: tokenVersion === "v2" ? tokenKey.trim() : null,
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
      <div className="space-y-2">
        <span className={labelClass}>NetBox API token version *</span>
        <div className="flex flex-wrap gap-4 text-sm text-[var(--panel-foreground)]">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="netbox-token-version"
              checked={tokenVersion === "v1"}
              onChange={() => setTokenVersion("v1")}
            />
            v1 (legacy single token)
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="netbox-token-version"
              checked={tokenVersion === "v2"}
              onChange={() => setTokenVersion("v2")}
            />
            v2 (key + secret)
          </label>
        </div>
      </div>
      {tokenVersion === "v1" ? (
        <label className="space-y-1 block">
          <span className={labelClass}>API token *</span>
          <input
            className={inputClass}
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="NetBox v1 API token"
          />
        </label>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <label className="space-y-1">
            <span className={labelClass}>Token key *</span>
            <input
              className={inputClass}
              value={tokenKey}
              onChange={(e) => setTokenKey(e.target.value)}
              placeholder="Public identifier (nbt_ prefix optional)"
            />
          </label>
          <label className="space-y-1">
            <span className={labelClass}>Token secret *</span>
            <input
              className={inputClass}
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Secret portion"
            />
          </label>
        </div>
      )}
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
  const hasRequiredCoreFields = Boolean(name.trim() && ipAddress.trim() && username.trim())
  const canSubmit =
    mode === "create"
      ? Boolean(hasRequiredCoreFields && (hasPassword || hasTokenPair) && !hasPartialToken)
      : Boolean(hasRequiredCoreFields && !hasPartialToken)

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)

    if (!canSubmit) {
      if (hasPartialToken) {
        setError("Provide token name and token value together.")
      } else if (mode === "create") {
        setError("Fill required fields and provide password or token credentials.")
      } else {
        setError("Fill required fields.")
      }
      return
    }

    const payload: Partial<ProxmoxEndpointPayload> = {
      name: name.trim(),
      ip_address: ipAddress.trim(),
      domain: domain.trim() || null,
      port,
      username: username.trim(),
      verify_ssl: verifySsl,
    }

    if (mode === "create") {
      payload.password = password.trim() || null
      payload.token_name = tokenName.trim() || null
      payload.token_value = tokenValue.trim() || null
    } else {
      if (hasPassword) {
        payload.password = password.trim()
      }
      if (hasTokenPair) {
        payload.token_name = tokenName.trim()
        payload.token_value = tokenValue.trim()
      }
    }

    await onSubmit(payload)
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
