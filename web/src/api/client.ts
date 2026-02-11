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

// Handle 401 responses
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      auth.clearToken()
      // Dispatch custom event for auth state change
      window.dispatchEvent(new CustomEvent('auth:logout'))
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

export interface Proxy {
  id: string
  host: string
  port: number
  protocol: string
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

export interface Source {
  id: string
  name: string
  type: string
  enabled: boolean
  proxy_count: number
  last_refresh: string | null
  refresh_interval_seconds: number
  created_at: string
}

export interface SourceListResponse {
  total: number
  sources: Source[]
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

export const fetchProxies = async (): Promise<ProxyListResponse> => {
  const response = await api.get('/proxies')
  return response.data
}

export const fetchSources = async (): Promise<SourceListResponse> => {
  const response = await api.get('/sources')
  return response.data
}

export const fetchMetrics = async (): Promise<MetricsResponse> => {
  const response = await api.get('/metrics')
  return response.data
}

export const createProxy = async (data: { host: string; port: number; protocol?: string }) => {
  const response = await api.post('/proxies', data)
  return response.data
}

export const deleteProxy = async (id: string) => {
  await api.delete(`/proxies/${id}`)
}

export const setStrategy = async (strategy: string) => {
  const response = await api.post('/proxies/strategy', { strategy })
  return response.data
}

export default api

