// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ColumnDef } from '@tanstack/react-table'
import {
  AreaChart, Area, BarChart, Bar, Cell, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { ChevronDown, ChevronRight, Search, Target } from 'lucide-react'
import {
  fetchProjectHostMetrics, fetchProjectHostHistory,
  HostMetrics, HostsRange, HostsResponse, HostsHistoryResponse,
} from '../api/client'
import { useProject } from '../contexts/ProjectContext'
import { useTheme } from '../contexts/ThemeContext'
import { bytesShort, compact, formatBytes, parseApiDate, plural } from '../utils/format'
import { DataTable } from '../components/DataTable'
import { Page, EmptyState } from '../components/layout/Page'
import { ProviderLogo } from '../components/ProviderLogo'
import { Alert, Card, CardHeader, InfoTip, Input, Segmented, Select } from '../components/ui'

const RANGES: HostsRange[] = ['1h', '6h', '24h', '7d', '30d']
const RANGE_DURATION_MS: Record<HostsRange, number> = {
  '1h': 3_600_000, '6h': 6 * 3_600_000, '24h': 24 * 3_600_000, '7d': 7 * 24 * 3_600_000, '30d': 30 * 24 * 3_600_000,
}
/** How many hosts get a series of their own in the chart; the rest stack as one. */
const TOP_SERIES = 8
/** How many hosts the table loads; the header says when there are more and search reaches them. */
const TABLE_LIMIT = 500

type Metric = 'requests' | 'bytes'
const METRIC_OPTIONS: { value: Metric; label: string }[] = [
  { value: 'requests', label: 'Requests' },
  { value: 'bytes', label: 'Data' },
]

// One hue per series position, fixed so the chart, the bars and the table dots agree.
const SERIES_COLORS = ['#2563eb', '#16a34a', '#ea580c', '#9333ea', '#0d9488', '#db2777', '#ca8a04', '#0ea5e9', '#dc2626', '#4f46e5', '#65a30d', '#f97316']
const OVERFLOW_COLOR = '#94a3b8'

function formatTickByRange(epoch: number, range: HostsRange): string {
  const d = new Date(epoch)
  if (range === '7d' || range === '30d') return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function formatShare(v: number): string {
  if (v === 0) return '0%'
  if (v < 1) return '<1%'
  return `${v < 10 ? v.toFixed(1) : Math.round(v)}%`
}

/** Wait for typing to pause before the value is used (for the server-side host search). */
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(handle)
  }, [value, delayMs])
  return debounced
}

type ChartPoint = { time: number } & Record<string, number>

/**
 * Recharts reads a dataKey as a lodash path, so "shop.example.com" would be
 * looked up as nested fields. Series are keyed by position instead; the host
 * travels as the series name.
 */
const seriesKey = (index: number) => `s${index}`

/** Series by position, one row per bucket, so the areas stack. */
function buildChartData(history: HostsHistoryResponse | undefined, metric: Metric): ChartPoint[] {
  if (!history || history.series.length === 0) return []
  const byTime = new Map<number, ChartPoint>()
  history.series.forEach((series, index) => {
    for (const point of series.points) {
      const time = parseApiDate(point.timestamp)?.getTime() ?? 0
      const row = byTime.get(time) ?? ({ time } as ChartPoint)
      row[seriesKey(index)] = metric === 'requests' ? point.request_count : point.bytes_sent + point.bytes_received
      byTime.set(time, row)
    }
  })
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

export default function HostsPage() {
  const navigate = useNavigate()
  const { selectedProjectId, selectedProject } = useProject()
  const { isDark } = useTheme()
  const [range, setRange] = useState<HostsRange>('24h')
  const [connectorId, setConnectorId] = useState<string>('')
  const [metric, setMetric] = useState<Metric>('requests')
  const [search, setSearch] = useState('')
  const debouncedSearch = useDebounced(search.trim(), 300)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['host-metrics', selectedProjectId, range, connectorId, debouncedSearch],
    queryFn: () => fetchProjectHostMetrics(selectedProjectId!, { range, connectorId: connectorId || null, search: debouncedSearch || undefined, limit: TABLE_LIMIT }),
    enabled: !!selectedProjectId,
    refetchInterval: 30000,
    placeholderData: (previous) => previous,
  })
  const { data: history } = useQuery({
    queryKey: ['host-history', selectedProjectId, range, connectorId],
    queryFn: () => fetchProjectHostHistory(selectedProjectId!, { range, connectorId: connectorId || null, top: TOP_SERIES }),
    enabled: !!selectedProjectId,
    refetchInterval: 60000,
    placeholderData: (previous) => previous,
  })

  const overflow = data?.overflow_host ?? '(other)'
  const hosts = data?.hosts ?? []
  const totals = data?.totals
  const connectors = data?.connectors ?? []
  const connectorName = useMemo(() => new Map(connectors.map((c) => [c.connector_id, c])), [connectors])

  // Colours follow the chart's series order, so a host is the same colour everywhere on the page.
  const colorOf = useMemo(() => {
    const map = new Map<string, string>()
    history?.series.forEach((s, i) => {
      map.set(s.host, s.host === overflow ? OVERFLOW_COLOR : SERIES_COLORS[i % SERIES_COLORS.length])
    })
    return (host: string) => map.get(host) ?? (host === overflow ? OVERFLOW_COLOR : undefined)
  }, [history, overflow])

  const chartData = useMemo(() => buildChartData(history, metric), [history, metric])
  const seriesHosts = useMemo(() => history?.series.map((s) => s.host) ?? [], [history])
  const timeDomain = useMemo(() => { const now = Date.now(); return [now - RANGE_DURATION_MS[range], now] }, [range])

  // The busiest hosts as bars, from the same (search-narrowed) list as the table and
  // the totals, so the three agree; the header names the search when one is on.
  const topBars = useMemo(() => {
    const ranked = [...hosts]
      .map((h) => ({ host: h.host, value: metric === 'requests' ? h.request_count : h.bytes_sent + h.bytes_received, share: metric === 'requests' ? h.request_share : h.bytes_share }))
      .sort((a, b) => b.value - a.value)
    return ranked.slice(0, 10)
  }, [hosts, metric])

  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tickColor = '#9ca3af'
  const tooltipStyle = isDark
    ? { backgroundColor: '#1f2937', border: '1px solid #374151', color: '#f3f4f6', borderRadius: 8, fontSize: 12 }
    : { borderRadius: 8, fontSize: 12, border: '1px solid #e5e7eb' }
  const formatMetric = (v: number) => (metric === 'requests' ? v.toLocaleString() : formatBytes(v))

  const columns = useMemo<ColumnDef<HostMetrics, any>[]>(() => [
    {
      accessorKey: 'host',
      header: 'Host',
      cell: ({ row, getValue }) => {
        const host = getValue<string>()
        const color = colorOf(host)
        const isOverflow = host === overflow
        return (
          <span className="flex items-center gap-2 min-w-0">
            <button
              type="button"
              onClick={() => row.toggleExpanded()}
              className="p-0.5 -m-0.5 rounded text-fg-subtle hover:text-fg flex-none"
              aria-label={row.getIsExpanded() ? 'Hide connectors' : 'Show connectors'}
              title={`${row.original.connectors.length} ${plural(row.original.connectors.length, 'connector', 'connectors')}`}
            >
              {row.getIsExpanded() ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
            </button>
            <span className="w-2 h-2 rounded-full flex-none" style={{ background: color ?? 'transparent', boxShadow: color ? undefined : 'inset 0 0 0 1px rgb(var(--color-line-strong))' }} />
            {isOverflow ? (
              <span className="truncate text-fg-muted italic" title="Hosts beyond the configured ceiling, folded together. Raise or clear metrics.hosts.max_hosts to name them.">
                other hosts
              </span>
            ) : (
              <span className="truncate font-medium" title={host}>{host}</span>
            )}
            {row.original.connectors.length > 1 && (
              <span className="text-[11px] text-fg-subtle flex-none tabular-nums">{row.original.connectors.length} conn.</span>
            )}
          </span>
        )
      },
    },
    { accessorKey: 'request_count', header: 'Requests', size: 100, meta: { align: 'right' as const }, cell: ({ getValue }) => getValue<number>().toLocaleString() },
    {
      id: 'success_rate',
      accessorFn: (row: HostMetrics) => (row.request_count > 0 ? (row.success_count / row.request_count) * 100 : -1),
      header: 'Success',
      size: 90,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => {
        const rate = getValue<number>()
        if (rate < 0) return <span className="text-fg-subtle">-</span>
        return <span className={rate < 90 ? 'text-danger' : ''}>{rate.toFixed(1)}%</span>
      },
    },
    {
      accessorKey: 'failure_count',
      header: 'Failed',
      size: 90,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => {
        const n = getValue<number>()
        return n > 0 ? <span className="text-danger">{n.toLocaleString()}</span> : <span className="text-fg-subtle">0</span>
      },
    },
    { accessorKey: 'bytes_sent', header: 'Sent', size: 100, meta: { align: 'right' as const }, cell: ({ getValue }) => <span className="text-fg-muted">{formatBytes(getValue<number>())}</span> },
    { accessorKey: 'bytes_received', header: 'Received', size: 110, meta: { align: 'right' as const }, cell: ({ getValue }) => <span className="text-fg-muted">{formatBytes(getValue<number>())}</span> },
    {
      id: 'bytes_total',
      accessorFn: (row: HostMetrics) => row.bytes_sent + row.bytes_received,
      header: 'Total',
      size: 110,
      meta: { align: 'right' as const },
      cell: ({ getValue }) => formatBytes(getValue<number>()),
    },
    {
      accessorKey: 'avg_latency_ms',
      header: 'Latency',
      size: 90,
      meta: { align: 'right' as const },
      cell: ({ getValue, row }) => (row.original.request_count > 0 ? `${Math.round(getValue<number>()).toLocaleString()} ms` : '-'),
    },
    {
      id: 'share',
      accessorFn: (row: HostMetrics) => (metric === 'requests' ? row.request_share : row.bytes_share),
      header: () => (
        <span className="inline-flex items-center gap-1">
          Share
          <InfoTip>Of the window's {metric === 'requests' ? 'requests' : 'bytes, both directions'}, across every host that matched the filters.</InfoTip>
        </span>
      ),
      size: 130,
      meta: { align: 'right' as const },
      cell: ({ getValue, row }) => {
        const share = getValue<number>()
        return (
          <span className="inline-flex items-center justify-end gap-2 tabular-nums">
            <span className="w-14 h-1.5 rounded-full bg-primary-soft overflow-hidden inline-block flex-none">
              <span className="block h-full rounded-full" style={{ width: `${Math.min(100, share)}%`, background: colorOf(row.original.host) ?? 'rgb(var(--color-primary))' }} />
            </span>
            {formatShare(share)}
          </span>
        )
      },
    },
  ], [colorOf, metric, overflow])

  const base = `/projects/${selectedProjectId}`
  const nothingYet = !isLoading && data && totals?.host_count === 0 && !debouncedSearch && !connectorId
  const transferred = totals ? totals.bytes_sent + totals.bytes_received : 0
  const failureRate = totals && totals.request_count > 0 ? (totals.failure_count / totals.request_count) * 100 : null

  return (
    <Page
      title="Hosts"
      subtitle={selectedProject ? `Where ${selectedProject.name} sends its traffic, by destination host and connector` : undefined}
      count={totals ? totals.host_count : undefined}
      actions={
        <>
          {connectors.length > 1 && (
            <Select value={connectorId} onChange={(e) => setConnectorId(e.target.value)} className="w-auto py-1.5 text-[13px]" aria-label="Connector">
              <option value="">All connectors</option>
              {connectors.map((c) => (
                <option key={c.connector_id} value={c.connector_id}>{c.name}{c.enabled ? '' : ' (disabled)'}</option>
              ))}
            </Select>
          )}
          <Segmented options={RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} />
        </>
      }
    >
      {data && !data.enabled && (
        <Alert variant="warning">
          Per-host counting is switched off (<code className="font-mono text-xs">metrics.hosts.enabled</code>), so nothing new is recorded here. What is shown is history from before it was turned off.
        </Alert>
      )}
      {isError && (
        <Alert variant="error">Could not load the host metrics{error instanceof Error && error.message ? `: ${error.message}` : ''}.</Alert>
      )}

      {/* KPI row */}
      <Card className="grid grid-cols-2 @lg:grid-cols-3 @4xl:grid-cols-5 gap-px bg-line overflow-hidden [&>*]:bg-surface">
        <Kpi label="Hosts" value={totals ? totals.host_count.toLocaleString() : '-'} sub={`in the last ${range}`} />
        <Kpi label="Requests" value={totals ? compact(totals.request_count) : '-'} sub={totals && totals.request_count > 0 ? `${compact(totals.success_count)} ok` : undefined} />
        <Kpi label="Failure rate" value={failureRate == null ? '-' : `${failureRate.toFixed(1)}%`} sub={totals && totals.failure_count > 0 ? `${compact(totals.failure_count)} failed` : undefined} tone={failureRate != null && failureRate >= 10 ? 'danger' : undefined} />
        <Kpi label="Data transferred" value={totals ? bytesShort(transferred) : '-'} sub={totals ? `${bytesShort(totals.bytes_sent)} sent · ${bytesShort(totals.bytes_received)} received` : undefined} />
        <Kpi label="Avg latency" value={totals && totals.request_count > 0 ? `${Math.round(totals.avg_latency_ms)} ms` : '-'} sub="to the upstream proxy" />
      </Card>

      {nothingYet ? (
        <EmptyState
          icon={<Target />}
          title="No requests recorded yet"
          description={<>Hosts appear here once requests go through the project's proxies. The numbers come from flushed history, so a request shows up within a minute or so of completing.</>}
        />
      ) : (
        <>
          <div className="grid grid-cols-1 @4xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-4 items-stretch">
            {/* Busiest hosts over time, stacked */}
            <Card className="p-4 min-w-0">
              <CardHeader
                title={<>{metric === 'requests' ? 'Requests' : 'Data'} over time <span className="text-fg-subtle font-normal">· top {Math.min(TOP_SERIES, seriesHosts.filter((h) => h !== overflow).length)} hosts</span></>}
                action={<Segmented options={METRIC_OPTIONS} value={metric} onChange={setMetric} size="sm" />}
                className="mb-2"
              />
              {chartData.length === 0 ? (
                <p className="text-fg-subtle text-sm text-center h-[260px] flex items-center justify-center">No data for this time range</p>
              ) : (
                <ResponsiveContainer width="100%" height={260}>
                  <AreaChart data={chartData} margin={{ top: 8, right: 8, left: metric === 'bytes' ? 4 : -12, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
                    <XAxis dataKey="time" type="number" scale="time" domain={timeDomain} tickFormatter={(v: number) => formatTickByRange(v, range)} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} minTickGap={48} />
                    <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} tickFormatter={(v: number) => (metric === 'requests' ? compact(v) : bytesShort(v))} width={metric === 'bytes' ? 64 : undefined} />
                    <Tooltip
                      labelFormatter={(v: number) => new Date(v).toLocaleString()}
                      formatter={(v: number, name: string) => [formatMetric(v), name === overflow ? 'other hosts' : name]}
                      contentStyle={tooltipStyle}
                      itemSorter={(item) => -(item.value as number)}
                    />
                    {seriesHosts.map((host, index) => ({ host, index })).reverse().map(({ host, index }) => (
                      <Area
                        key={host}
                        type="monotone"
                        dataKey={seriesKey(index)}
                        name={host}
                        stackId="hosts"
                        stroke={colorOf(host)}
                        strokeWidth={1.5}
                        fill={colorOf(host)}
                        fillOpacity={host === overflow ? 0.25 : 0.45}
                      />
                    ))}
                  </AreaChart>
                </ResponsiveContainer>
              )}
              <div className="flex items-center flex-wrap gap-x-3 gap-y-1 text-[11px] text-fg-muted mt-2">
                {seriesHosts.map((host) => (
                  <span key={host} className="inline-flex items-center gap-1.5 min-w-0">
                    <span className="inline-block w-2.5 h-2.5 rounded-sm flex-none" style={{ background: colorOf(host) }} />
                    <span className="truncate max-w-[200px]">{host === overflow ? 'other hosts' : host}</span>
                  </span>
                ))}
              </div>
            </Card>

            {/* Top hosts as bars */}
            <Card className="p-4 min-w-0 flex flex-col">
              <CardHeader
                title={<>Top hosts <span className="text-fg-subtle font-normal">· by {metric === 'requests' ? 'requests' : 'data'}</span></>}
                action={debouncedSearch ? <span className="text-xs text-fg-muted">matching "{debouncedSearch}"</span> : undefined}
                className="mb-2"
              />
              {topBars.length === 0 ? (
                <p className="text-fg-subtle text-sm text-center flex-1 min-h-[200px] flex items-center justify-center">No hosts</p>
              ) : (
                <ResponsiveContainer width="100%" height={Math.max(200, topBars.length * 28 + 20)}>
                  <BarChart data={topBars} layout="vertical" margin={{ top: 4, right: 48, left: 4, bottom: 0 }} barCategoryGap={6}>
                    <CartesianGrid strokeDasharray="2 4" stroke={gridColor} horizontal={false} />
                    <XAxis type="number" tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} tickFormatter={(v: number) => (metric === 'requests' ? compact(v) : bytesShort(v))} />
                    <YAxis type="category" dataKey="host" width={150} tick={{ fontSize: 11, fill: isDark ? '#d1d5db' : '#374151' }} axisLine={false} tickLine={false} tickFormatter={(v: string) => (v === overflow ? 'other hosts' : v.length > 24 ? `${v.slice(0, 23)}…` : v)} />
                    <Tooltip
                      cursor={{ fill: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)' }}
                      formatter={(v: number, _name: string, item: { payload?: { share?: number } }) => [`${formatMetric(v)} · ${formatShare(item.payload?.share ?? 0)}`, metric === 'requests' ? 'Requests' : 'Data']}
                      labelFormatter={(v: string) => (v === overflow ? 'other hosts' : v)}
                      contentStyle={tooltipStyle}
                    />
                    <Bar dataKey="value" radius={[0, 4, 4, 0]} label={{ position: 'right', fontSize: 10, fill: tickColor, formatter: (v: number) => (metric === 'requests' ? compact(v) : bytesShort(v)) }}>
                      {topBars.map((b) => <Cell key={b.host} fill={colorOf(b.host) ?? '#2563eb'} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              )}
            </Card>
          </div>

          {/* The table: every host in the window, expandable to its connectors */}
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="relative w-full max-w-xs">
              <Search className="w-3.5 h-3.5 text-fg-subtle absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
              <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search hosts…" className="pl-8 py-1.5 text-[13px]" aria-label="Search hosts" />
            </div>
            <p className="text-xs text-fg-muted">
              {totals && totals.host_count > hosts.length && (
                <>Showing the {hosts.length.toLocaleString()} busiest of {totals.host_count.toLocaleString()} hosts; search to reach the rest. </>
              )}
              A request is one CONNECT tunnel or one plain HTTP exchange; bytes are exact. <a href="https://www.octoprox.com/metrics" target="_blank" rel="noreferrer" className="text-primary hover:brightness-110">How counting works →</a>
            </p>
          </div>
          <DataTable
            columns={columns}
            data={hosts}
            getRowId={(row) => row.host}
            defaultPageSize={25}
            emptyMessage={debouncedSearch ? `No hosts match "${debouncedSearch}" in the last ${range}.` : `No requests in the last ${range}.`}
            renderExpandedRow={(row) => (
              <ConnectorBreakdown
                host={row}
                nameOf={(id) => connectorName.get(id)}
                onOpenConnector={(id) => navigate(`${base}/connectors?open=${id}`)}
              />
            )}
          />
        </>
      )}
    </Page>
  )
}

/** A host's traffic split by the connector that carried it, under its row. */
function ConnectorBreakdown({ host, nameOf, onOpenConnector }: {
  host: HostMetrics
  nameOf: (connectorId: string) => HostsResponse['connectors'][number] | undefined
  onOpenConnector: (connectorId: string) => void
}) {
  const total = host.request_count || 1
  return (
    <div className="bg-surface-raised/40 border-t border-line px-3 py-2 @container">
      <div className="hidden @xl:grid grid-cols-[minmax(0,2fr)_repeat(6,minmax(0,1fr))] gap-x-3 px-1.5 text-[11px] text-fg-subtle mb-1">
        <span>Connector</span>
        <span className="text-right">Requests</span>
        <span className="text-right">Success</span>
        <span className="text-right">Sent</span>
        <span className="text-right">Received</span>
        <span className="text-right">Latency</span>
        <span className="text-right">Of this host</span>
      </div>
      {host.connectors.map((part) => {
        const connector = nameOf(part.connector_id)
        const name = part.name ?? connector?.name
        const rate = part.request_count > 0 ? (part.success_count / part.request_count) * 100 : null
        const share = (part.request_count / total) * 100
        return (
          <button
            key={part.connector_id}
            type="button"
            onClick={() => name && onOpenConnector(part.connector_id)}
            disabled={!name}
            className="w-full grid grid-cols-[minmax(0,1fr)_auto] @xl:grid-cols-[minmax(0,2fr)_repeat(6,minmax(0,1fr))] items-center gap-x-3 gap-y-0.5 py-1.5 @xl:py-0 @xl:h-8 px-1.5 rounded-md text-[12.5px] text-left hover:bg-surface-raised transition-colors disabled:hover:bg-transparent disabled:cursor-default"
          >
            <span className="flex items-center gap-2 min-w-0">
              <ProviderLogo type={part.credential_type ?? connector?.credential_type} className="w-4 h-4 text-[16px] flex-none" />
              {name ? <span className="truncate">{name}</span> : <span className="truncate text-fg-subtle italic">deleted connector</span>}
              {connector && !connector.enabled && <span className="text-fg-subtle text-[11px] flex-none">disabled</span>}
            </span>
            <span className="tabular-nums text-right">{part.request_count.toLocaleString()}</span>
            <span className="col-span-2 @xl:col-span-1 flex @xl:block items-center flex-wrap gap-x-3 text-fg-muted tabular-nums @xl:text-right">
              <span><span className="text-fg-subtle @xl:hidden">Success </span>{rate == null ? '-' : <span className={rate < 90 ? 'text-danger' : ''}>{rate.toFixed(1)}%</span>}</span>
              <span className="@xl:hidden"><span className="text-fg-subtle">Sent </span>{formatBytes(part.bytes_sent)}</span>
              <span className="@xl:hidden"><span className="text-fg-subtle">Received </span>{formatBytes(part.bytes_received)}</span>
              <span className="@xl:hidden"><span className="text-fg-subtle">Latency </span>{part.request_count > 0 ? `${Math.round(part.avg_latency_ms)} ms` : '-'}</span>
              <span className="@xl:hidden"><span className="text-fg-subtle">Of this host </span>{formatShare(share)}</span>
            </span>
            <span className="hidden @xl:block tabular-nums text-right text-fg-muted">{formatBytes(part.bytes_sent)}</span>
            <span className="hidden @xl:block tabular-nums text-right text-fg-muted">{formatBytes(part.bytes_received)}</span>
            <span className="hidden @xl:block tabular-nums text-right text-fg-muted">{part.request_count > 0 ? `${Math.round(part.avg_latency_ms)} ms` : '-'}</span>
            <span className="hidden @xl:inline-flex items-center justify-end gap-2 tabular-nums text-fg-muted">
              <span className="w-10 h-1 rounded-full bg-primary-soft overflow-hidden inline-block flex-none">
                <span className="block h-full rounded-full bg-primary" style={{ width: `${Math.min(100, share)}%` }} />
              </span>
              {formatShare(share)}
            </span>
          </button>
        )
      })}
    </div>
  )
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'danger' }) {
  return (
    <div className="px-3 py-2.5 @lg:px-4 @lg:py-3 flex flex-col gap-1 min-w-0">
      <div className="text-xs text-fg-muted truncate">{label}</div>
      <div className={`text-[17px] leading-6 @lg:text-[21px] @lg:leading-7 font-semibold tabular-nums truncate ${tone === 'danger' ? 'text-danger' : ''}`} title={value}>{value}</div>
      {sub && <div className="text-[11px] text-fg-subtle truncate">{sub}</div>}
    </div>
  )
}
