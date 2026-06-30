export type NetBoxTokenVersion = "v1" | "v2"

export interface NetBoxEndpoint {
  id?: number
  name: string
  ip_address: string
  domain: string
  port: number
  token_version: NetBoxTokenVersion
  token_key?: string | null
  token: string
  verify_ssl: boolean
}

export type ProxmoxAccessMethod = "api" | "api_ssh"

export interface ProxmoxEndpoint {
  id?: number
  name: string
  ip_address: string
  domain?: string | null
  port: number
  username: string
  password?: string | null
  verify_ssl: boolean
  // Transport access method: "api" (Read+Write over API only, default) or
  // "api_ssh" (Read+Write over API + SSH). SSH only complements API; there is
  // no SSH-only option.
  access_methods?: ProxmoxAccessMethod
  token_name?: string | null
  token_value?: string | null
}

export type ProxmoxEndpointPayload = Omit<ProxmoxEndpoint, "id">
