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

// Request IDs: sent on every call and echoed back by the API in the same
// header, so a failure shown in the UI can be matched to the server logs.
export const REQUEST_ID_HEADER = 'X-Request-ID'

function newRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID().replace(/-/g, '')
  }
  // Insecure contexts (plain http on a non-localhost host) lack randomUUID.
  let id = ''
  while (id.length < 32) id += Math.random().toString(16).slice(2)
  return id.slice(0, 32)
}

// Add auth token and request ID to requests
api.interceptors.request.use((config) => {
  const token = auth.getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  if (!config.headers[REQUEST_ID_HEADER]) {
    config.headers[REQUEST_ID_HEADER] = newRequestId()
  }
  return config
})

/** Request ID of a failed call, for support and log lookup. */
export function requestIdOf(error: unknown): string | null {
  if (!axios.isAxiosError(error)) return null
  const fromResponse = error.response?.headers?.[REQUEST_ID_HEADER.toLowerCase()]
  if (typeof fromResponse === 'string' && fromResponse) return fromResponse
  const fromRequest = error.config?.headers?.[REQUEST_ID_HEADER]
  return typeof fromRequest === 'string' && fromRequest ? fromRequest : null
}

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
    // Server-side failures are the ones worth reporting: put the request ID
    // in the message every error surface already shows, so the person can
    // quote it and the server logs for that request can be found.
    const status = error.response?.status
    if (typeof status === 'number' && status >= 500) {
      const requestId = requestIdOf(error)
      if (requestId) error.message = `${error.message} (request ${requestId})`
    }
    return Promise.reject(error)
  }
)

// Auth API types
export type UserRole = 'admin' | 'editor' | 'viewer'

export interface AuthStatus {
  authenticated: boolean
  username: string | null
  role: UserRole | null
  user_id: string | null
  theme_preference: string | null
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
  metrics_retention_days: number
  /** IP attribution: what a contradicted vendor location does to this project's proxies. */
  location_policy: LocationPolicy
  /** IP attribution: verify a session's exit before its first request. */
  location_preflight: PreflightMode
  /** Source precedence override; null inherits the install default. */
  location_sources: GeoSourceKind[] | null
  /** Conflict rule override; null inherits the install default. */
  location_conflict_rule: ConflictRule | null
  created_at: string
  updated_at: string
}

export type LocationPolicy = 'off' | 'warn' | 'strict'
export type PreflightMode = 'off' | 'report' | 'retry' | 'reject'
export type ConflictRule = 'consensus' | 'first'

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
  metrics_retention_days?: number
  location_policy?: LocationPolicy
  location_preflight?: PreflightMode
  location_sources?: GeoSourceKind[] | null
  location_conflict_rule?: ConflictRule | null
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
  metrics_retention_days?: number
  location_policy?: LocationPolicy
  location_preflight?: PreflightMode
  /** An empty list clears the override. */
  location_sources?: GeoSourceKind[] | null
  /** An empty string clears the override. */
  location_conflict_rule?: ConflictRule | '' | null
}

export interface Proxy {
  id: string
  host: string
  port: number
  protocol: string
  username: string | null
  password: string | null
  display_host: string  // The host to display in UI (the discovered exit IP for port-mode provider proxies)
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
  quarantined: boolean
  quarantine_remaining_seconds: number
  country: string | null  // Exit country (ISO code) when discovered or provisioned per geo
  /** Which source produced `country`: database, vendor, endpoint or manual. */
  country_source: string | null
  /** What the vendor claimed for this exit, when it made a claim. */
  vendor_country: string | null
  /** Attribution contradicts the vendor's claim (see the project's location policy). */
  location_conflict: boolean
  /** Full record from the IP databases, when any covers the exit IP. */
  location: IpLocation | null
  tags: string[]
  created_at: string
}

// --- IP attribution -----------------------------------------------------------------

export interface IpLocation {
  country?: string | null
  region?: string | null
  city?: string | null
  postal_code?: string | null
  latitude?: number | null
  longitude?: number | null
  asn?: number | null
  organization?: string | null
  is_anonymous?: boolean | null
  is_hosting?: boolean | null
  is_vpn?: boolean | null
  is_public_proxy?: boolean | null
  is_tor?: boolean | null
  is_residential_proxy?: boolean | null
}

export type GeoSourceKind = 'database' | 'vendor' | 'endpoint'

export interface SourcePolicy {
  sources: GeoSourceKind[]
  conflict_rule: ConflictRule
}

/** Install-wide attribution settings (one typed row). Projects inherit the default_* judgement. */
export interface GeoSettingsDoc {
  default_sources: GeoSourceKind[]
  default_conflict_rule: ConflictRule
  echo_url: string
  echo_ip_path: string
  echo_country_path: string | null
  echo_timeout_seconds: number
  health_check_attribution: boolean
  preflight_session_ttl_seconds: number
  preflight_max_attempts: number
  observation_retention_days: number
  exit_ip_retention_days: number
}

export interface LoadedGeoDatabase {
  id: string
  name: string
  vendor: string
  kind: string
  format: string
  source: string
  priority: number
  path: string
  size_bytes: number
  database_type: string
  build_epoch: string | null
  record_count: number
}

export interface GeoSettings {
  settings: GeoSettingsDoc
  from_database: boolean
  echo_enabled: boolean
  echo_trusted_proxies: string[]
  databases_loaded: LoadedGeoDatabase[]
}

export interface GeoDatabase {
  id: string
  name: string
  vendor: string
  kind: string
  format: string
  source: 'upload' | 'url' | 'path'
  enabled: boolean
  priority: number
  path: string | null
  sha256: string
  size_bytes: number
  database_type: string
  build_epoch: string | null
  record_count: number
  ip_version: number
  languages: string[]
  description: string
  attribution: string
  update_url: string | null
  update_interval_hours: number
  update_auth: Record<string, unknown>
  last_update_at: string | null
  last_update_error: string | null
  uploaded_by: string | null
  version: number
  created_at: string
  updated_at: string
  loaded_here: boolean
  load_error: string | null
  has_file: boolean
}

export interface GeoDatabaseUpdate {
  name?: string
  enabled?: boolean
  priority?: number
  update_url?: string | null
  update_interval_hours?: number
  update_auth?: Record<string, unknown>
}

export interface GeoDatabaseFromUrl {
  name: string
  update_url: string
  update_interval_hours: number
  update_auth: Record<string, unknown>
  priority: number
  enabled: boolean
}

export interface GeoLookupCandidate {
  source: GeoSourceKind
  origin: string
  country: string | null
  location: IpLocation | null
}

export interface GeoResolution {
  country: string | null
  source: GeoSourceKind | null
  origin: string | null
  conflict: boolean
  disagreement: boolean
  claimed_country: string | null
  location: IpLocation | null
}

export interface GeoLookupResponse {
  ip: string
  resolution: GeoResolution
  policy: SourcePolicy
  candidates: GeoLookupCandidate[]
  databases_loaded: number
}

export interface IpObservation {
  id: number
  observed_at: string
  proxy_id: string | null
  connector_id: string | null
  connector_name: string | null
  project_id: string | null
  project_name: string | null
  session_id: string | null
  source: string
  ip: string
  claimed_country: string | null
  endpoint_country: string | null
  resolved_country: string | null
  resolved_source: string | null
  conflict: boolean
  disagreement: boolean
  candidates: { source: string; origin: string; country: string | null }[]
  instance_id: string
}

/** A contradicted pair: what the vendor claimed, what attribution resolved, how many exits. */
export interface ClaimBreakdown {
  claimed_country: string | null
  observed_country: string | null
  exits: number
}

/** A connector's distinct exits in the window and how their vendor claims were judged, each exit once. */
export interface ConnectorAccuracy {
  connector_id: string
  connector_name: string | null
  project_id: string | null
  exits: number
  claimed: number
  confirmed: number
  contradicted: number
  uncertain: number
  accuracy: number | null
  breakdown: ClaimBreakdown[]
  coverage: ExitCoverage | null
}

export interface GeoAccuracyResponse {
  since: string
  connectors: ConnectorAccuracy[]
}

/**
 * Present only for dynamic-sessions connectors, whose exits nothing but preflight sees.
 * Below 100% the unique-exit and reuse figures for session-less traffic are undercounts.
 */
export interface ExitCoverage {
  dynamic: boolean
  preflight_on: boolean
  sampled_percent: number
}

export interface ConnectorExits {
  connector_id: string
  connector_name: string | null
  project_id: string | null
  coverage: ExitCoverage | null
  unique_total: number
  unique_in_window: number
  sightings: number
  reused: number
  max_sightings: number
  last_seen: string | null
}

export interface GeoExitsResponse {
  since: string
  connectors: ConnectorExits[]
}

/** One distinct exit of a connector, with the state of its latest observation. */
export interface ExitIp {
  connector_id: string
  connector_name: string | null
  project_id: string | null
  project_name: string | null
  ip: string
  first_seen: string
  last_seen: string
  sightings: number
  country: string | null
  proxy_id: string | null
  source: string | null
  claimed_country: string | null
  resolved_source: string | null
  conflict: boolean
  disagreement: boolean
}

export interface ExitIpFilters {
  project_id?: string
  connector_id?: string
  ip?: string
  proxy_id?: string
  country?: string
  claimed_country?: string
  verdict?: ObservationVerdict
}

export interface ExitIpsPage {
  ips: ExitIp[]
  total: number
  limit: number
  offset: number
}

export interface GeoStatus {
  databases_loaded: number
  databases_total: number
  load_errors: Record<string, string>
  policy_from_database: boolean
  pending_observations: number
  published_observations: number
  dropped_observations: number
  stored_observations: number
  preflight_checks: number
  preflight_rejections: number
}

export const fetchGeoSettings = async (): Promise<GeoSettings> => (await api.get('/geo/settings')).data
export const updateGeoSettings = async (doc: GeoSettingsDoc): Promise<GeoSettings> => (await api.put('/geo/settings', doc)).data
export const fetchGeoDatabases = async (): Promise<GeoDatabase[]> => (await api.get('/geo/databases')).data.databases
export const uploadGeoDatabase = async (file: File, opts: { name?: string; priority?: number; enabled?: boolean } = {}): Promise<GeoDatabase> => {
  const formData = new FormData()
  formData.append('file', file)
  if (opts.name) formData.append('name', opts.name)
  if (opts.priority !== undefined) formData.append('priority', String(opts.priority))
  if (opts.enabled !== undefined) formData.append('enabled', opts.enabled ? 'true' : 'false')
  return (await api.post('/geo/databases', formData, { headers: { 'Content-Type': 'multipart/form-data' } })).data
}
export const addGeoDatabaseFromUrl = async (data: GeoDatabaseFromUrl): Promise<GeoDatabase> => (await api.post('/geo/databases/from-url', data)).data
export const updateGeoDatabase = async (id: string, data: GeoDatabaseUpdate): Promise<GeoDatabase> => (await api.patch(`/geo/databases/${id}`, data)).data
export const refreshGeoDatabase = async (id: string): Promise<GeoDatabase> => (await api.post(`/geo/databases/${id}/refresh`)).data
export const deleteGeoDatabase = async (id: string): Promise<void> => { await api.delete(`/geo/databases/${id}`) }
export const lookupGeoIp = async (ip: string, claimedCountry?: string, projectId?: string): Promise<GeoLookupResponse> =>
  (await api.post('/geo/lookup', { ip, claimed_country: claimedCountry || null, project_id: projectId || null })).data
export const reattributeProxies = async (connectorId?: string): Promise<{ scanned: number; updated: number }> =>
  (await api.post('/geo/reattribute', { connector_id: connectorId ?? null })).data
export type ObservationVerdict = 'contradicted' | 'uncertain' | 'confirmed' | 'no_claim'

export interface ObservationFilters {
  connector_id?: string
  proxy_id?: string
  project_id?: string
  source?: string
  ip?: string
  claimed_country?: string
  resolved_country?: string
  verdict?: ObservationVerdict
  conflicts_only?: boolean
}

export interface ObservationsPage {
  observations: IpObservation[]
  total: number
  limit: number
  offset: number
}

export const fetchGeoObservations = async (params: ObservationFilters & { limit?: number; offset?: number } = {}): Promise<ObservationsPage> =>
  (await api.get('/geo/observations', { params })).data
export const fetchGeoAccuracy = async (params: { project_id?: string; connector_id?: string; days?: number } = {}): Promise<GeoAccuracyResponse> =>
  (await api.get('/geo/accuracy', { params })).data
export const fetchGeoStatus = async (): Promise<GeoStatus> => (await api.get('/geo/status')).data
export const fetchGeoExits = async (params: { project_id?: string; connector_id?: string; days?: number } = {}): Promise<GeoExitsResponse> =>
  (await api.get('/geo/exits', { params })).data
export const fetchGeoExitIps = async (params: ExitIpFilters & { limit?: number; offset?: number } = {}): Promise<ExitIpsPage> =>
  (await api.get('/geo/exits/ips', { params })).data

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
  /** Exit country (ISO code). Omit to have it looked up through the proxy. */
  country?: string
}

export interface ProxyUpdate {
  host?: string
  port?: number
  protocol?: string
  username?: string
  password?: string
  tags?: string[]
  /** Exit country (ISO code); empty string clears it. */
  country?: string
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

// Credential types. `type` names a provider in the catalog (see /providers):
// the four code-implemented types plus any descriptor id.
export type CredentialType = string

// ---------------------------------------------------------------------------
// Provider catalog (schemas the forms render from) and admin descriptors

export type ProviderFieldType = 'text' | 'password' | 'number' | 'select' | 'boolean' | 'textarea' | 'url' | 'country'

export interface ProviderOption {
  value: string
  label: string
  description?: string | null
}

export interface ProviderCondition {
  field: string
  equals?: string | null
  in?: string[] | null
  negate?: boolean
}

export interface ProviderField {
  key: string
  label: string
  type: ProviderFieldType
  required: boolean
  secret: boolean
  readonly: boolean
  default: string | number | boolean | null
  placeholder: string | null
  help: string | null
  details: string | null
  group: string
  options: ProviderOption[]
  options_preset: 'countries' | null
  options_from: string | null
  options_from_when: ProviderCondition | null
  empty_label: string | null
  fill: Record<string, string>
  min: number | null
  max: number | null
  max_from_option: { field: string; extra: string }[]
  depends_on: string[]
  pattern: string | null
  transform: 'upper' | 'lower' | 'strip' | null
  show_when: ProviderCondition | null
}

export interface ProviderSummary {
  id: string
  name: string
  description: string
  kind: 'code' | 'descriptor'
  source: 'builtin' | 'file' | 'plugin' | 'custom'
  editable: boolean
  syncable: boolean
  cloud: boolean
  beta: boolean
  logo: string | null
  docs_url: string | null
  credential_fields: ProviderField[]
  connector_fields: ProviderField[]
  proxy_type_field: string | null
  proxy_types: { key: string; label: string; mode: string }[]
  egress_hosts: string[]
  gateway_hosts: string[]
  has_validation: boolean
  credential_count: number
  connector_count: number
  version: number
  updated_at: string | null
}

/** Raw descriptor document as stored/edited. Kept loose on purpose: the server validates it. */
export type ProviderSpec = Record<string, any>

export interface ProviderDetail extends ProviderSummary {
  spec: ProviderSpec | null
  origin: string
}

export interface ProviderListResponse {
  total: number
  providers: ProviderSummary[]
  presets: Record<string, ProviderOption[]>
  countries: CountryOption[]
}

export interface ProviderValidateResponse {
  valid: boolean
  errors: string[]
  warnings: string[]
  spec: ProviderSpec | null
  egress_hosts: string[]
  gateway_hosts: string[]
  discovery_hosts: string[]
  yaml: string | null
}

export interface ProviderTestResponse {
  ok: boolean
  message: string
  result: unknown
  traces: { method: string; url: string; status: number | null; elapsed_ms: number; error: string | null; page: number; headers: Record<string, string> }[]
}

export interface ProviderAuditEntry {
  id: string
  provider_id: string
  action: 'created' | 'updated' | 'deleted' | 'imported'
  actor: string
  egress_hosts: string[]
  hosts_changed: boolean
  created_at: string
}

export interface ResolvedProviderOption {
  value: string
  label: string
  description: string | null
  extra: Record<string, unknown>
}

/** 409 body returned when a descriptor's egress hosts still need confirming. */
export interface HostConfirmationRequired {
  detail: string
  egress_hosts: string[]
  unconfirmed_hosts: string[]
}

export const isHostConfirmationError = (error: unknown): HostConfirmationRequired | null => {
  const detail = (error as any)?.response?.data?.detail
  if ((error as any)?.response?.status === 409 && detail && Array.isArray(detail.unconfirmed_hosts)) return detail as HostConfirmationRequired
  return null
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
  /** Relative share of the project's traffic against its other connectors; omitted means 1. */
  weight?: number
}

export const DEFAULT_ROUTING_WEIGHT = 1
export const MAX_ROUTING_WEIGHT = 100

/** A connector's routing weight: the stored value when valid, else the default, matching the server. */
export const connectorWeight = (rc: RoutingConfig | undefined): number => {
  const w = rc?.weight
  return typeof w === 'number' && Number.isInteger(w) && w >= 1 && w <= MAX_ROUTING_WEIGHT ? w : DEFAULT_ROUTING_WEIGHT
}

export interface RateLimitConfig {
  max_requests?: number
  window_seconds?: number
  quarantine_seconds_min?: number
  quarantine_seconds_max?: number
  sticky_quarantine?: boolean
}

/** Intended pool size of a connector and how it is derived; total is null when there is no target. */
export interface ProxyTarget {
  total: number | null
  per_country: number | null
  countries: string[]
  on_demand: string[]
  /** Dynamic sessions: one gateway row, a vendor session per request; countries is the allow-list. */
  dynamic: boolean
  /** Dynamic sessions: share of session-less requests preflight echoes to observe their exit. */
  exit_sample_percent: number | null
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
  rate_limit_config: RateLimitConfig
  enabled: boolean
  proxy_count: number
  target: ProxyTarget | null
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
  rate_limit_config?: RateLimitConfig
  enabled?: boolean
}

export interface ConnectorUpdate {
  name?: string
  credential_id?: string
  config?: Record<string, unknown>
  routing_config?: RoutingConfig
  rate_limit_config?: RateLimitConfig
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
  countries: CountryOption[]
}

export interface PoolMetrics {
  total_proxies: number
  healthy_proxies: number
  unhealthy_proxies: number
  quarantined_proxies: number
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

// Backup / migration API functions
export interface UserConflict {
  original_username: string
  new_username: string
  new_id: boolean
  email_cleared: boolean
}

export interface ImportSummary {
  users: number
  projects: number
  credentials: number
  connectors: number
  proxies: number
  proxy_metrics: number
  project_metrics: number
  provider_descriptors: number
  provider_audit_log: number
  geo_settings: number
  geo_databases: number
  geo_database_blobs: number
  ip_observations: number
  connector_exit_ips: number
  kept_current_user: boolean
  user_conflicts: UserConflict[]
}

export const exportBackup = async (
  passphrase: string,
  includeMetrics: boolean,
  includeDatabaseFiles: boolean
): Promise<void> => {
  const response = await api.post(
    '/backup/export',
    { passphrase, include_metrics: includeMetrics, include_database_files: includeDatabaseFiles },
    { responseType: 'blob' }
  )
  // Derive the filename from the Content-Disposition header, falling back to a default.
  const disposition = response.headers['content-disposition'] || ''
  const match = disposition.match(/filename="?([^"]+)"?/)
  const filename = match ? match[1] : 'octoprox-backup.opbak'

  const url = URL.createObjectURL(response.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export const importBackup = async (
  file: File,
  passphrase: string,
  keepCurrentUser: boolean
): Promise<ImportSummary> => {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('passphrase', passphrase)
  formData.append('mode', 'replace')
  formData.append('keep_current_user', keepCurrentUser ? 'true' : 'false')
  const response = await api.post('/backup/import', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return response.data
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

export const locateProjectProxy = async (projectId: string, proxyId: string): Promise<Proxy> => {
  const response = await api.post(`/projects/${projectId}/proxies/${proxyId}/locate`)
  return response.data
}

export const unquarantineProjectProxy = async (projectId: string, proxyId: string): Promise<void> => {
  await api.post(`/projects/${projectId}/proxies/${proxyId}/unquarantine`)
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

export type TrafficSplitRange = '1h' | '6h' | '24h' | '7d' | '30d'

export interface ConnectorTrafficShare {
  connector_id: string
  name: string
  credential_type: CredentialType
  enabled: boolean
  weight: number
  dynamic: boolean
  total_proxies: number
  eligible_proxies: number
  /** Share of untargeted requests this connector takes right now, 0-100. */
  expected_share: number
  excluded_reason: 'disabled' | 'no_eligible_proxies' | null
  observed_requests: number
  /** Share of the window's requests that went through this connector, 0-100; null when nothing was observed. */
  observed_share: number | null
}

export interface TrafficSplitResponse {
  strategy: string
  range: TrafficSplitRange
  total_weight: number
  observed_requests: number
  connectors: ConnectorTrafficShare[]
}

export const fetchProjectTrafficSplit = async (projectId: string, range: TrafficSplitRange = '1h'): Promise<TrafficSplitResponse> => {
  const response = await api.get(`/projects/${projectId}/metrics/traffic-split`, { params: { range } })
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

// Provider catalog + admin descriptor API functions
export const fetchProviders = async (): Promise<ProviderListResponse> => {
  const response = await api.get('/providers')
  return response.data
}

export const fetchProvider = async (providerId: string): Promise<ProviderDetail> => {
  const response = await api.get(`/providers/${providerId}`)
  return response.data
}

export const resolveProviderOptions = async (
  providerId: string,
  optionName: string,
  body: { credential_id?: string; credential_config?: Record<string, unknown>; connector_config?: Record<string, unknown>; spec?: ProviderSpec },
): Promise<ResolvedProviderOption[]> => {
  const response = await api.post(`/providers/${providerId}/options/${optionName}`, body)
  return response.data.options
}

export const validateProviderSpec = async (spec: ProviderSpec): Promise<ProviderValidateResponse> => {
  const response = await api.post('/providers/validate', { spec })
  return response.data
}

export const createProvider = async (spec: ProviderSpec, confirmedHosts: string[]): Promise<ProviderDetail> => {
  const response = await api.post('/providers', { spec, confirmed_hosts: confirmedHosts })
  return response.data
}

export const updateProvider = async (
  providerId: string,
  data: { spec?: ProviderSpec; enabled?: boolean; confirmed_hosts?: string[] },
): Promise<ProviderDetail> => {
  const response = await api.put(`/providers/${providerId}`, data)
  return response.data
}

export const deleteProvider = async (providerId: string): Promise<void> => {
  await api.delete(`/providers/${providerId}`)
}

export const importProviderYaml = async (yaml: string, confirmedHosts: string[], replace = false): Promise<ProviderDetail> => {
  const response = await api.post('/providers/import', { yaml, confirmed_hosts: confirmedHosts, replace })
  return response.data
}

export const exportProviderYaml = async (providerId: string): Promise<string> => {
  const response = await api.get(`/providers/${providerId}/export`, { responseType: 'text', transformResponse: [(d) => d] })
  return response.data as string
}

export const fetchProviderAudit = async (providerId: string): Promise<{ total: number; entries: ProviderAuditEntry[] }> => {
  const response = await api.get(`/providers/${providerId}/audit`)
  return response.data
}

export type ProviderTestAction = 'validate' | 'options' | 'list_proxies' | 'proxy_request'

/**
 * Exercise a stored provider (built-in or custom) or an unsaved draft (`spec`).
 * `proxy_request` provisions one proxy endpoint in memory and fetches `target_url` through it.
 */
export const testProvider = async (
  providerId: string,
  body: { action: ProviderTestAction; credential_config: Record<string, unknown>; connector_config?: Record<string, unknown>; option_name?: string; target_url?: string; spec?: ProviderSpec },
): Promise<ProviderTestResponse> => {
  const response = await api.post(`/providers/${providerId}/test`, body)
  return response.data
}

// MITM Inspector types
export interface TlsClientHello {
  version: string
  supported_versions: string[]
  cipher_suites: { id: string; name: string }[]
  extensions: { id: number; name: string }[]
  sni: string
  alpn: string[]
  supported_groups: { id: number; name: string }[]
  signature_algorithms: { id: string; name: string }[]
  ec_point_formats: number[]
  compression_methods: { id: number; name: string }[]
  session_id_length: number
  record_layer_version: string
  key_share_groups: { group: string; key_length: number }[]
  compress_certificate: { id: number; name: string }[]
  alps_protocols: string[]
  psk_key_exchange_modes: { id: number; name: string }[]
  ja3: string
  ja3_full: string
  ja4: string
  ja4_r: string
}

export interface MitmRequestRecord {
  id: string
  timestamp: string
  method: string
  url: string
  request_headers: [string, string][]
  upstream_headers: [string, string][]
  request_body_size: number
  request_content_type: string
  status_code: number
  response_headers: [string, string][]
  response_body_size: number
  response_content_type: string
  target_host: string
  proxy_url: string
  mitm_mode: string
  mitm_engine: string
  mitm_browser: string
  latency_ms: number
  tls_version: string
  tls_cipher: string
  tls_key_bits: number
  tls_shared_ciphers: string[]
  tls_client_hello: TlsClientHello | null
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

// User types
export interface UserAccount {
  id: string
  username: string
  email: string
  role: UserRole
  is_active: boolean
  has_password: boolean
  theme_preference: string
  last_login_at: string | null
  created_at: string
  updated_at: string
}

export interface UserListResponse {
  total: number
  users: UserAccount[]
}

export interface UserCreate {
  username: string
  email?: string
  password: string
  role: UserRole
}

export interface UserInviteCreate {
  username: string
  email?: string
  role: UserRole
}

export interface InviteResponse {
  user: UserAccount
  invite_url: string
}

export interface UserUpdate {
  username?: string
  email?: string
  password?: string
  role?: UserRole
  is_active?: boolean
  theme_preference?: string
}

export interface UserSelfUpdate {
  email?: string
  password?: string
  current_password?: string
  theme_preference?: string
}

// User API functions
export const fetchUsers = async (): Promise<UserListResponse> => {
  const response = await api.get('/users')
  return response.data
}

export const createUser = async (data: UserCreate): Promise<UserAccount> => {
  const response = await api.post('/users', data)
  return response.data
}

export const inviteUser = async (data: UserInviteCreate): Promise<InviteResponse> => {
  const response = await api.post('/users/invite', data)
  return response.data
}

export const reinviteUser = async (userId: string): Promise<InviteResponse> => {
  const response = await api.post(`/users/${userId}/reinvite`)
  return response.data
}

export const updateUser = async (id: string, data: UserUpdate): Promise<UserAccount> => {
  const response = await api.patch(`/users/${id}`, data)
  return response.data
}

export const deleteUser = async (id: string): Promise<void> => {
  await api.delete(`/users/${id}`)
}

export const fetchCurrentUser = async (): Promise<UserAccount> => {
  const response = await api.get('/users/me')
  return response.data
}

export const updateSelf = async (data: UserSelfUpdate): Promise<UserAccount> => {
  const response = await api.patch('/users/me', data)
  return response.data
}

export const setPasswordWithToken = async (token: string, password: string): Promise<LoginResponse> => {
  const response = await api.post('/auth/set-password', { token, password })
  const data = response.data as LoginResponse
  auth.setToken(data.access_token)
  return data
}

// System statistics (admin only)
export interface SystemRuntime {
  version: string
  instance_id: string
  role: string
  environment: string
  python_version: string
  platform: string
  pid: number
  started_at: string | null
  uptime_seconds: number
  api_port: number
  proxy_port: number
  log_level: string
  health_check_interval: number
  metrics_flush_interval: number
  ip_refresh_interval: number
}

export interface SystemInventory {
  projects: number
  credentials: number
  connectors: number
  connectors_enabled: number
  connectors_failing: number
  proxies: number
  users: number
  users_active: number
  users_by_role: Record<string, number>
  providers_total: number
  providers_builtin: number
  providers_custom: number
  custom_providers_enabled: number
  proxies_by_status: Record<string, number>
}

export interface SystemProjectUsage {
  id: string
  name: string
  credentials: number
  connectors: number
  proxies: number
}

export interface SystemTableStats {
  name: string
  row_estimate: number | null
  total_bytes: number
  table_bytes: number
  index_bytes: number
}

export interface SystemDatabase {
  name: string
  size_bytes: number
  tables: SystemTableStats[]
  backends: number | null
  pool_size: number | null
  pool_checked_out: number | null
  error: string | null
}

export interface SystemRedis {
  version: string
  uptime_seconds: number
  used_memory_bytes: number
  used_memory_peak_bytes: number
  used_memory_rss_bytes: number
  maxmemory_bytes: number
  connected_clients: number
  ops_per_sec: number
  keyspace_hits: number
  keyspace_misses: number
  total_keys: number
  groups: { label: string; keys: number }[]
  scanned_keys: number
  truncated: boolean
  error: string | null
}

export interface SystemCache {
  projects: number
  credentials: number
  connectors: number
  proxies: number
  project_strategies: number
  geo_provision_locks: number
  pending_proxy_deltas: number
  pending_project_deltas: number
  quarantined_proxies: number
  tls_contexts: number
  provider_types: number
}

export type WorkerState = 'running' | 'done' | 'cancelled' | 'failed'

/** 'instance' runs on every instance; 'singleton' only on the lease holder. */
export type WorkerScope = 'instance' | 'singleton'

export interface SystemWorkerTask {
  name: string
  description: string
  scope: WorkerScope
  /** Lease this worker elects on, matching SystemLease.name (or its prefix). */
  lease: string | null
  /**
   * Whether that lease is taken per resource. A global singleton holds its
   * lease continuously; a per-resource worker takes one per connector and
   * releases it a moment later, so between ticks no lease exists at all -
   * idle, not standing by, and several instances can hold different ones.
   */
  lease_per_resource: boolean
  /** State of the asyncio task itself - whether the loop still exists. */
  state: WorkerState
  /** The exception that ended the loop, when state is not 'running'. */
  error: string | null
  /** Cadence the loop was started with; null for the event-driven subscribers. */
  interval_seconds: number | null
  /** Counters for the cycles inside the loop, since this process started. */
  runs: number
  /**
   * Runs that found nothing to do - an empty buffer, a shard owned by peers, a
   * snapshot already covered. A subset of `runs`, so `runs - idle_runs` is the
   * cycles that did something, and the durations describe those only.
   */
  idle_runs: number
  failures: number
  /** Cycles that ran longer than interval_seconds, i.e. pushed the loop off cadence. */
  overruns: number
  /** Streak of those, cleared by the first cycle back inside the cadence: is it behind now? */
  consecutive_overruns: number
  last_overrun_at: string | null
  consecutive_failures: number
  last_run_at: string | null
  last_duration_ms: number | null
  avg_duration_ms: number | null
  max_duration_ms: number | null
  /** Last cycle error, which - unlike `error` - the loop recovered from. */
  last_error: string | null
  last_error_at: string | null
}

export interface SystemLease {
  name: string
  kind: string
  /** Name of the background worker that takes this lease. */
  worker: string
  target: string | null
  holder: string
  held_by_self: boolean
  ttl_ms: number
}

/**
 * The sections of a stats response that describe one process, as that process
 * published them on its own heartbeat. Every instance reports these about
 * itself, which is how the System page shows workers for an instance other
 * than the one the load balancer routed the request to.
 */
export interface SystemInstanceSnapshot {
  runtime: SystemRuntime
  cache: SystemCache
  tasks: SystemWorkerTask[]
  proxy_server_listening: boolean
  proxy_server_connections: number
  geo_lookup_enabled: boolean
  geo_lookups_in_flight: number
}

export interface SystemInstance {
  instance_id: string
  role: string
  is_self: boolean
  ttl_seconds: number
  /**
   * What that instance last published about itself. Published alongside its
   * membership, so a live instance normally has one; null when it runs a
   * version predating snapshots. Present for this instance too, a few seconds
   * behind the live top-level sections of the same response.
   */
  snapshot: SystemInstanceSnapshot | null
  /** Seconds since the snapshot was published; null when there is none. */
  age_seconds: number | null
}

export interface SystemWorkers {
  tasks: SystemWorkerTask[]
  leases: SystemLease[]
  instances: SystemInstance[]
  proxy_server_listening: boolean
  proxy_server_connections: number
  geo_lookup_enabled: boolean
  geo_lookups_in_flight: number
}

export interface SystemStats {
  generated_at: string
  runtime: SystemRuntime
  inventory: SystemInventory
  projects: SystemProjectUsage[]
  database: SystemDatabase
  redis: SystemRedis
  cache: SystemCache
  workers: SystemWorkers
}

export const fetchSystemStats = async (): Promise<SystemStats> => {
  const response = await api.get('/system/stats')
  return response.data
}

export const SYSTEM_HISTORY_RANGES = ['1h', '24h', '7d', '30d', '90d'] as const
export type SystemHistoryRange = (typeof SYSTEM_HISTORY_RANGES)[number]

export interface SystemMetricsPoint {
  timestamp: string
  database_size_bytes: number
  redis_memory_bytes: number
  redis_keys: number
  projects: number
  credentials: number
  connectors: number
  connectors_enabled: number
  users: number
  proxies_total: number
  proxies_healthy: number
  proxies_unhealthy: number
}

export interface TableGrowth {
  name: string
  first_bytes: number
  last_bytes: number
  delta_bytes: number
}

export interface SystemMetricsHistory {
  range: SystemHistoryRange
  /** Null when points are raw snapshots; set when they are bucket averages. */
  bucket_seconds: number | null
  interval_seconds: number
  snapshots: SystemMetricsPoint[]
  table_growth: TableGrowth[]
}

export const fetchSystemHistory = async (
  range: SystemHistoryRange
): Promise<SystemMetricsHistory> => {
  const response = await api.get('/system/stats/history', { params: { range } })
  return response.data
}

export default api

