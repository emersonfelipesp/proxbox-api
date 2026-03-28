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

export interface ProxmoxEndpoint {
  id?: number
  name: string
  ip_address: string
  domain?: string | null
  port: number
  username: string
  password?: string | null
  verify_ssl: boolean
  token_name?: string | null
  token_value?: string | null
}

export type ProxmoxEndpointPayload = Omit<ProxmoxEndpoint, "id">
