// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  headers: {
    'Content-Type': 'application/json',
  },
})

// Token storage key
const TOKEN_KEY = 'octoprox_token'

// Auth state management
export const auth = {
  getToken: (): string | null => {
    return localStorage.getItem(TOKEN_KEY)
  },

  setToken: (token: string): void => {
    localStorage.setItem(TOKEN_KEY, token)
  },

  clearToken: (): void => {
    localStorage.removeItem(TOKEN_KEY)
  },

  isAuthenticated: (): boolean => {
    return !!localStorage.getItem(TOKEN_KEY)
  },
}

// Add auth token to requests
api.interceptors.request.use((config) => {
  const token = auth.getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Handle API errors
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      auth.clearToken()
      // Dispatch custom event for auth state change
      window.dispatchEvent(new CustomEvent('auth:logout'))
    }
    // Extract error message from API response for better error display
    const apiDetail = error.response?.data?.detail
    if (apiDetail) {
      // Handle Pydantic validation errors (422) which return an array of error objects
      if (Array.isArray(apiDetail)) {
        const messages = apiDetail.map((err: { msg?: string; loc?: string[] }) => {
          const field = err.loc?.slice(-1)[0] || 'field'
          return err.msg || `Invalid ${field}`
        })
        error.message = messages.join('; ')
      } else if (typeof apiDetail === 'string') {
        error.message = apiDetail
      } else {
        error.message = JSON.stringify(apiDetail)
      }
    }
    return Promise.reject(error)
  }
)

// Auth API types
export interface AuthStatus {
  enabled: boolean
  authenticated: boolean
  username: string | null
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
}

// Auth API functions
export const checkAuthStatus = async (): Promise<AuthStatus> => {
  const response = await api.get('/auth/status')
  return response.data
}

export const login = async (username: string, password: string): Promise<LoginResponse> => {
  const response = await api.post('/auth/login', { username, password })
  const data = response.data as LoginResponse
  auth.setToken(data.access_token)
  return data
}

export const logout = (): void => {
  auth.clearToken()
  window.dispatchEvent(new CustomEvent('auth:logout'))
}

// Project types
export interface Project {
  id: string
  name: string
  description: string
  username: string
  password: string
  routing_strategy: string
  health_check_interval: number
  health_check_timeout: number
  connection_timeout: number
  max_retries: number
  tls_mitm_mode: string
  tls_mitm_engine: string | null
  tls_mitm_browser: string | null
  created_at: string
  updated_at: string
}

export interface ProjectSummary extends Project {
  credential_count: number
  connector_count: number
  proxy_count: number
  healthy_proxy_count: number
}

export interface ProjectListResponse {
  total: number
  projects: ProjectSummary[]
}

export interface ProjectCreate {
  name: string
  description?: string
  username: string
  password: string
  routing_strategy?: string
  health_check_interval?: number
  health_check_timeout?: number
  connection_timeout?: number
  max_retries?: number
  tls_mitm_mode?: string
  tls_mitm_engine?: string | null
  tls_mitm_browser?: string | null
}

export interface ProjectUpdate {
  name?: string
  description?: string
  username?: string
  password?: string
  routing_strategy?: string
  health_check_interval?: number
  health_check_timeout?: number
  connection_timeout?: number
  max_retries?: number
  tls_mitm_mode?: string
  tls_mitm_engine?: string | null
  tls_mitm_browser?: string | null
}

export interface Proxy {
  id: string
  host: string
  port: number
  protocol: string
  username: string | null
  password: string | null
  display_host: string  // The host to display in UI (may differ from host for Oxylabs)
  connector_id: string
  connector_name: string | null
  connector_enabled: boolean
  status: string
  request_count: number
  success_count: number
  failure_count: number
  success_rate: number
  avg_latency_ms: number
  bytes_sent: number
  bytes_received: number
  tags: string[]
  created_at: string
}

export interface ProxyListResponse {
  total: number
  healthy: number
  proxies: Proxy[]
}

export interface ProxyCreate {
  host: string
  port: number
  connector_id: string
  protocol?: string
  username?: string
  password?: string
  tags?: string[]
}

export interface ProxyUpdate {
  host?: string
  port?: number
  protocol?: string
  username?: string
  password?: string
  tags?: string[]
}

export interface ProxyUploadError {
  line_number: number
  line: string
  error: string
}

export interface ProxyUploadResponse {
  total_lines: number
  successful: number
  failed: number
  proxies: Proxy[]
  errors: ProxyUploadError[]
}

// Credential types
export type CredentialType = 'static_proxy_provider' | 'aws' | 'gcp' | 'azure' | 'oxylabs' | 'brightdata'

// Oxylabs proxy types
export type OxylabsProxyType = 'residential' | 'mobile' | 'isp' | 'dedicated_isp' | 'datacenter' | 'datacenter_dedicated'

export interface OxylabsCredentialConfig {
  proxy_type: OxylabsProxyType
  username: string
  password: string
}

export interface OxylabsConnectorConfig {
  num_proxies: number
  country_code?: string | null
  session_duration_minutes: number
}

// BrightData proxy types
export type BrightDataProxyType = 'residential' | 'mobile' | 'isp' | 'datacenter'

export interface BrightDataCredentialConfig {
  token: string
  customer_id: string
}

export interface BrightDataConnectorConfig {
  zone_name: string
  zone_password: string
  proxy_type: BrightDataProxyType
  num_proxies: number
  country_code?: string | null
  healthcheck_url?: string | null  // Custom healthcheck URL (default: https://httpbin.org/ip)
}

export interface BrightDataZone {
  name: string
  type: string
  proxy_type: BrightDataProxyType
  password: string
  country_counts: Record<string, number> | null
  total_ips: number | null
}

export interface Credential {
  id: string
  name: string
  type: CredentialType
  project_id: string
  has_username: boolean
  has_password: boolean
  created_at: string
  updated_at: string
}

export interface CredentialDetail extends Credential {
  config: Record<string, unknown>
}

export interface CredentialListResponse {
  total: number
  credentials: Credential[]
}

export interface CredentialCreate {
  name: string
  type: CredentialType
  config: Record<string, unknown>
}

export interface CredentialUpdate {
  name?: string
  config?: Record<string, unknown>
}

// Connector types
export interface RoutingConfig {
  domain_whitelist?: string[]
  domain_blacklist?: string[]
}

export interface Connector {
  id: string
  name: string
  credential_id: string
  credential_name: string | null
  credential_type: CredentialType | null
  project_id: string
  config: Record<string, unknown>
  routing_config: RoutingConfig
  enabled: boolean
  proxy_count: number
  // Cloud provider error tracking
  last_error: string | null
  last_error_at: string | null
  consecutive_errors: number
  created_at: string
  updated_at: string
}

export interface ConnectorListResponse {
  total: number
  connectors: Connector[]
}

export interface ConnectorCreate {
  name: string
  credential_id: string
  config?: Record<string, unknown>
  routing_config?: RoutingConfig
  enabled?: boolean
}

export interface ConnectorUpdate {
  name?: string
  credential_id?: string
  config?: Record<string, unknown>
  routing_config?: RoutingConfig
  enabled?: boolean
}

// Rich option types for dropdowns
export interface RegionOption {
  code: string
  name: string
}

export interface InstanceTypeOption {
  code: string
  vcpus: number
  memory_gb: number
  architecture: string  // "x86_64" or "arm64"
  description: string
}

export interface CountryOption {
  code: string
  name: string
}

export interface ConnectorOptions {
  aws_regions: RegionOption[]
  aws_instance_types: InstanceTypeOption[]
  gcp_zones: RegionOption[]
  gcp_machine_types: InstanceTypeOption[]
  azure_locations: RegionOption[]
  azure_vm_sizes: InstanceTypeOption[]
  oxylabs_countries: CountryOption[]
}

export interface PoolMetrics {
  total_proxies: number
  healthy_proxies: number
  unhealthy_proxies: number
  draining_proxies: number
  terminating_proxies: number
  total_requests: number
  total_successes: number
  total_failures: number
  overall_success_rate: number
  avg_latency_ms: number
  total_bytes_sent: number
  total_bytes_received: number
}

export interface ScalingMetrics {
  demand_level: 'LOW' | 'MEDIUM' | 'HIGH'
  requests_per_minute: number
  rate_per_proxy: number
  current_instances: number
  healthy_instances: number
  min_instances: number
  max_instances: number
  draining_instances: number
  terminating_instances: number
}

export interface MetricsResponse {
  pool: PoolMetrics
  strategy: {
    current_strategy: string
    available_strategies: string[]
  }
}

export const fetchMetrics = async (): Promise<MetricsResponse> => {
  const response = await api.get('/metrics')
  return response.data
}

export const setStrategy = async (strategy: string) => {
  const response = await api.post('/proxies/strategy', { strategy })
  return response.data
}

export const downloadCaCertificate = async (): Promise<void> => {
  const response = await api.get('/projects/ca-certificate', { responseType: 'blob' })
  const url = URL.createObjectURL(response.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'octoprox-ca.crt'
  a.click()
  URL.revokeObjectURL(url)
}

// Project API functions
export const fetchProjects = async (): Promise<ProjectListResponse> => {
  const response = await api.get('/projects')
  return response.data
}

export const fetchProject = async (id: string): Promise<ProjectSummary> => {
  const response = await api.get(`/projects/${id}`)
  return response.data
}

export const createProject = async (data: ProjectCreate): Promise<ProjectSummary> => {
  const response = await api.post('/projects', data)
  return response.data
}

export const updateProject = async (id: string, data: ProjectUpdate): Promise<ProjectSummary> => {
  const response = await api.patch(`/projects/${id}`, data)
  return response.data
}

export const deleteProject = async (id: string, confirmation: string): Promise<void> => {
  await api.delete(`/projects/${id}`, { data: { confirmation } })
}

// Project-scoped Proxy API functions
export const fetchProjectProxies = async (projectId: string): Promise<ProxyListResponse> => {
  const response = await api.get(`/projects/${projectId}/proxies`)
  return response.data
}

export const createProjectProxy = async (projectId: string, data: ProxyCreate): Promise<Proxy> => {
  const response = await api.post(`/projects/${projectId}/proxies`, data)
  return response.data
}

export const updateProjectProxy = async (projectId: string, proxyId: string, data: ProxyUpdate): Promise<Proxy> => {
  const response = await api.patch(`/projects/${projectId}/proxies/${proxyId}`, data)
  return response.data
}

export const deleteProjectProxy = async (projectId: string, proxyId: string): Promise<void> => {
  await api.delete(`/projects/${projectId}/proxies/${proxyId}`)
}

export const uploadProjectProxies = async (projectId: string, file: File, connectorId: string): Promise<ProxyUploadResponse> => {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('connector_id', connectorId)
  const response = await api.post(`/projects/${projectId}/proxies/upload`, formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  })
  return response.data
}

export const fetchProjectMetrics = async (projectId: string): Promise<MetricsResponse> => {
  const response = await api.get(`/projects/${projectId}/metrics`)
  return response.data
}

export const fetchProjectScalingMetrics = async (projectId: string): Promise<ScalingMetrics> => {
  const response = await api.get(`/projects/${projectId}/metrics/scaling`)
  return response.data
}

export interface MetricsSnapshot {
  timestamp: string
  request_count: number
  success_count: number
  failure_count: number
  avg_latency_ms: number
  bytes_sent: number
  bytes_received: number
}

export interface MetricsHistoryResponse {
  snapshots: MetricsSnapshot[]
}

export const fetchProjectMetricsHistory = async (projectId: string, range: string): Promise<MetricsHistoryResponse> => {
  const response = await api.get(`/projects/${projectId}/metrics/history`, { params: { range } })
  return response.data
}

// Project-scoped Credential API functions
export const fetchProjectCredentials = async (projectId: string): Promise<CredentialListResponse> => {
  const response = await api.get(`/projects/${projectId}/credentials`)
  return response.data
}

export const fetchProjectCredential = async (projectId: string, credentialId: string): Promise<CredentialDetail> => {
  const response = await api.get(`/projects/${projectId}/credentials/${credentialId}`)
  return response.data
}

export const createProjectCredential = async (projectId: string, data: CredentialCreate): Promise<CredentialDetail> => {
  const response = await api.post(`/projects/${projectId}/credentials`, data)
  return response.data
}

export const updateProjectCredential = async (projectId: string, credentialId: string, data: CredentialUpdate): Promise<CredentialDetail> => {
  const response = await api.patch(`/projects/${projectId}/credentials/${credentialId}`, data)
  return response.data
}

export const deleteProjectCredential = async (projectId: string, credentialId: string): Promise<void> => {
  await api.delete(`/projects/${projectId}/credentials/${credentialId}`)
}

// Project-scoped Connector API functions
export const fetchProjectConnectors = async (projectId: string): Promise<ConnectorListResponse> => {
  const response = await api.get(`/projects/${projectId}/connectors`)
  return response.data
}

export const fetchProjectConnector = async (projectId: string, connectorId: string): Promise<Connector> => {
  const response = await api.get(`/projects/${projectId}/connectors/${connectorId}`)
  return response.data
}

export const createProjectConnector = async (projectId: string, data: ConnectorCreate): Promise<Connector> => {
  const response = await api.post(`/projects/${projectId}/connectors`, data)
  return response.data
}

export const updateProjectConnector = async (projectId: string, connectorId: string, data: ConnectorUpdate): Promise<Connector> => {
  const response = await api.patch(`/projects/${projectId}/connectors/${connectorId}`, data)
  return response.data
}

export const deleteProjectConnector = async (projectId: string, connectorId: string): Promise<void> => {
  await api.delete(`/projects/${projectId}/connectors/${connectorId}`)
}

// Connector options (regions, instance types, etc.)
export const fetchConnectorOptions = async (): Promise<ConnectorOptions> => {
  const response = await api.get('/connector-options')
  return response.data
}

// BrightData API functions
export const fetchBrightDataZones = async (credentialId: string): Promise<BrightDataZone[]> => {
  const response = await api.get(`/brightdata/zones/${credentialId}`)
  return response.data
}

// MITM Inspector types
export interface MitmRequestRecord {
  id: string
  timestamp: string
  method: string
  url: string
  request_headers: Record<string, string>
  upstream_headers: Record<string, string>
  request_body_size: number
  request_content_type: string
  status_code: number
  response_headers: Record<string, string>
  response_body_size: number
  response_content_type: string
  target_host: string
  proxy_url: string
  mitm_mode: string
  mitm_engine: string
  mitm_browser: string
  latency_ms: number
}

export interface MitmRequestsResponse {
  records: MitmRequestRecord[]
  next_cursor: string | null
}

export const fetchMitmRequests = async (
  projectId: string,
  count: number = 50,
  cursor?: string | null,
): Promise<MitmRequestsResponse> => {
  const params: Record<string, string | number> = { count }
  if (cursor) params.cursor = cursor
  const response = await api.get(`/projects/${projectId}/mitm/requests`, { params })
  return response.data
}

export const clearMitmRequests = async (projectId: string): Promise<void> => {
  await api.delete(`/projects/${projectId}/mitm/requests`)
}

export default api

