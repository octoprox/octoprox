// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { Settings, Check, Loader, Power, AlertTriangle } from 'lucide-react'
import {
  fetchProjectMetrics, fetchProjectScalingMetrics, fetchProjectMetricsHistory, fetchProjectConnectors,
  updateProject, MetricsSnapshot, ProjectUpdate,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useAuth } from '../contexts/AuthContext'
import { useTheme } from '../contexts/ThemeContext'
import { useToast } from '../contexts/ToastContext'
import { formatBytes } from '../utils/format'
import { CREDENTIAL_TYPES } from '../utils/credentials'
import { Page } from '../components/layout/Page'
import { Sparkline } from '../components/charts/Sparkline'
import { ProjectPanel } from '../components/ProjectForm'
import { Badge, Button, Card, CardHeader, KeyValue, Segmented } from '../components/ui'

const TIME_RANGES = ['1h', '6h', '24h', '7d', '30d'] as const
type TimeRange = (typeof TIME_RANGES)[number]

// Gap thresholds per range: 2x the expected interval between points.
const GAP_THRESHOLD_MS: Record<TimeRange, number> = {
  '1h': 120_000, '6h': 120_000, '24h': 120_000, '7d': 2 * 3600_000, '30d': 2 * 6 * 3600_000,
}
const RANGE_DURATION_MS: Record<TimeRange, number> = {
  '1h': 3_600_000, '6h': 6 * 3_600_000, '24h': 24 * 3_600_000, '7d': 7 * 24 * 3_600_000, '30d': 30 * 24 * 3_600_000,
}

type ChartPoint = {
  time: number
  request_count: number | null
  success_count: number | null
  failure_count: number | null
  avg_latency_ms: number | null
  bytes_sent: number | null
  bytes_received: number | null
}

function formatTickByRange(epoch: number, range: TimeRange): string {
  const d = new Date(epoch)
  if (range === '7d' || range === '30d') return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

/** Snapshots → chart points, inserting null markers so lines break across gaps. */
function buildChartData(snapshots: MetricsSnapshot[], range: TimeRange): ChartPoint[] {
  if (snapshots.length === 0) return []
  const threshold = GAP_THRESHOLD_MS[range]
  const points: ChartPoint[] = []
  for (let i = 0; i < snapshots.length; i++) {
    const s = snapshots[i]
    const epoch = new Date(s.timestamp).getTime()
    if (i > 0) {
      const prevEpoch = new Date(snapshots[i - 1].timestamp).getTime()
      if (epoch - prevEpoch > threshold) {
        points.push({ time: prevEpoch + 1, request_count: null, success_count: null, failure_count: null, avg_latency_ms: null, bytes_sent: null, bytes_received: null })
      }
    }
    points.push({
      time: epoch,
      request_count: s.request_count,
      success_count: s.success_count,
      failure_count: s.failure_count,
      avg_latency_ms: s.avg_latency_ms,
      bytes_sent: s.bytes_sent,
      bytes_received: s.bytes_received,
    })
  }
  return points
}

function computeTotals(snapshots: MetricsSnapshot[]) {
  let requests = 0, successes = 0, failures = 0, bytesSent = 0, bytesReceived = 0
  for (const s of snapshots) {
    requests += s.request_count
    successes += s.success_count
    failures += s.failure_count
    bytesSent += s.bytes_sent
    bytesReceived += s.bytes_received
  }
  const successRate = requests > 0 ? (successes / requests) * 100 : null
  return { requests, successes, failures, successRate, bytesSent, bytesReceived }
}

/** Down-sample a series to at most n points for sparklines. */
function sample(values: number[], n = 24): number[] {
  if (values.length <= n) return values
  const step = values.length / n
  const out: number[] = []
  for (let i = 0; i < n; i++) {
    const start = Math.floor(i * step), end = Math.floor((i + 1) * step)
    const slice = values.slice(start, Math.max(end, start + 1))
    out.push(slice.reduce((a, b) => a + b, 0) / slice.length)
  }
  return out
}

/** Short byte format for tight tiles and axis ticks: 1 decimal, no trailing zero. */
function bytesShort(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(sizes.length - 1, Math.floor(Math.log(bytes) / Math.log(k)))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 2)}M`
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}K`
  return n.toLocaleString()
}

function formatStrategy(strategy: string): string {
  return strategy.split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

// Series colours are literal so charts read the same on every theme.
const C = { requests: '#2563eb', successes: '#16a34a', failures: '#dc2626', latency: '#2563eb', sent: '#ea580c', received: '#0d9488' }

export default function Overview() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { selectedProjectId, selectedProject, refreshProject } = useProject()
  const { canMutate } = useAuth()
  const { isDark } = useTheme()
  const toast = useToast()
  const [range, setRange] = useState<TimeRange>('24h')
  const [routingOpen, setRoutingOpen] = useState(false)
  const [projectPanelOpen, setProjectPanelOpen] = useState(false)

  const { data: metrics } = useQuery({
    queryKey: ['metrics', selectedProjectId],
    queryFn: () => fetchProjectMetrics(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 10000,
  })
  const { data: scaling } = useQuery({
    queryKey: ['scaling-metrics', selectedProjectId],
    queryFn: () => fetchProjectScalingMetrics(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 10000,
  })
  const { data: history } = useQuery({
    queryKey: ['metrics-history', selectedProjectId, range],
    queryFn: () => fetchProjectMetricsHistory(selectedProjectId!, range),
    enabled: !!selectedProjectId,
    refetchInterval: 60000,
  })
  const { data: connectorsData } = useQuery({
    queryKey: ['connectors', selectedProjectId],
    queryFn: () => fetchProjectConnectors(selectedProjectId!),
    enabled: !!selectedProjectId,
    refetchInterval: 15000,
  })

  const strategyMutation = useMutation({
    mutationFn: (strategy: string) => updateProject(selectedProjectId!, { routing_strategy: strategy }),
    onSuccess: async (_d, strategy) => {
      queryClient.invalidateQueries({ queryKey: ['metrics', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      await refreshProject()
      setRoutingOpen(false)
      toast.show(`Routing strategy set to ${formatStrategy(strategy)}`)
    },
    onError: (e: Error) => toast.show(e.message || 'Failed to change strategy', 'error'),
  })

  const updateMutation = useMutation({
    mutationFn: (data: ProjectUpdate) => updateProject(selectedProjectId!, data),
    onSuccess: async () => {
      queryClient.invalidateQueries({ queryKey: ['metrics', selectedProjectId] })
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      await refreshProject()
      setProjectPanelOpen(false)
      toast.show('Project settings saved')
    },
  })

  const pool = metrics?.pool
  const strategy = metrics?.strategy
  const snapshots = history?.snapshots ?? []
  const totals = useMemo(() => computeTotals(snapshots), [snapshots])
  const chartData = useMemo(() => buildChartData(snapshots, range), [snapshots, range])
  const timeDomain = useMemo(() => { const now = Date.now(); return [now - RANGE_DURATION_MS[range], now] }, [range])
  const sparks = useMemo(() => ({
    requests: sample(snapshots.map((s) => s.request_count)),
    successes: sample(snapshots.map((s) => s.success_count)),
    failures: sample(snapshots.map((s) => s.failure_count)),
    sent: sample(snapshots.map((s) => s.bytes_sent)),
    received: sample(snapshots.map((s) => s.bytes_received)),
    latency: sample(snapshots.map((s) => s.avg_latency_ms)),
  }), [snapshots])

  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tickColor = isDark ? '#9ca3af' : '#9ca3af'
  const tooltipStyle = isDark
    ? { backgroundColor: '#1f2937', border: '1px solid #374151', color: '#f3f4f6', borderRadius: 8, fontSize: 12 }
    : { borderRadius: 8, fontSize: 12, border: '1px solid #e5e7eb' }

  const total = pool?.total_proxies ?? 0
  const healthy = pool?.healthy_proxies ?? 0
  const quarantined = pool?.quarantined_proxies ?? 0
  const unhealthy = pool?.unhealthy_proxies ?? 0
  const other = Math.max(0, total - healthy - quarantined - unhealthy)
  const successRate = totals.successRate ?? pool?.overall_success_rate ?? null

  const connectors = connectorsData?.connectors ?? []
  const base = `/projects/${selectedProjectId}`

  return (
    <Page
      title="Overview"
      subtitle={selectedProject ? `Pool health and traffic for ${selectedProject.name}` : undefined}
      actions={
        <>
          <Segmented options={TIME_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} />
          {canMutate && selectedProject && (
            <Button variant="outline" size="sm" onClick={() => { updateMutation.reset(); setProjectPanelOpen(true) }}>
              <Settings className="w-3.5 h-3.5" /> Edit project
            </Button>
          )}
        </>
      }
      panel={projectPanelOpen && selectedProject ? (
        <ProjectPanel
          project={selectedProject}
          onClose={() => setProjectPanelOpen(false)}
          onSave={(data) => updateMutation.mutate(data)}
          isLoading={updateMutation.isPending}
          error={updateMutation.error?.message}
        />
      ) : null}
    >
      {/* Hero: success rate + pool health */}
      <Card className="grid grid-cols-[240px_minmax(0,1fr)] overflow-hidden">
        <div className="px-5 py-4 border-r border-line flex flex-col justify-center gap-0.5">
          <div className="text-xs text-fg-muted">Success rate · {range}</div>
          <div className="text-[40px] font-semibold leading-[44px] tracking-tight tabular-nums">
            {successRate == null ? '—' : `${successRate.toFixed(1)}%`}
          </div>
          <div className="text-xs text-fg-muted tabular-nums">
            {totals.requests.toLocaleString()} requests in the last {range}
          </div>
        </div>
        <div className="px-5 py-4 flex flex-col justify-center gap-2.5">
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm font-semibold">Pool health</div>
            <div className="text-xs text-fg-muted tabular-nums">
              <b className="text-fg font-semibold">{total}</b> proxies · avg latency <b className="text-fg font-semibold">{Math.round(pool?.avg_latency_ms ?? 0)} ms</b>
              {scaling && <> · <b className="text-fg font-semibold">{scaling.requests_per_minute.toFixed(0)}</b> req/min</>}
            </div>
          </div>
          <div className="flex h-2.5 rounded-md overflow-hidden gap-0.5 bg-surface-raised">
            {total > 0 ? (
              <>
                <div style={{ flex: healthy }} className="bg-success rounded-sm first:rounded-l-md last:rounded-r-md" />
                <div style={{ flex: quarantined }} className="bg-orange-500 rounded-sm" />
                <div style={{ flex: unhealthy }} className="bg-danger rounded-sm" />
                <div style={{ flex: other }} className="bg-fg-subtle/40 rounded-sm last:rounded-r-md" />
              </>
            ) : null}
          </div>
          <div className="flex items-center gap-4 text-[12.5px] text-fg-muted tabular-nums flex-wrap">
            <Legend dot="bg-success" n={healthy} label="healthy" />
            <Legend dot="bg-orange-500" n={quarantined} label="quarantined" />
            <Legend dot="bg-danger" n={unhealthy} label="unhealthy" />
            {other > 0 && <Legend dot="bg-fg-subtle/40" n={other} label="initializing / draining" />}
            <span className="flex-1" />
            {scaling && scaling.draining_instances > 0 && (
              <span className="inline-flex items-center gap-1.5"><Loader className="w-3 h-3 text-orange-500 animate-spin" /> {scaling.draining_instances} draining</span>
            )}
            {scaling && scaling.terminating_instances > 0 && (
              <span className="inline-flex items-center gap-1.5"><Power className="w-3 h-3 text-purple-500" /> {scaling.terminating_instances} terminating</span>
            )}
            <button onClick={() => navigate(`${base}/proxies`)} className="text-primary font-medium hover:brightness-110">View proxies →</button>
          </div>
        </div>
      </Card>

      {/* KPI row */}
      <Card className="grid grid-cols-6 divide-x divide-line overflow-hidden">
        <Kpi label="Requests" value={compact(totals.requests)} spark={sparks.requests} color={C.requests} compact={projectPanelOpen} />
        <Kpi label="Successes" value={compact(totals.successes)} spark={sparks.successes} color={C.successes} compact={projectPanelOpen} />
        <Kpi label="Failures" value={compact(totals.failures)} spark={sparks.failures} color={C.failures} compact={projectPanelOpen} />
        <Kpi label="Bytes sent" value={bytesShort(totals.bytesSent)} spark={sparks.sent} color={C.sent} compact={projectPanelOpen} />
        <Kpi label="Bytes received" value={bytesShort(totals.bytesReceived)} spark={sparks.received} color={C.received} compact={projectPanelOpen} />
        <Kpi label="Requests / min" value={scaling ? scaling.requests_per_minute.toFixed(1) : '—'} sub={scaling ? `${scaling.rate_per_proxy.toFixed(1)} per proxy` : undefined} compact={projectPanelOpen} />
      </Card>

      <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,1fr)] gap-4 items-start">
        {/* Left: charts */}
        <div className="flex flex-col gap-4 min-w-0">
          <Card className="p-4">
            <CardHeader
              title={<>Requests over time <span className="text-fg-subtle font-normal">· {range}</span></>}
              action={<ChartLegend items={[['Requests', C.requests], ['Successes', C.successes], ['Failures', C.failures]]} />}
              className="mb-2"
            />
            {chartData.length === 0 ? (
              <p className="text-fg-subtle text-sm text-center py-16">No data for this time range</p>
            ) : (
              <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={chartData} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
                  <XAxis dataKey="time" type="number" scale="time" domain={timeDomain} tickFormatter={(v: number) => formatTickByRange(v, range)} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} minTickGap={48} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} />
                  <Tooltip labelFormatter={(v: number) => new Date(v).toLocaleString()} contentStyle={tooltipStyle} />
                  <Area connectNulls={false} type="monotone" dataKey="request_count" name="Requests" stroke={C.requests} strokeWidth={2} fill={C.requests} fillOpacity={0.08} />
                  <Area connectNulls={false} type="monotone" dataKey="success_count" name="Successes" stroke={C.successes} strokeWidth={2} fill={C.successes} fillOpacity={0.08} />
                  <Area connectNulls={false} type="monotone" dataKey="failure_count" name="Failures" stroke={C.failures} strokeWidth={2} fill={C.failures} fillOpacity={0.08} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </Card>

          <div className="grid grid-cols-2 gap-4">
            <Card className="p-3.5">
              <CardHeader title={<span className="text-[13px]">Latency</span>} action={<span className="text-xs text-fg-muted">avg {Math.round(pool?.avg_latency_ms ?? 0)} ms</span>} className="mb-1" />
              {chartData.length === 0 ? (
                <p className="text-fg-subtle text-xs text-center py-8">No data</p>
              ) : (
                <ResponsiveContainer width="100%" height={110}>
                  <AreaChart data={chartData} margin={{ top: 6, right: 4, left: -20, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
                    <XAxis dataKey="time" type="number" scale="time" domain={timeDomain} tickFormatter={(v: number) => formatTickByRange(v, range)} tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} minTickGap={48} />
                    <YAxis tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} tickFormatter={(v: number) => `${Math.round(v)}`} />
                    <Tooltip labelFormatter={(v: number) => new Date(v).toLocaleString()} formatter={(v: number) => [`${Math.round(v)} ms`, 'Latency']} contentStyle={tooltipStyle} />
                    <Area connectNulls={false} type="monotone" dataKey="avg_latency_ms" name="Latency" stroke={C.latency} strokeWidth={2} fill={C.latency} fillOpacity={0.08} />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </Card>
            <Card className="p-3.5">
              <CardHeader title={<span className="text-[13px]">Traffic</span>} action={<ChartLegend items={[['Sent', C.sent], ['Received', C.received]]} small />} className="mb-1" />
              {chartData.length === 0 ? (
                <p className="text-fg-subtle text-xs text-center py-8">No data</p>
              ) : (
                <ResponsiveContainer width="100%" height={110}>
                  <AreaChart data={chartData} margin={{ top: 6, right: 4, left: -8, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
                    <XAxis dataKey="time" type="number" scale="time" domain={timeDomain} tickFormatter={(v: number) => formatTickByRange(v, range)} tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} minTickGap={48} />
                    <YAxis tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} tickFormatter={(v: number) => bytesShort(v)} width={56} />
                    <Tooltip labelFormatter={(v: number) => new Date(v).toLocaleString()} formatter={(v: number) => formatBytes(v)} contentStyle={tooltipStyle} />
                    <Area connectNulls={false} type="monotone" dataKey="bytes_sent" name="Sent" stroke={C.sent} strokeWidth={2} fill={C.sent} fillOpacity={0.08} />
                    <Area connectNulls={false} type="monotone" dataKey="bytes_received" name="Received" stroke={C.received} strokeWidth={2} fill={C.received} fillOpacity={0.08} />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </Card>
          </div>
        </div>

        {/* Right: connectors, routing, auto-scaling */}
        <div className="flex flex-col gap-4 min-w-0">
          <Card className="px-4 py-3">
            <CardHeader
              title="Connectors"
              action={<button onClick={() => navigate(`${base}/connectors`)} className="text-xs text-primary hover:brightness-110">All {connectors.length} →</button>}
              className="mb-1"
            />
            {connectors.length === 0 ? (
              <p className="text-xs text-fg-muted py-3">No connectors yet. <button onClick={() => navigate(`${base}/connectors`)} className="text-primary">Add one →</button></p>
            ) : (
              <div className="-mx-1.5">
                {connectors.map((c) => {
                  const ct = CREDENTIAL_TYPES.find((t) => t.value === c.credential_type)
                  const max = getConfiguredProxies(c.config, c.credential_type)
                  return (
                    <button
                      key={c.id}
                      onClick={() => navigate(`${base}/connectors?open=${c.id}`)}
                      className="w-full flex items-center gap-2.5 h-[34px] px-1.5 rounded-md text-[12.5px] hover:bg-surface-raised transition-colors text-left"
                    >
                      {ct && <img src={isDark ? ct.logoDark : ct.logo} alt="" className="w-4 h-4 object-contain flex-none" />}
                      <span className={cnTrunc(!c.enabled)}>{c.name}</span>
                      {c.last_error && <AlertTriangle className="w-3.5 h-3.5 text-danger flex-none" />}
                      <span className="text-fg-muted tabular-nums flex-none">{c.proxy_count}{max != null && <span className="text-fg-subtle">/{max}</span>}</span>
                      <span className={`w-2 h-2 rounded-full flex-none ${!c.enabled ? 'bg-fg-subtle' : c.last_error ? 'bg-danger' : 'bg-success'}`} />
                    </button>
                  )
                })}
              </div>
            )}
          </Card>

          <Card className="p-4">
            <CardHeader
              title="Routing"
              action={canMutate && strategy ? (
                <button onClick={() => setRoutingOpen((o) => !o)} className="text-xs text-primary hover:brightness-110">{routingOpen ? 'Done' : 'Change'}</button>
              ) : undefined}
              className="mb-2.5"
            />
            {!routingOpen ? (
              <div className="flex items-center gap-2.5 flex-wrap">
                <Badge color="blue" className="px-2.5 py-1 text-[13px] inline-flex items-center gap-1">
                  <Check className="w-3 h-3" /> {formatStrategy(strategy?.current_strategy ?? selectedProject?.routing_strategy ?? '')}
                </Badge>
                {strategy && <span className="text-xs text-fg-muted">{strategy.available_strategies.length - 1} other strategies available</span>}
              </div>
            ) : (
              <div>
                <div className="flex flex-wrap gap-1.5">
                  {strategy?.available_strategies.map((s) => {
                    const active = s === strategy.current_strategy
                    return (
                      <button
                        key={s}
                        onClick={() => !active && strategyMutation.mutate(s)}
                        disabled={strategyMutation.isPending}
                        className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[12.5px] font-medium border transition-colors ${active ? 'border-primary bg-primary-soft text-primary-soft-fg' : 'border-line text-fg-muted hover:border-line-strong hover:text-fg'}`}
                      >
                        {active && <Check className="w-3 h-3" />}{formatStrategy(s)}
                      </button>
                    )
                  })}
                </div>
                <p className="text-xs text-fg-muted mt-2">Changes take effect immediately for new requests.</p>
              </div>
            )}
          </Card>

          {scaling && (
            <Card className="px-4 py-3">
              <CardHeader title="Auto-scaling" action={<DemandBadge level={scaling.demand_level} />} className="mb-1" />
              <KeyValue label="Instances" value={<>{scaling.current_instances} <span className="text-fg-subtle font-normal">/ {scaling.max_instances}</span></>} />
              <div className="h-1.5 rounded-full bg-primary-soft overflow-hidden my-2">
                <div className="h-full bg-primary rounded-full" style={{ width: `${scaling.max_instances > 0 ? Math.min(100, (scaling.current_instances / scaling.max_instances) * 100) : 0}%` }} />
              </div>
              <KeyValue label="Requests/min" value={scaling.requests_per_minute.toFixed(1)} />
              <KeyValue label="Rate per proxy" value={scaling.rate_per_proxy.toFixed(1)} />
              <KeyValue label="Draining" value={scaling.draining_instances} />
              <KeyValue label="Terminating" value={scaling.terminating_instances} />
            </Card>
          )}
        </div>
      </div>
    </Page>
  )
}

function cnTrunc(off: boolean) {
  return `flex-1 min-w-0 truncate ${off ? 'text-fg-subtle' : ''}`
}

function getConfiguredProxies(config: Record<string, unknown>, type: string | null): number | null {
  if (!type || type === 'static_proxy_provider') return null
  if (type === 'oxylabs' || type === 'brightdata') return typeof config.num_proxies === 'number' ? config.num_proxies : null
  return typeof config.max_proxies === 'number' ? config.max_proxies : null
}

function Legend({ dot, n, label }: { dot: string; n: number; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`w-2 h-2 rounded-full ${dot}`} />
      <b className="text-fg font-semibold">{n}</b> {label}
    </span>
  )
}

function ChartLegend({ items, small }: { items: [string, string][]; small?: boolean }) {
  return (
    <div className={`flex items-center gap-3 ${small ? 'text-[11px]' : 'text-xs'} text-fg-muted`}>
      {items.map(([label, color]) => (
        <span key={label} className="inline-flex items-center gap-1.5">
          <span className="inline-block w-2.5 h-0.5 rounded" style={{ background: color }} />
          {label}
        </span>
      ))}
    </div>
  )
}

function Kpi({ label, value, sub, spark, color, compact }: { label: string; value: string; sub?: string; spark?: number[]; color?: string; compact?: boolean }) {
  return (
    <div className={`${compact ? 'px-3 py-2.5' : 'px-4 py-3'} flex flex-col gap-1 min-w-0`}>
      <div className="text-xs text-fg-muted truncate">{label}</div>
      <div className="flex items-end justify-between gap-2">
        <div className="min-w-0">
          <div className={`${compact ? 'text-[17px] leading-6' : 'text-[21px] leading-7'} font-semibold tabular-nums truncate`} title={value}>{value}</div>
          {sub && <div className="text-[11px] text-fg-subtle truncate">{sub}</div>}
        </div>
        {!compact && spark && spark.length > 1 && color && <Sparkline values={spark} color={color} width={48} height={24} />}
      </div>
    </div>
  )
}

const DEMAND_COLORS: Record<string, 'green' | 'yellow' | 'red'> = { LOW: 'green', MEDIUM: 'yellow', HIGH: 'red' }
function DemandBadge({ level }: { level: string }) {
  return <Badge color={DEMAND_COLORS[level] ?? 'green'}>{level}</Badge>
}
