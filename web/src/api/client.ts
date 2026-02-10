import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  headers: {
    'Content-Type': 'application/json',
  },
})

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

