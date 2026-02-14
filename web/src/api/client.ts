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
}

export interface Proxy {
  id: string
  host: string
  port: number
  protocol: string
  username: string | null
  password: string | null
  connector_id: string
  connector_name: string | null
  status: string
  request_count: number
  success_count: number
  failure_count: number
  success_rate: number
  avg_latency_ms: number
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

// Credential types
export type CredentialType = 'static_proxy_provider' | 'aws' | 'gcp' | 'azure'

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
export interface Connector {
  id: string
  name: string
  credential_id: string
  credential_name: string | null
  credential_type: CredentialType | null
  project_id: string
  config: Record<string, unknown>
  enabled: boolean
  proxy_count: number
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
  enabled?: boolean
}

export interface ConnectorUpdate {
  name?: string
  credential_id?: string
  config?: Record<string, unknown>
  enabled?: boolean
}

export interface ConnectorOptions {
  aws_regions: string[]
  aws_instance_types: string[]
  gcp_regions: string[]
  gcp_machine_types: string[]
  azure_regions: string[]
  azure_vm_sizes: string[]
}

export interface PoolMetrics {
  total_proxies: number
  healthy_proxies: number
  unhealthy_proxies: number
  total_requests: number
  total_successes: number
  total_failures: number
  overall_success_rate: number
  avg_latency_ms: number
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

export const fetchProjectMetrics = async (projectId: string): Promise<MetricsResponse> => {
  const response = await api.get(`/projects/${projectId}/metrics`)
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

export default api

