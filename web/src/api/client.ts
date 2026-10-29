// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import axios from 'axios'
import { noteServerVersion, VERSION_HEADER } from '../utils/appVersion'

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

// Every response names the server's release, so an open UI notices a new one.
api.interceptors.response.use(
  (response) => {
    noteServerVersion(response.headers[VERSION_HEADER.toLowerCase()])
    return response
  },
  (error) => {
    noteServerVersion(error.response?.headers?.[VERSION_HEADER.toLowerCase()])
    return Promise.reject(error)
  },
)

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
  /** IP attribution: what to do when the independent sources agree against the vendor's claim. */
  location_contradicted: ContradictedAction
  /** IP attribution: what to do when nothing independent places the exit. */
  location_unknown: UnknownAction
  /** IP attribution: verify a session's exit before its first request (one echo per session). */
  location_preflight: boolean
  /** Conflict rule override; null inherits the install default. */
  location_conflict_rule: ConflictRule | null
  /** Km from a claimed city that still confirms it (0 compares names only); null inherits the install default. */
  location_city_radius_km: number | null
  /** Tell clients each request's exit IP in X-Octoprox-* response headers. */
  exit_headers: boolean
  created_at: string
  updated_at: string
}

/**
 * The databases and the echo agree against the vendor's claim: route the proxy
 * on the evidence (where they put the exit), route on the vendor's claim anyway
 * and only show the contradiction, or refuse the proxy (and replace it when it
 * is a pooled session slot).
 */
export type ContradictedAction = 'evidence' | 'vendor' | 'refuse'
/**
 * Nothing independent places the exit (no database knows the IP, or they
 * disagree with each other under consensus): route on the vendor's word, or
 * refuse the proxy until a later sighting places it.
 */
export type UnknownAction = 'vendor' | 'refuse'
/**
 * What a disagreement between databases is: consensus leaves the claim open,
 * first lets the top-priority database judge, any confirms the claim when any
 * database agrees with the vendor (routing then uses the agreeing answer).
 */
export type ConflictRule = 'consensus' | 'first' | 'any'

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
  location_contradicted?: ContradictedAction
  location_unknown?: UnknownAction
  location_preflight?: boolean
  location_conflict_rule?: ConflictRule | null
  location_city_radius_km?: number | null
  exit_headers?: boolean
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
  location_contradicted?: ContradictedAction
  location_unknown?: UnknownAction
  location_preflight?: boolean
  /** An empty string clears the override. */
  location_conflict_rule?: ConflictRule | '' | null
  /** An empty string clears the override. */
  location_city_radius_km?: number | '' | null
  exit_headers?: boolean
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
  location_verdict: 'confirmed' | 'contradicted' | 'open' | null
  /** How a claimed state or city fared; null when none was claimed. */
  state_verdict: 'confirmed' | 'contradicted' | 'open' | null
  city_verdict: 'confirmed' | 'contradicted' | 'open' | null
  /** The evidence puts the exit outside the connector's declared countries: skipped for every request, kept. */
  outside_declared_countries: boolean
  /** Full record from the IP databases, when any covers the exit IP. */
  location: IpLocation | null
  /** State code and city that -st- and -city- routing matches this exit on, each with its source. */
  state_code: string | null
  /** manual when pinned by hand, else the attribution source the policy let decide (database, vendor, endpoint). */
  state_source: string | null
  city: string | null
  city_source: string | null
  tags: string[]
  created_at: string
}

// --- IP attribution -----------------------------------------------------------------

export interface IpLocation {
  country?: string | null
  region?: string | null
  /** ISO 3166-2 subdivision part (NY, ON, ENG), when the database knows it. */
  state_code?: string | null
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

/**
 * How a vendor's claim is judged for one project. The routing value is the
 * databases in their priority, then the echo, then the vendor filling the gap,
 * unless `contradicted` is `vendor`, in which case a contradicted level carries
 * the claim.
 */
export interface SourcePolicy {
  conflict_rule: ConflictRule
  contradicted: ContradictedAction
  /** Km from a claimed city that still confirms it; takes effect once a GeoNames file is loaded. */
  city_radius_km: number
}

/** Install-wide attribution settings (one typed row). Projects inherit the default_* judgement. */
export interface GeoSettingsDoc {
  default_conflict_rule: ConflictRule
  /** Km from a claimed city that still confirms it, for projects without their own; needs a GeoNames file. */
  default_city_radius_km: number
  echo_url: string
  echo_ip_path: string
  echo_country_path: string | null
  echo_state_path: string | null
  echo_city_path: string | null
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
  /** The databases the api process that answered has open, in lookup order. Where requests are judged, see GeoDatabase.gateways_loaded. */
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
  /** Whether vendor download credentials are stored; the credentials themselves are never sent back. */
  has_update_auth: boolean
  /** The schedule is kept but skipped; Download now still runs. */
  update_paused: boolean
  last_update_at: string | null
  last_update_error: string | null
  uploaded_by: string | null
  version: number
  created_at: string
  updated_at: string
  /** Whether the api process that answered has it open (lookups and /echo use its copy), and why not when opening it failed there. */
  loaded_here: boolean
  load_error: string | null
  has_file: boolean
  /** When the schedule is next due (the IP database update job, hourly by default, downloads it at its first run after); null without a schedule, or while paused or disabled. */
  next_update_at: string | null
  /** How many of the live gateways have it open, out of how many, as their heartbeats (a few seconds old) report it. */
  gateways_loaded: number
  gateways_total: number
  /** Why it is not open on the gateways that report a reason, by gateway instance id. */
  gateway_errors: Record<string, string>
}

export interface GeoDatabaseUpdate {
  name?: string
  enabled?: boolean
  priority?: number
  update_url?: string | null
  update_interval_hours?: number
  update_auth?: Record<string, unknown>
  update_paused?: boolean
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

/** What the resolver decided for one IP: the claim, what independent evidence observed, and what routing uses. */
export interface GeoResolution {
  claimed_country: string | null
  observed_country: string | null
  resolved_country: string | null
  resolved_source: GeoSourceKind | null
  resolved_origin: string | null
  /** One verdict per level: true contradicted, false confirmed, null no verdict. */
  country_conflict: boolean | null
  location: IpLocation | null
  claimed_city: string | null
  observed_city: string | null
  resolved_city: string | null
  city_conflict: boolean | null
  /** Km between the observed and the claimed city, when both could be placed. */
  city_distance_km: number | null
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
  /** One verdict per level: true contradicted, false confirmed, null no verdict (nothing independent answered, or sources disagreed). */
  country_conflict: boolean | null
  claimed_state: string | null
  claimed_city: string | null
  resolved_state: string | null
  resolved_city: string | null
  state_conflict: boolean | null
  city_conflict: boolean | null
  candidates: { source: string; origin: string; country: string | null }[]
  instance_id: string
}

export type PlaceLevel = 'country' | 'state' | 'city'

/** A contradicted pair at one level: what was claimed, what attribution resolved, how many exits. */
export interface ClaimBreakdown {
  level: PlaceLevel
  claimed: string | null
  observed: string | null
  exits: number
}

/**
 * How a connector's exits with a claim at one level were judged, each exit once.
 * `open` is a claim without a verdict: no independent source answered at that
 * level, or they disagreed. It counts in neither: `accuracy` is confirmed over
 * confirmed plus contradicted.
 */
export interface LevelAccuracy {
  claimed: number
  confirmed: number
  contradicted: number
  open: number
  accuracy: number | null
}

/** A connector's distinct exits in the window and how their vendor claims were judged, each exit once. */
export interface ConnectorAccuracy {
  connector_id: string
  connector_name: string | null
  project_id: string | null
  exits: number
  country: LevelAccuracy
  state: LevelAccuracy
  city: LevelAccuracy
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

/** A connector's distinct exits seen in the window, how many it reused (ever) and how many another connector also had. */
export interface ConnectorSharing {
  connector_id: string
  connector_name: string | null
  /** The connector's credential type. */
  provider: string | null
  coverage: ExitCoverage | null
  exits: number
  reused: number
  shared: number
}

/** The exits two connectors both handed out in the window; `connector_id` sorts before `other_id`. */
export interface SharedPair {
  connector_id: string
  other_id: string
  shared: number
}

export interface GeoExitSharingResponse {
  since: string
  /** Distinct exits across the project's connectors. */
  exits: number
  /** Of those, handed out by more than one connector. */
  shared: number
  connectors: ConnectorSharing[]
  pairs: SharedPair[]
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
  country_conflict: boolean | null
  claimed_state: string | null
  claimed_city: string | null
  resolved_state: string | null
  resolved_city: string | null
  state_conflict: boolean | null
  city_conflict: boolean | null
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
  /** Counted as for observations: a lower bound when `total_exact` is false. */
  total: number
  total_exact: boolean
  limit: number
  offset: number
}

/** One live gateway's attribution, as its last heartbeat (a few seconds old) reports it. */
export interface GatewayGeoStatus {
  instance_id: string
  /** The IP databases it has open, the ids it still waits for, and why the others are not open, by database id. */
  databases_loaded: number
  databases_missing: string[]
  load_errors: Record<string, string>
  /** Echoes preflight sent through an upstream, and verdicts that placed the exit elsewhere, since the gateway started. */
  preflight_checks: number
  preflight_rejections: number
  /** Observations and preflight sightings waiting for Redis, and those dropped since start because Redis would not take them. */
  observations_pending: number
  observations_dropped: number
  sightings_pending: number
  sightings_dropped: number
}

export interface GeoStatus {
  /** The api process that answered: the databases it has open, out of every database listed, and why the others are not open there. */
  databases_loaded: number
  databases_total: number
  load_errors: Record<string, string>
  policy_from_database: boolean
  /** The answering api process's own observation buffer (Detect records into it); each gateway's is in `gateways`. */
  pending_observations: number
  published_observations: number
  dropped_observations: number
  /** Sightings and verdict rewrites on the observations stream that no worker has ingested yet; exit IPs and accuracy lag until they are. Null when Redis cannot say. */
  queued_observations: number | null
  /** The planner's estimate, within a few percent: an exact count would read the whole table. */
  stored_observations: number
  /** Preflight runs on gateways: summed over every live gateway, since each started. */
  preflight_checks: number
  preflight_rejections: number
  /** Every live gateway, by instance id; empty when no gateway reports or the heartbeats cannot be read. */
  gateways: GatewayGeoStatus[]
}

/** A re-attribution a worker will run: the job's kind and the key it is queued under ('all' or 'connector:<id>'). */
export interface ReattributeQueued {
  kind: 'reattribute'
  key: string
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
export const lookupGeoIp = async (ip: string, claimedCountry?: string, projectId?: string, claimedCity?: string): Promise<GeoLookupResponse> =>
  (await api.post('/geo/lookup', { ip, claimed_country: claimedCountry || null, claimed_city: claimedCity || null, project_id: projectId || null })).data
/** Queues a re-attribution of one connector's proxies, or of every proxy; answers 202 before a worker runs it. */
export const reattributeProxies = async (connectorId?: string): Promise<ReattributeQueued> =>
  (await api.post('/geo/reattribute', { connector_id: connectorId ?? null })).data
export type ObservationVerdict = 'contradicted' | 'open' | 'confirmed' | 'no_claim'

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
  /** Start the log at this moment (ISO time) instead of now. */
  until?: string
}

export interface ObservationsPage {
  observations: IpObservation[]
  /** Counted no further than 10,000 rows past this page: a lower bound when `total_exact` is false. */
  total: number
  total_exact: boolean
  limit: number
  offset: number
}

export const fetchGeoObservations = async (params: ObservationFilters & { limit?: number; offset?: number } = {}): Promise<ObservationsPage> =>
  (await api.get('/geo/observations', { params })).data
/** Every observation under the filters, counted outright. Slow under filters no index serves; the server gives up after 30 s with a 504. */
export const countGeoObservations = async (params: ObservationFilters = {}): Promise<number> =>
  (await api.get('/geo/observations/count', { params })).data.total
export const fetchGeoAccuracy = async (params: { project_id?: string; connector_id?: string; days?: number } = {}): Promise<GeoAccuracyResponse> =>
  (await api.get('/geo/accuracy', { params })).data
export const fetchGeoStatus = async (): Promise<GeoStatus> => (await api.get('/geo/status')).data
export const fetchGeoExits = async (params: { project_id?: string; connector_id?: string; days?: number } = {}): Promise<GeoExitsResponse> =>
  (await api.get('/geo/exits', { params })).data
export const fetchGeoExitSharing = async (params: { project_id: string; days?: number }): Promise<GeoExitSharingResponse> =>
  (await api.get('/geo/exits/sharing', { params })).data
export const fetchGeoExitIps = async (params: ExitIpFilters & { limit?: number; offset?: number } = {}): Promise<ExitIpsPage> =>
  (await api.get('/geo/exits/ips', { params })).data
export const countGeoExitIps = async (params: ExitIpFilters = {}): Promise<number> =>
  (await api.get('/geo/exits/ips/count', { params })).data.total

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
  /** Exit state code (NY, ON, ENG) and city, pinned by hand for -st- and -city- routing. */
  state?: string
  city?: string
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
  /** Exit state code and city pinned by hand; empty string clears each. */
  state?: string
  city?: string
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

export type TrafficPeriod = 'day' | 'week' | 'month'
export type TrafficLimitAction = 'alert' | 'block' | 'interrupt'
export type TrafficLimitStatus = 429 | 509

/**
 * Traffic accounting, limit and pricing for a connector. Only explicit choices are
 * stored; a missing key means the default in TRAFFIC_DEFAULTS.
 */
export interface TrafficConfig {
  /** Bytes per period before the action applies; omitted means metering only. */
  limit_bytes?: number
  period?: TrafficPeriod
  /** Day of month (1-28) for monthly periods, weekday (1 = Monday) for weekly ones. */
  reset_day?: number
  action?: TrafficLimitAction
  warn_percent?: number
  limit_status?: TrafficLimitStatus
  price_per_gb?: number
  currency?: string
}

export const TRAFFIC_DEFAULTS: Required<Omit<TrafficConfig, 'limit_bytes' | 'price_per_gb'>> = {
  period: 'month',
  reset_day: 1,
  action: 'alert',
  warn_percent: 80,
  limit_status: 509,
  currency: 'USD',
}

/** Vendors bill decimal gigabytes; limits and prices are entered in these. */
export const BYTES_PER_GB = 1_000_000_000

export interface TrafficUsage {
  period: TrafficPeriod
  period_start: string
  period_end: string
  bytes_sent: number
  bytes_received: number
  total_bytes: number
  limit_bytes: number | null
  /** total_bytes as a share of the limit, 0-100 and beyond; null without a limit. */
  percent: number | null
  action: TrafficLimitAction
  status: 'ok' | 'warning' | 'exceeded'
  /**
   * True while the connector takes no new requests because of its limit: the
   * block the traffic job stored, which gateways enforce. After a reset or a
   * raised limit it stays true, with pending, until a worker decides again;
   * false while the api cannot read Redis.
   */
  blocked: boolean
  /**
   * True from a traffic reset or a limit edit until a worker stores the
   * traffic decision it asked for; false while the api cannot read Redis.
   */
  pending: boolean
  price_per_gb: number | null
  currency: string
  cost: number | null
  /** Usage counts from here when set: a manual reset inside the period. */
  reset_at: string | null
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
  traffic_config: TrafficConfig
  traffic_reset_at: string | null
  traffic_usage: TrafficUsage | null
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
  traffic_config?: TrafficConfig
  enabled?: boolean
}

export interface ConnectorUpdate {
  name?: string
  credential_id?: string
  config?: Record<string, unknown>
  routing_config?: RoutingConfig
  rate_limit_config?: RateLimitConfig
  traffic_config?: TrafficConfig
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

/**
 * A project's pool: the proxies of its enabled connectors that no traffic
 * limit blocks. Statuses come from Postgres as the health jobs wrote them,
 * quarantines from Redis.
 */
export interface PoolMetrics {
  total_proxies: number
  /** Healthy and not quarantined: what requests can be routed to. */
  healthy_proxies: number
  /** Neither routable nor on its way out: every status but healthy, draining and terminating, plus quarantined healthy proxies. */
  unhealthy_proxies: number
  /** Quarantined by a rate limit, whatever their status, so they are also inside unhealthy (or draining, terminating). */
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

/** A pool split into parts that add up to its total, each proxy counted once. */
export interface PoolBreakdown {
  total: number
  healthy: number
  quarantined: number
  /** Not routable for another reason: unhealthy, initializing, unknown. */
  unhealthy: number
  /** Draining or terminating: on their way out. */
  leaving: number
}

/**
 * The pool as one bar can draw it. The API counts a quarantined proxy inside
 * unhealthy_proxies as well, so drawing both as they come counts it twice and
 * pushes draining and terminating proxies off the bar. Taking quarantines out
 * of unhealthy is exact unless a quarantined proxy is also draining or
 * terminating, which is rare and short-lived.
 */
export const poolBreakdown = (pool: PoolMetrics | undefined): PoolBreakdown => {
  const total = pool?.total_proxies ?? 0
  const healthy = pool?.healthy_proxies ?? 0
  const quarantined = pool?.quarantined_proxies ?? 0
  const unhealthy = Math.max(0, (pool?.unhealthy_proxies ?? 0) - quarantined)
  return { total, healthy, quarantined, unhealthy, leaving: Math.max(0, total - healthy - quarantined - unhealthy) }
}

/**
 * Save the MITM CA certificate. The api reads it from the CA volume the
 * gateways share, and a gateway creates it when it first starts, so until
 * then there is none: that 404 comes back as an error saying so.
 */
export const downloadCaCertificate = async (): Promise<void> => {
  let response
  try {
    response = await api.get('/projects/ca-certificate', { responseType: 'blob' })
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      throw new Error('There is no CA certificate yet: a gateway creates it when it first starts, on the CA volume the api reads it from.')
    }
    throw error
  }
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

/** One connector a request with the generated username would reach now. */
export interface ConnectRoute {
  connector_id: string
  connector_name: string
  /** Dynamic sessions: the place is rendered into a fresh vendor session per request. */
  dynamic: boolean
  /** Nothing serves the country yet: this pool provisions a slot group for it on the first request. */
  on_demand: boolean
  proxies: number
  /** Expected share of requests by routing weight, 0..1. */
  share: number
}

/** A state or city some of the project's fixed exits are known to be in. */
export interface ConnectPlace {
  country: string
  state: string | null
  city: string | null
  proxies: number
}

export interface ConnectInfo {
  /**
   * Where clients reach the proxy: https when a load balancer terminates TLS in front of it.
   * host is null when no public address is configured (use the host the UI was reached at);
   * port is null when the public URL names none (the scheme's default; https URLs leave it out).
   */
  endpoint: { scheme: 'http' | 'https'; host: string | null; port: number | null }
  /** Canonical when it parses, as composed when it does not. */
  username: string
  sessid: string | null
  location: { country: string | null; state: string | null; city: string | null } | null
  /** The proxy's 400 message for this username, or null. */
  error: string | null
  routes: ConnectRoute[]
  countries: string[]
  /** A country outside `countries` is served too. */
  any_country: boolean
  places: ConnectPlace[]
}

export interface ConnectParams {
  country?: string
  state?: string
  city?: string
  sessid?: string
}

export const fetchConnectInfo = async (projectId: string, params: ConnectParams): Promise<ConnectInfo> =>
  (await api.get(`/projects/${projectId}/connect`, { params })).data

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

/** A part of a request's latency, in the order a request goes through them. */
export type LatencyStep = 'route' | 'preflight' | 'connect' | 'handshake' | 'response'

export interface MetricsSnapshot {
  timestamp: string
  request_count: number
  success_count: number
  failure_count: number
  /** Over the successful requests; null when none succeeded. */
  avg_latency_ms: number | null
  /** Each step's average over the successful requests, adding up to avg_latency_ms; null when none succeeded or before steps were kept. */
  step_latency_ms: Partial<Record<LatencyStep, number>> | null
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

/** The windows project, connector, device and host history serve; the shared time range narrows to these. */
export const HISTORY_RANGES = ['1h', '6h', '24h', '7d', '30d'] as const
export type HistoryRange = (typeof HISTORY_RANGES)[number]
/** Twice the interval between a range's history points: a longer stretch without one is a gap a chart breaks its lines at. */
export const HISTORY_GAP_MS: Record<HistoryRange, number> = {
  '1h': 120_000, '6h': 120_000, '24h': 120_000, '7d': 2 * 3600_000, '30d': 2 * 6 * 3600_000,
}
export type TrafficSplitRange = HistoryRange

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
  excluded_reason: 'disabled' | 'traffic_limit' | 'no_eligible_proxies' | null
  observed_requests: number
  /** Share of the window's requests that went through this connector, 0-100; null when nothing was observed. */
  observed_share: number | null
  /** Bytes both ways in the window, and their cost at the connector's price (null when unpriced). */
  observed_bytes: number
  cost: number | null
  currency: string | null
}

export interface TrafficSplitResponse {
  strategy: string
  range: TrafficSplitRange
  total_weight: number
  observed_requests: number
  observed_bytes: number
  connectors: ConnectorTrafficShare[]
}

export const fetchProjectTrafficSplit = async (projectId: string, range: TrafficSplitRange = '1h'): Promise<TrafficSplitResponse> => {
  const response = await api.get(`/projects/${projectId}/metrics/traffic-split`, { params: { range } })
  return response.data
}

// Hosts: where a project's traffic goes, by destination host and connector.

export type HostsRange = TrafficSplitRange

/** One connector's part of a host's traffic in the window. */
export interface HostConnectorMetrics {
  connector_id: string
  /** Null when the connector was deleted after the rows were written (a flush-window race). */
  name: string | null
  credential_type: CredentialType | null
  request_count: number
  success_count: number
  failure_count: number
  avg_latency_ms: number
  bytes_sent: number
  bytes_received: number
}

export interface HostMetrics {
  host: string
  request_count: number
  success_count: number
  failure_count: number
  avg_latency_ms: number
  bytes_sent: number
  bytes_received: number
  /** Share of the window's requests and of its bytes (both directions), 0-100. */
  request_share: number
  bytes_share: number
  connectors: HostConnectorMetrics[]
}

export interface HostsTotals {
  host_count: number
  request_count: number
  success_count: number
  failure_count: number
  avg_latency_ms: number
  bytes_sent: number
  bytes_received: number
}

export interface HostsConnector {
  connector_id: string
  name: string
  credential_type: CredentialType
  enabled: boolean
}

export interface HostsResponse {
  range: HostsRange
  since: string
  /** The row hosts past the per-window cap are folded into. */
  overflow_host: string
  /** Whether per-host counting is on at all (metrics.hosts.enabled). */
  enabled: boolean
  /** How many hosts the response holds at most; totals.host_count says how many matched. */
  limit: number
  totals: HostsTotals
  connectors: HostsConnector[]
  hosts: HostMetrics[]
}

export interface HostSeriesPoint {
  timestamp: string
  request_count: number
  success_count: number
  failure_count: number
  avg_latency_ms: number
  bytes_sent: number
  bytes_received: number
}

export interface HostSeries {
  host: string
  points: HostSeriesPoint[]
}

export interface HostsHistoryResponse {
  range: HostsRange
  since: string
  bucket_seconds: number
  overflow_host: string
  /** Busiest first; the remainder of the hosts last under overflow_host. Every series has every bucket. */
  series: HostSeries[]
}

export interface HostsQuery {
  range: HostsRange
  connectorId?: string | null
  search?: string
  limit?: number
}

export const fetchProjectHostMetrics = async (projectId: string, query: HostsQuery): Promise<HostsResponse> => {
  const params: Record<string, string | number> = { range: query.range }
  if (query.connectorId) params.connector_id = query.connectorId
  if (query.search) params.search = query.search
  if (query.limit) params.limit = query.limit
  const response = await api.get(`/projects/${projectId}/metrics/hosts`, { params })
  return response.data
}

export const fetchProjectHostHistory = async (
  projectId: string, query: { range: HostsRange; connectorId?: string | null; top?: number },
): Promise<HostsHistoryResponse> => {
  const params: Record<string, string | number> = { range: query.range }
  if (query.connectorId) params.connector_id = query.connectorId
  if (query.top) params.top = query.top
  const response = await api.get(`/projects/${projectId}/metrics/hosts/history`, { params })
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

/** Start the connector's traffic usage over from now; returns the updated connector. */
export const resetConnectorTraffic = async (projectId: string, connectorId: string): Promise<Connector> => {
  const response = await api.post(`/projects/${projectId}/connectors/${connectorId}/traffic/reset`)
  return response.data
}

/** The connector's own metrics history, which survives its proxies being rotated. */
export const fetchConnectorMetricsHistory = async (projectId: string, connectorId: string, range: string): Promise<MetricsHistoryResponse> => {
  const response = await api.get(`/projects/${projectId}/connectors/${connectorId}/metrics/history`, { params: { range } })
  return response.data
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

/** Editors and admins only: viewers get a 403. */
export const clearMitmRequests = async (projectId: string): Promise<void> => {
  await api.delete(`/projects/${projectId}/mitm/requests`)
}

// Failure log types

/** One example: a failure, and every identical one since, whichever gateway saw it. */
export interface FailureRecord {
  /** Kind and fingerprint; the same while the failure recurs. */
  id: string
  /** e.g. upstream_timeout, no_proxy_available */
  kind: string
  /** What the client was answered (a tunnel device's connection is closed instead). */
  status: number
  message: string
  error_type: string | null
  method: string | null
  /** host:port, or the URL without its query for an intercepted request */
  target: string | null
  session_id: string | null
  location: string | null
  peer_id: string | null
  peer_name: string | null
  proxy_id: string | null
  connector_id: string | null
  connector_name: string | null
  /** host:port of the upstream proxy */
  proxy: string | null
  /** The vendor's HTTP status when it refused the request. */
  upstream_status: number | null
  /** What the vendor said about its refusal besides the status: its error headers and body text. */
  upstream_message: string | null
  latency_ms: number | null
  /** The step of the request's latency it failed in. */
  step: LatencyStep | null
  /** Milliseconds each step took, for the first failure of the example. */
  steps: Partial<Record<LatencyStep, number>> | null
  count: number
  first_at: string
  last_at: string
}

export interface FailureLogResponse {
  records: FailureRecord[]
  next_cursor: string | null
  /** Examples the log holds, of the kind asked for or of all kinds. */
  total: number
  /** Where the log stood when read; the head counts the examples changed since. */
  newest: number
  enabled: boolean
  /** Examples kept per kind. */
  max_entries: number
  retention_hours: number
}

export interface FailureLogHead {
  /** Examples new or seen again since the page was read. */
  changed: number
  total: number
}

export interface FailureKindCount {
  kind: string
  count: number
  /** The vendor's statuses within this kind, most first. */
  upstream_statuses: { status: number; count: number }[]
}

export interface FailureSummary {
  range: HistoryRange
  since: string
  bucket_seconds: number
  total: number
  kinds: FailureKindCount[]
  /** connector_id is null for requests refused before a proxy was chosen. */
  connectors: { connector_id: string | null; name: string | null; count: number }[]
  /** One per kind seen, most first, each with every bucket of the window. */
  series: { kind: string; points: { timestamp: string; count: number }[] }[]
  enabled: boolean
}

export const fetchFailures = async (
  projectId: string,
  { kind, count = 200, cursor }: { kind?: string | null; count?: number; cursor?: string | null } = {},
): Promise<FailureLogResponse> => {
  const params: Record<string, string | number> = { count }
  if (kind) params.kind = kind
  if (cursor) params.cursor = cursor
  const response = await api.get(`/projects/${projectId}/failures`, { params })
  return response.data
}

export const fetchFailureHead = async (projectId: string, since: number, kind?: string | null): Promise<FailureLogHead> => {
  const params: Record<string, string | number> = { since }
  if (kind) params.kind = kind
  const response = await api.get(`/projects/${projectId}/failures/head`, { params })
  return response.data
}

export const fetchFailureSummary = async (projectId: string, range: HistoryRange): Promise<FailureSummary> => {
  const response = await api.get(`/projects/${projectId}/failures/summary`, { params: { range } })
  return response.data
}

export const clearFailures = async (projectId: string): Promise<void> => {
  await api.delete(`/projects/${projectId}/failures`)
}

// User types
export interface UserAccount {
  id: string
  username: string
  email: string
  role: UserRole
  is_active: boolean
  has_password: boolean
  sign_in_providers: string[]
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

// What's New (docs/_data/releases.yml, see the Changelog on the docs site)
export interface ReleaseChange {
  text: string
  /** A page on the docs site, such as `connecting#exit-headers`. */
  docs?: string | null
}

export interface ReleaseNote {
  version: string
  /** The first release a summary covers; set on the summaries up to 6.1.4. */
  from?: string | null
  date: string
  title: string
  changes: ReleaseChange[]
}

export interface ReleaseUpdate {
  version: string
  checked_at: string
  /** The releases newer than the running one, newest first. */
  notes: ReleaseNote[]
}

export interface ReleaseInfo {
  current_version: string
  last_seen_version: string | null
  unseen: number
  /** The running version's notes and those before it, newest first. */
  notes: ReleaseNote[]
  /** Admins only, when the daily update check found a newer release. */
  update: ReleaseUpdate | null
}

export const fetchReleases = async (): Promise<ReleaseInfo> => {
  const response = await api.get('/releases')
  return response.data
}

export const markReleasesSeen = async (): Promise<ReleaseInfo> => {
  const response = await api.post('/releases/seen')
  return response.data
}

export const setPasswordWithToken = async (token: string, password: string): Promise<LoginResponse> => {
  const response = await api.post('/auth/set-password', { token, password })
  const data = response.data as LoginResponse
  auth.setToken(data.access_token)
  return data
}

// System statistics (admin only): the install as Postgres and Redis describe it,
// the same whichever api process answers. Nothing in it describes that process.

/** The kinds of work workers run, one job stream each: the controller publishes the periodic ones, and any process asks for a run now. */
export const JOB_KINDS = [
  'health', 'sync', 'scale', 'provision', 'reattribute', 'enrich', 'traffic',
  'metrics_flush', 'compaction', 'snapshot', 'geo_update', 'tunnel_last_seen', 'index_hygiene', 'update_check',
] as const
export type JobKind = (typeof JOB_KINDS)[number]

/** A Redis stream entry ID's two numbers: the milliseconds Redis stamped the entry with, and its sequence within them. */
export interface StreamId {
  ms: number
  seq: number
}

/** A stream position as its numbers ("1760875200000-0"); null for none, or anything that is not an entry ID. */
export function parseStreamId(id: string | null | undefined): StreamId | null {
  const match = id ? /^(\d+)-(\d+)$/.exec(id) : null
  return match ? { ms: Number(match[1]), seq: Number(match[2]) } : null
}

/** Whether position `a` lies before `b` in their stream, by their numbers: entry IDs never compare as strings. */
export function streamIdBefore(a: StreamId, b: StreamId): boolean {
  return a.ms < b.ms || (a.ms === b.ms && a.seq < b.seq)
}

/** The process a snapshot describes: what it runs, since when, and how late its event loop is. */
export interface SystemInstanceRuntime {
  version: string
  /** all, api, gateway, controller or worker. */
  role: string
  instance_id: string
  pid: number
  started_at: string | null
  uptime_seconds: number
  // How late the event loop ran timers over the last full minute, worst and
  // average; null before the first minute is up.
  loop_lag_max_ms: number | null
  loop_lag_mean_ms: number | null
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
  /** Proxies per status, from the column the health jobs and the drains write; a status no proxy holds is absent. */
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
  dead_rows: number
  last_vacuum_at: string | null
}

export interface SystemDatabase {
  name: string
  size_bytes: number
  tables: SystemTableStats[]
  /** Connections to the database from every process and client; each api process's own pool is in its snapshot. */
  backends: number | null
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

export type WorkerState = 'running' | 'done' | 'cancelled' | 'failed'

/** 'instance' runs in every process of its roles; 'singleton' only in the one holding its lease (the controller's loops). */
export type WorkerScope = 'instance' | 'singleton'

export interface SystemWorkerTask {
  name: string
  description: string
  scope: WorkerScope
  /** The lease a singleton runs under ('controller' for the controller's loops); null for every other loop. */
  lease: string | null
  /** State of the asyncio task itself - whether the loop still exists. */
  state: WorkerState
  /** The exception that ended the loop, when state is not 'running'. */
  error: string | null
  /** Cadence the loop was started with; null for a loop that declared none. */
  interval_seconds: number | null
  /** Counters for the cycles inside the loop, since its process started. A standby controller's loops count none. */
  runs: number
  /**
   * Runs that found nothing to do - an empty buffer, no key due this second,
   * a snapshot not due yet. A subset of `runs`, so `runs - idle_runs` is the
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

/** An api process: its Postgres pool. How far it has read the change stream is in its snapshot's `streams.changes`. */
export interface SystemApiSnapshot {
  /** Connections the pool keeps open, in use now, and opened beyond its size; null when the pool does not say. */
  pool_size: number | null
  pool_checked_out: number | null
  pool_overflow: number | null
}

/** A gateway: its listener, the feed it routes on, and its traffic and buffers since it started. */
export interface SystemGatewaySnapshot {
  listener_port: number
  listening: boolean
  /** Client connections being served now. */
  active_connections: number
  /**
   * The routing feed it applied last: the head's digest, how far the change
   * stream had been read by the compile that made it (an entry ID), and when
   * it was applied; null before the first apply.
   */
  feed_digest: string | null
  feed_changes: string | null
  feed_applied_at: string | null
  feed_age_seconds: number | null
  /** What it keeps out of routing on its own: retired session slots, and quarantines it tripped before the feed carried them. */
  retired_slots: number
  local_quarantines: number
  /** Transfers open now. */
  in_flight: number
  /** Requests carried since it started (counted when they end) and their bytes (counted as they flow). */
  requests: number
  successes: number
  failures: number
  bytes_sent: number
  bytes_received: number
  /** Failed and refused requests since start, by failure kind. */
  failures_by_kind: Record<string, number>
  /** Metric entries waiting for the next write to Redis, writes Redis did not take, and rate-limit windows that could not be counted. */
  pending_metric_deltas: number
  metric_flush_failures: number
  rate_limit_errors: number
  /** Echoes preflight sent through an upstream, and verdicts that placed the exit elsewhere, since start. */
  preflight_checks: number
  preflight_rejections: number
  /** IP databases: the ids open, why the others are not, and the ids it still waits for. */
  geo_databases_loaded: string[]
  geo_database_errors: Record<string, string>
  geo_databases_missing: string[]
  /** Buffers on their way to Redis, and what they dropped since start. */
  observations_pending: number
  observations_dropped: number
  sightings_pending: number
  sightings_dropped: number
  failure_log_dropped: number
  mitm_captures_dropped: number
  /** The tunnel endpoints it runs, one per protocol it terminates. */
  tunnels: TunnelEndpointState[]
}

/**
 * A controller: whether it leads, and what it last compiled and published.
 * A standby keeps what it saw while it led, if it ever did.
 */
export interface SystemControllerSnapshot {
  is_leader: boolean
  /** Times this process has taken the lease since it started. */
  lease_terms: number
  /** How far the change stream had been read by its last compile (an entry ID), and when that compile was made. */
  changes: string | null
  compiled_at: string | null
  compiles: number
  compile_ms: number | null
  /** Why the last compile failed, while none has succeeded since. */
  compile_error: string | null
  publishes: number
  published_at: string | null
  publish_ms: number | null
  publish_error: string | null
  /** Sections the feed holds now; sections its publishes wrote (new or changed) and removed since start. */
  sections: number
  sections_written: number
  sections_removed: number
  /** Proxies in quarantine, and connectors whose traffic block still runs, as published. */
  quarantined: number
  blocked_connectors: number
}

/** One job kind on one worker, since the process started. */
export interface SystemJobKindStats {
  /** Batches run, and batches whose run raised: delivered again, and given up after five deliveries. */
  runs: number
  failures: number
  /** Failures in a row; 0 once a batch runs, or a read works again while no batch is failing, so a past outage is not "failing now". */
  consecutive_failures: number
  /** Seconds spent in the job, summed over the batches that ran. */
  duration_seconds: number
  /** Entries it holds now, in the batches it is running or giving up. */
  in_flight: number
}

/** A worker: the job kinds it takes, and the sightings and observations it ingested. */
export interface SystemWorkerSnapshot {
  jobs: Partial<Record<JobKind, SystemJobKindStats>>
  /** Preflight sightings attributed, and lost with a batch given up after five deliveries, since start. */
  sightings_consumed: number
  sightings_dropped: number
  /** IP observations and exit judgements ingested, and lost the same way, since start. */
  observations_consumed: number
  observations_dropped: number
}

/** Why a follower read a stream in full rather than on from its position. */
export type FullReadReason = 'start' | 'gap' | 'reconnect' | 'reconcile'

/** How far one process has read one stream it follows, and how often it read in full instead. */
export interface SystemStreamState {
  /** The last entry ID it handled, or the newest one at its last full read ('0-0' when there was no stream); null before its first read. */
  position: string | null
  /** How far behind the stream's newest entry its last read found it, by the milliseconds both IDs carry: 0 when it found nothing new, and after a full read; null before its first read. */
  lag_seconds: number | null
  /** Entries read since the process started. */
  entries: number
  /**
   * Full reads since the process started, by reason: a gap means entries after
   * its position were lost (it fell behind the stream's cap, or Redis lost the
   * stream), a reconnect that a read failed.
   */
  full_reads: Partial<Record<FullReadReason, number>>
  last_full_read_at: string | null
}

/**
 * What one process publishes about itself on its heartbeat every few
 * seconds: the process, whether it is ready, its loops, the streams it
 * follows, and one section per runtime it runs (a process of role 'all' has
 * all four).
 */
export interface SystemInstanceSnapshot {
  runtime: SystemInstanceRuntime
  /** Whether it answers /ready with 200 now, and when not, why ("gateway: feed not applied yet"). */
  ready: boolean
  reasons: string[]
  tasks: SystemWorkerTask[]
  /**
   * Each stream it follows from a position of its own, by name: `changes`
   * (the api's and workers' definitions, or a controller's compile),
   * `runtime` (a controller), `feed` (a gateway).
   */
  streams: Record<string, SystemStreamState>
  api: SystemApiSnapshot | null
  gateway: SystemGatewaySnapshot | null
  controller: SystemControllerSnapshot | null
  worker: SystemWorkerSnapshot | null
}

/** One live process, listed while the registry key its heartbeat renews lasts. */
export interface SystemInstance {
  instance_id: string
  role: string
  /** Null when it could not build a snapshot, or another release wrote one this release cannot read; it is listed anyway. */
  snapshot: SystemInstanceSnapshot | null
  /** Seconds since the snapshot was published, read off the key's TTL so no two clocks are compared; null without one. */
  age_seconds: number | null
}

/** The active controller: who holds the lease, and how far its last compile read the change stream against the stream's head. */
export interface SystemControllerStatus {
  /** Instance id holding the controller lease; null while none leads (none running, or a failover under way). */
  leader: string | null
  /** How far the leader's last compile read the change stream (an entry ID), and when it was made, from its heartbeat; null until it reports one. */
  changes: string | null
  compiled_at: string | null
  /**
   * The change stream's newest entry ID, read now: ahead of `changes` while
   * the leader has changes to compile, or its heartbeat has not reported the
   * compile yet. Null while there is no stream (nothing announced since Redis
   * started empty), so nothing is behind it.
   */
  changes_head: string | null
  /**
   * How soon a standby takes the lease over, in milliseconds: it asks for the
   * lease every `standby_poll_ms`, and a leader that fails holds it for at
   * most `lease_ttl_ms` more. The values the controllers run with.
   */
  standby_poll_ms: number
  lease_ttl_ms: number
  /** Why the lease and the heartbeats could not be read from Redis. */
  error: string | null
}

/** One gateway's routing feed against the head in Redis. */
export interface SystemGatewayFeed {
  /** Every process with a gateway section (role gateway or all). */
  instance_id: string
  /** The feed it applied last, and how far the change stream had been read by the compile that made it; null before its first apply. */
  digest: string | null
  changes: string | null
  /** Whether it routes on the head now. False after each publish until its next heartbeat reports the apply, or while it cannot apply one. */
  in_sync: boolean
  /** Seconds since it applied a feed update, as of this answer; it grows while nothing changes. */
  age_seconds: number | null
}

/** The routing feed: the head the active controller published, and every gateway against it. */
export interface SystemFeed {
  /** The head (feed:head): its digest, how far its compile read the change stream, when that was, and its sections; all null while there is none. */
  digest: string | null
  changes: string | null
  compiled_at: string | null
  sections: number | null
  gateways: SystemGatewayFeed[]
  /** Redis could not be read, or holds a head this release cannot read. */
  error: string | null
}

/** One work stream and the workers' consumer group on it, as Redis describes it now. */
export interface SystemWorkStream {
  /** Entries on the stream: queued and running, since a worker deletes each entry once it has handled it. */
  length: number
  /** Delivered to a worker and not acknowledged yet: running, or waiting to be reclaimed (a worker stopped, a batch failed, or its acknowledgement was refused). */
  pending: number
  /** Not delivered to any worker yet: the backlog. */
  queued: number
  /** Worker processes in the group, the stopped ones too until index hygiene prunes them (within the hour). */
  consumers: number
  /** Age of the oldest entry, queued or running, by Redis's clock; null when the stream is empty. */
  oldest_seconds: number | null
}

/** Every work stream by name: `jobs.<kind>` for each job kind, then `sightings` and `observations`. */
export interface SystemWorkStreams {
  /** Empty when Redis could not be read (`error` says why). */
  by_name: Record<string, SystemWorkStream>
  error: string | null
}

/** The cluster alone, all from Redis: what the topology view polls, without the stores' heavier reads. */
export interface SystemTopology {
  generated_at: string
  /**
   * Every live process by the role it registered with, in the order all,
   * api, gateway, controller, worker, then any other; each list sorted by
   * instance id. A role with no live process is absent and echo processes do
   * not heartbeat. Empty when Redis cannot be read (controller.error says why).
   */
  instances: Record<string, SystemInstance[]>
  controller: SystemControllerStatus
  feed: SystemFeed
  /** The work the workers take: each job kind's stream, the sightings and the observations. */
  streams: SystemWorkStreams
}

export interface SystemStats extends SystemTopology {
  inventory: SystemInventory
  projects: SystemProjectUsage[]
  database: SystemDatabase
  redis: SystemRedis
}

export const fetchSystemStats = async (): Promise<SystemStats> => {
  const response = await api.get('/system/stats')
  return response.data
}

export const fetchSystemTopology = async (): Promise<SystemTopology> => {
  const response = await api.get('/system/topology')
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


// Tunnels: what WireGuard and OpenVPN share. Gateways terminate them; the api shows them from the gateways' heartbeats.
export type TunnelProtocol = 'wireguard' | 'openvpn'

/**
 * What a gateway's endpoint is doing: bringing the tunnel up (also while it
 * waits for the feed to carry the settings), carrying it, failed (it says
 * why, and tries again), or stopped with the gateway.
 */
export type TunnelEndpointPhase = 'starting' | 'running' | 'failed' | 'stopped'

/** One gateway's endpoint for one tunnel protocol, as its last heartbeat (a few seconds old) reports it. */
export interface TunnelEndpointState {
  protocol: TunnelProtocol
  instance_id: string
  state: TunnelEndpointPhase
  /** Why the tunnel is not up, or what last went wrong applying a change to it (with the daemon's last output for OpenVPN). */
  error: string | null
  /** WireGuard: the kernel module or wireguard-go, once the interface is up. */
  backend: 'kernel' | 'userspace' | null
  /** The port devices reach this gateway on: its own override, else the install's endpoint port; null until the feed carries the settings. */
  listen_port: number | null
  /** Connections the gateway's tunnel listener relays now, every tunnel protocol included (the listener is shared). */
  active_connections: number
  /** OpenVPN: times the daemon exited on its own and was started again, and connections refused at admission, since the gateway started. */
  restarts: number
  denied: number
  daemon_version: string | null
  /** The gateway's own tunnel configuration: the interface it creates, the range its resolver answers from, and whether it closes encrypted DNS. */
  interface: string | null
  fake_ip_range: string | null
  block_encrypted_dns: boolean
}

/** A tunnel protocol across the install: every gateway carrying it, and its devices. */
export interface TunnelStatus {
  /** One entry per live gateway running the protocol's endpoint, by instance id; empty when none does. */
  gateways: TunnelEndpointState[]
  peers_total: number
  peers_enabled: number
  /** Devices a gateway carries now (OpenVPN) or that handshaked recently (WireGuard), merged across gateways. */
  peers_online: number
  /** Connections relayed by address because no destination name could be recovered: every device, all time. */
  connections_by_address: number
  /** Encrypted-DNS connections (DoT, known DoH resolvers) closed so devices keep the tunnel resolver: every device, all time. */
  encrypted_dns_blocked: number
}

// WireGuard: the install's tunnel endpoint and the devices allowed in (see docs/wireguard.md)
export interface WireGuardServerSettings {
  public_key: string
  endpoint_host: string
  endpoint_port: number
  subnet: string
  gateway: string
  persistent_keepalive: number
  client_mtu: number | null
  configured: boolean
  updated_at: string
  /** The tunnel on every gateway carrying it, and the devices. */
  status: TunnelStatus
}

export interface WireGuardServerSettingsDoc {
  endpoint_host: string
  endpoint_port: number
  subnet: string
  persistent_keepalive: number
  client_mtu: number | null
}

export interface WireGuardPeerStatus {
  online: boolean
  last_handshake_at: string | null
  endpoint: string | null
  /** True when a gateway carrying the tunnel published this reading in the last half minute; false when it is the persisted last sighting. */
  live: boolean
  /** The carrying gateway's interface counters since its interface came up (wire bytes, handshakes and DNS included); zero when none is carrying the device. */
  rx_bytes: number
  tx_bytes: number
}

/** A tunnel device's traffic as the pool relayed it: history plus the current window, from every gateway. */
export interface TunnelPeerMetrics {
  request_count: number
  success_count: number
  failure_count: number
  avg_latency_ms: number
  bytes_sent: number
  bytes_received: number
  /** Connections relayed by address because no destination name could be recovered. */
  connections_by_address: number
  /** Encrypted-DNS connections closed so the device keeps the tunnel resolver. */
  encrypted_dns_blocked: number
}

export interface TunnelPeerMetricsSnapshot extends TunnelPeerMetrics {
  timestamp: string
}

export interface TunnelPeerMetricsHistoryResponse {
  snapshots: TunnelPeerMetricsSnapshot[]
}

export interface WireGuardPeer {
  id: string
  project_id: string
  name: string
  public_key: string
  has_private_key: boolean
  has_preshared_key: boolean
  address: string
  enabled: boolean
  session_id: string | null
  country: string | null
  state: string | null
  city: string | null
  created_at: string
  updated_at: string
  status: WireGuardPeerStatus
  metrics: TunnelPeerMetrics
}

export interface WireGuardPeerListResponse {
  total: number
  peers: WireGuardPeer[]
  server_configured: boolean
  server_public_key: string
}

export interface WireGuardPeerCreate {
  name: string
  public_key?: string | null
  preshared?: boolean
  enabled?: boolean
  session_id?: string | null
  country?: string | null
  state?: string | null
  city?: string | null
}

/** Partial update; an empty string clears session_id, country, state or city. */
export interface WireGuardPeerUpdate {
  name?: string
  enabled?: boolean
  session_id?: string
  country?: string
  state?: string
  city?: string
}

export interface WireGuardPeerConfig {
  filename: string
  config: string
  complete: boolean
  server_configured: boolean
}

export const fetchWireGuardSettings = async (): Promise<WireGuardServerSettings> => (await api.get('/wireguard/settings')).data
export const updateWireGuardSettings = async (doc: WireGuardServerSettingsDoc): Promise<WireGuardServerSettings> =>
  (await api.put('/wireguard/settings', doc)).data
export const rotateWireGuardServerKey = async (): Promise<WireGuardServerSettings> => (await api.post('/wireguard/settings/rotate-key')).data

export const fetchWireGuardPeers = async (projectId: string): Promise<WireGuardPeerListResponse> =>
  (await api.get(`/projects/${projectId}/wireguard/peers`)).data
export const createWireGuardPeer = async (projectId: string, data: WireGuardPeerCreate): Promise<WireGuardPeer> =>
  (await api.post(`/projects/${projectId}/wireguard/peers`, data)).data
export const updateWireGuardPeer = async (projectId: string, peerId: string, data: WireGuardPeerUpdate): Promise<WireGuardPeer> =>
  (await api.patch(`/projects/${projectId}/wireguard/peers/${peerId}`, data)).data
export const deleteWireGuardPeer = async (projectId: string, peerId: string): Promise<void> => {
  await api.delete(`/projects/${projectId}/wireguard/peers/${peerId}`)
}
export const rotateWireGuardPeerKeys = async (projectId: string, peerId: string): Promise<WireGuardPeer> =>
  (await api.post(`/projects/${projectId}/wireguard/peers/${peerId}/rotate-keys`)).data
/** The device's config file. It carries the device's private key, so editors and admins only: viewers get a 403. */
export const fetchWireGuardPeerConfig = async (projectId: string, peerId: string): Promise<WireGuardPeerConfig> =>
  (await api.get(`/projects/${projectId}/wireguard/peers/${peerId}/config`)).data
/** The device's own metrics history: same ranges and tiers as project and connector history. */
export const fetchWireGuardPeerMetricsHistory = async (projectId: string, peerId: string, range: string): Promise<TunnelPeerMetricsHistoryResponse> =>
  (await api.get(`/projects/${projectId}/wireguard/peers/${peerId}/metrics/history`, { params: { range } })).data

// OpenVPN: the install's CA and endpoint, and the devices allowed in (see docs/openvpn.md)
export type OpenVpnProtocol = 'udp' | 'tcp'

export interface OpenVpnServerSettings {
  ca_fingerprint: string
  ca_expires_at: string
  endpoint_host: string
  endpoint_port: number
  protocol: OpenVpnProtocol
  subnet: string
  gateway: string
  keepalive_interval: number
  keepalive_timeout: number
  client_mtu: number | null
  configured: boolean
  updated_at: string
  /** The endpoint on every gateway running it, and the devices. */
  status: TunnelStatus
}

export interface OpenVpnServerSettingsDoc {
  endpoint_host: string
  endpoint_port: number
  protocol: OpenVpnProtocol
  subnet: string
  keepalive_interval: number
  keepalive_timeout: number
  client_mtu: number | null
}

export interface OpenVpnPeerStatus {
  online: boolean
  /** When the current session started, while a carrying gateway reports one. */
  connected_since: string | null
  /** The current session's start, or the persisted last connection. */
  last_seen_at: string | null
  endpoint: string | null
  live: boolean
  rx_bytes: number
  tx_bytes: number
}

export interface OpenVpnPeer {
  id: string
  project_id: string
  name: string
  serial: string
  certificate_expires_at: string
  address: string
  enabled: boolean
  session_id: string | null
  country: string | null
  state: string | null
  city: string | null
  created_at: string
  updated_at: string
  status: OpenVpnPeerStatus
  metrics: TunnelPeerMetrics
}

export interface OpenVpnPeerListResponse {
  total: number
  peers: OpenVpnPeer[]
  server_configured: boolean
  ca_fingerprint: string
}

export interface OpenVpnPeerCreate {
  name: string
  enabled?: boolean
  session_id?: string | null
  country?: string | null
  state?: string | null
  city?: string | null
}

export type OpenVpnPeerUpdate = WireGuardPeerUpdate

export interface OpenVpnPeerConfig {
  filename: string
  config: string
  complete: boolean
  server_configured: boolean
}

export const fetchOpenVpnSettings = async (): Promise<OpenVpnServerSettings> => (await api.get('/openvpn/settings')).data
export const updateOpenVpnSettings = async (doc: OpenVpnServerSettingsDoc): Promise<OpenVpnServerSettings> =>
  (await api.put('/openvpn/settings', doc)).data
export const rotateOpenVpnIdentity = async (): Promise<OpenVpnServerSettings> => (await api.post('/openvpn/settings/rotate-identity')).data

export const fetchOpenVpnPeers = async (projectId: string): Promise<OpenVpnPeerListResponse> =>
  (await api.get(`/projects/${projectId}/openvpn/peers`)).data
export const createOpenVpnPeer = async (projectId: string, data: OpenVpnPeerCreate): Promise<OpenVpnPeer> =>
  (await api.post(`/projects/${projectId}/openvpn/peers`, data)).data
export const updateOpenVpnPeer = async (projectId: string, peerId: string, data: OpenVpnPeerUpdate): Promise<OpenVpnPeer> =>
  (await api.patch(`/projects/${projectId}/openvpn/peers/${peerId}`, data)).data
export const deleteOpenVpnPeer = async (projectId: string, peerId: string): Promise<void> => {
  await api.delete(`/projects/${projectId}/openvpn/peers/${peerId}`)
}
export const rotateOpenVpnPeerCertificate = async (projectId: string, peerId: string): Promise<OpenVpnPeer> =>
  (await api.post(`/projects/${projectId}/openvpn/peers/${peerId}/rotate-certificate`)).data
/** The device's profile. It carries the device's private key, so editors and admins only: viewers get a 403. */
export const fetchOpenVpnPeerConfig = async (projectId: string, peerId: string): Promise<OpenVpnPeerConfig> =>
  (await api.get(`/projects/${projectId}/openvpn/peers/${peerId}/config`)).data
export const fetchOpenVpnPeerMetricsHistory = async (projectId: string, peerId: string, range: string): Promise<TunnelPeerMetricsHistoryResponse> =>
  (await api.get(`/projects/${projectId}/openvpn/peers/${peerId}/metrics/history`, { params: { range } })).data

// ---------------------------------------------------------------------------
// Sign-in providers, linked sign-ins and personal access tokens (docs/sso.md)
// ---------------------------------------------------------------------------

export type AuthProviderKind = 'google' | 'microsoft' | 'github'

/** What the login page needs to draw a button. Public. */
export interface PublicSsoProvider {
  kind: AuthProviderKind
  label: string
}

export interface AuthProviderSettings {
  kind: AuthProviderKind
  label: string
  enabled: boolean
  client_id: string
  has_client_secret: boolean
  tenant_id: string
  redirect_uri: string
  configured: boolean
  updated_by: string | null
  updated_at: string
}

/** A null secret keeps the stored one. */
export interface AuthProviderDoc {
  enabled: boolean
  client_id: string
  client_secret: string | null
  tenant_id?: string
}

export interface AuthIdentity {
  id: string
  provider: AuthProviderKind
  label: string
  email: string
  created_at: string
  last_login_at: string | null
}

export interface ApiTokenSummary {
  id: string
  name: string
  token_prefix: string
  created_by: string | null
  expires_at: string | null
  last_used_at: string | null
  created_at: string
  expired: boolean
}

export interface ApiTokenCreate {
  name: string
  expires_in_days: number | null
}

/** The one response that carries the secret. */
export interface ApiTokenCreated {
  token: string
  api_token: ApiTokenSummary
}

export const fetchSsoProviders = async (): Promise<PublicSsoProvider[]> =>
  (await api.get('/auth/sso/providers')).data.providers

/** Where a sign-in button sends the browser: a top-level navigation, not an API call. */
export const ssoStartUrl = (kind: AuthProviderKind): string => `/api/v1/auth/sso/${kind}/start`

/** Trade the one-time code from the callback for the bearer token, and keep it. */
export const exchangeSsoCode = async (code: string): Promise<LoginResponse> => {
  const response = await api.post('/auth/sso/exchange', { code })
  const data = response.data as LoginResponse
  auth.setToken(data.access_token)
  return data
}

export const fetchAuthProviders = async (): Promise<AuthProviderSettings[]> =>
  (await api.get('/sso/providers')).data.providers
export const updateAuthProvider = async (kind: AuthProviderKind, doc: AuthProviderDoc): Promise<AuthProviderSettings> =>
  (await api.put(`/sso/providers/${kind}`, doc)).data

// A user's own tokens and sign-ins live under /me; an admin reaches anyone's by id.
const tokensBase = (userId?: string) => (userId ? `/users/${userId}/tokens` : '/users/me/tokens')
const identitiesBase = (userId?: string) => (userId ? `/users/${userId}/identities` : '/users/me/identities')

export const fetchApiTokens = async (userId?: string): Promise<ApiTokenSummary[]> =>
  (await api.get(tokensBase(userId))).data.tokens
export const createApiToken = async (data: ApiTokenCreate, userId?: string): Promise<ApiTokenCreated> =>
  (await api.post(tokensBase(userId), data)).data
export const deleteApiToken = async (tokenId: string, userId?: string): Promise<void> => {
  await api.delete(`${tokensBase(userId)}/${tokenId}`)
}

export const fetchAuthIdentities = async (userId?: string): Promise<AuthIdentity[]> =>
  (await api.get(identitiesBase(userId))).data.identities
export const deleteAuthIdentity = async (identityId: string, userId?: string): Promise<void> => {
  await api.delete(`${identitiesBase(userId)}/${identityId}`)
}
