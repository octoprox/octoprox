// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

/**
 * Traffic usage of a connector: the bar and badge used in lists, and the
 * inspector section with the period's numbers, a usage chart and the reset.
 */

import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { RotateCcw } from 'lucide-react'
import {
  Connector, MetricsSnapshot, TrafficPeriod, TrafficUsage,
  fetchConnectorMetricsHistory, resetConnectorTraffic,
} from '../api/client'
import { formatBytesDecimal, formatDateTime, formatMoney, parseApiDate } from '../utils/format'
import { useTheme } from '../contexts/ThemeContext'
import { useToast } from '../contexts/ToastContext'
import { Badge, ConfirmDialog, InspectorSection, KeyValue, Segmented } from './ui'

export const PERIOD_LABEL: Record<TrafficPeriod, string> = { day: 'today', week: 'this week', month: 'this month' }
export const ACTION_LABEL = { alert: 'Alert only', block: 'Block new requests', interrupt: 'Block and cut transfers' } as const

/** "in 3 days", "in 4 h", "in 12 min" for a future timestamp, or "now". */
export function timeUntil(value: string | null | undefined): string {
  const d = parseApiDate(value)
  if (!d) return '-'
  const diff = d.getTime() - Date.now()
  if (diff <= 0) return 'now'
  const m = Math.round(diff / 60000)
  if (m < 60) return `in ${m} min`
  const h = Math.round(m / 60)
  if (h < 48) return `in ${h} h`
  return `in ${Math.round(h / 24)} days`
}

type Tone = 'ok' | 'warning' | 'danger'

export function usageTone(usage: TrafficUsage): Tone {
  if (usage.blocked || usage.status === 'exceeded') return 'danger'
  if (usage.status === 'warning') return 'warning'
  return 'ok'
}

const BAR_COLOR: Record<Tone, string> = { ok: 'bg-primary', warning: 'bg-warning', danger: 'bg-danger' }

/** One sentence for a hover title: where the connector stands this period. */
export function describeUsage(usage: TrafficUsage): string {
  const used = formatBytesDecimal(usage.total_bytes)
  const period = PERIOD_LABEL[usage.period]
  const spend = usage.cost != null ? `, ${formatMoney(usage.cost, usage.currency)} at ${usage.price_per_gb} ${usage.currency}/GB` : ''
  if (usage.limit_bytes == null) return `${used} ${period}${spend}. No limit set.`
  const limit = formatBytesDecimal(usage.limit_bytes)
  const pct = usage.percent == null ? '' : ` (${Math.round(usage.percent)}%)`
  const resets = `Resets ${timeUntil(usage.period_end)}.`
  if (usage.blocked) return `Over its ${limit} limit${pct}: takes no new requests until the period resets or the limit is raised. ${resets}${spend}`
  if (usage.status === 'exceeded') return `${used} of ${limit}${pct}: over the limit, alert only, still routing. ${resets}${spend}`
  if (usage.status === 'warning') return `${used} of ${limit}${pct}: approaching the limit. ${resets}${spend}`
  return `${used} of ${limit}${pct} ${period}. ${resets}${spend}`
}

/** Usage bar with the numbers next to it. Without a limit it shows plain usage. */
export function TrafficUsageBar({ usage, compact, className }: { usage: TrafficUsage; compact?: boolean; className?: string }) {
  const tone = usageTone(usage)
  const used = formatBytesDecimal(usage.total_bytes, compact ? 1 : 2)
  if (usage.limit_bytes == null) {
    return (
      <span className={`inline-flex items-center gap-2 ${className ?? ''}`} title={describeUsage(usage)}>
        <span className="tabular-nums font-medium">{used}</span>
        {!compact && <span className="text-fg-subtle text-xs">{PERIOD_LABEL[usage.period]}</span>}
        {usage.cost != null && <span className="text-fg-subtle text-xs tabular-nums">{formatMoney(usage.cost, usage.currency)}</span>}
      </span>
    )
  }
  const pct = Math.min(100, usage.percent ?? 0)
  return (
    <span className={`inline-flex items-center gap-2 min-w-0 ${className ?? ''}`} title={describeUsage(usage)}>
      <span className={`${compact ? 'w-10' : 'w-16'} h-1.5 rounded-full bg-primary-soft overflow-hidden inline-block flex-none`}>
        <span className={`block h-full rounded-full ${BAR_COLOR[tone]}`} style={{ width: `${pct}%` }} />
      </span>
      <span className="tabular-nums font-medium truncate">
        {used}
        <span className="text-fg-subtle font-normal"> / {formatBytesDecimal(usage.limit_bytes, compact ? 0 : 2)}</span>
      </span>
      {!compact && usage.percent != null && <span className={`text-xs tabular-nums ${tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : 'text-fg-subtle'}`}>{Math.round(usage.percent)}%</span>}
    </span>
  )
}

/** "Over limit" or "Near limit"; nothing while usage is fine. */
export function TrafficStatusBadge({ usage }: { usage: TrafficUsage }) {
  if (usage.blocked) return <Badge color="orange" title={describeUsage(usage)}>Over limit</Badge>
  if (usage.status === 'exceeded') return <Badge color="orange" title={describeUsage(usage)}>Over limit · alert only</Badge>
  if (usage.status === 'warning') return <Badge color="yellow" title={describeUsage(usage)}>Near limit</Badge>
  return null
}

type ChartRange = '24h' | '7d' | '30d'
const CHART_RANGES: ChartRange[] = ['24h', '7d', '30d']

function tick(epoch: number, range: ChartRange): string {
  const d = new Date(epoch)
  if (range === '24h') return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function toPoints(snapshots: MetricsSnapshot[]) {
  return snapshots.map((s) => ({
    time: parseApiDate(s.timestamp)?.getTime() ?? 0,
    bytes: s.bytes_sent + s.bytes_received,
    requests: s.request_count,
  }))
}

/** Inspector section: this period's usage, spend, limit, a usage chart and the reset action. */
export function ConnectorTrafficSection({ connector, projectId, canMutate }: { connector: Connector; projectId: string; canMutate: boolean }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const { isDark } = useTheme()
  const [range, setRange] = useState<ChartRange>('7d')
  const [confirmReset, setConfirmReset] = useState(false)
  const usage = connector.traffic_usage

  const { data: history } = useQuery({
    queryKey: ['connector-history', projectId, connector.id, range],
    queryFn: () => fetchConnectorMetricsHistory(projectId, connector.id, range),
    refetchInterval: 60_000,
  })
  const points = useMemo(() => toPoints(history?.snapshots ?? []), [history])
  const rangeBytes = useMemo(() => points.reduce((a, p) => a + p.bytes, 0), [points])

  const resetMutation = useMutation({
    mutationFn: () => resetConnectorTraffic(projectId, connector.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connectors', projectId] })
      queryClient.invalidateQueries({ queryKey: ['traffic-split', projectId] })
      setConfirmReset(false)
      toast.show('Traffic usage reset')
    },
    onError: (e: Error) => { setConfirmReset(false); toast.show(e.message || 'Failed to reset usage', 'error') },
  })

  if (!usage) return null
  const tone = usageTone(usage)
  const gridColor = isDark ? '#374151' : '#e5e7eb'
  const tickColor = '#9ca3af'
  const tooltipStyle = isDark
    ? { backgroundColor: '#1f2937', border: '1px solid #374151', color: '#f3f4f6', borderRadius: 8, fontSize: 12 }
    : { borderRadius: 8, fontSize: 12, border: '1px solid #e5e7eb' }
  const lineColor = tone === 'danger' ? '#dc2626' : tone === 'warning' ? '#d97706' : '#2563eb'
  const period = `${formatDateTime(usage.period_start)} to ${formatDateTime(usage.period_end)}`

  return (
    <InspectorSection
      title={`Traffic ${PERIOD_LABEL[usage.period]}`}
      action={canMutate ? (
        <button type="button" onClick={() => setConfirmReset(true)} className="text-xs text-primary hover:brightness-110 inline-flex items-center gap-1">
          <RotateCcw className="w-3 h-3" /> Reset usage
        </button>
      ) : undefined}
    >
      <div className="flex items-center gap-3 flex-wrap">
        <TrafficUsageBar usage={usage} />
        <TrafficStatusBadge usage={usage} />
      </div>
      <KeyValue label="Used" value={<>{formatBytesDecimal(usage.total_bytes)} <span className="text-fg-subtle font-normal">· {formatBytesDecimal(usage.bytes_sent)} up, {formatBytesDecimal(usage.bytes_received)} down</span></>} />
      {usage.limit_bytes != null && (
        <KeyValue label="Limit" value={<>{formatBytesDecimal(usage.limit_bytes)} <span className="text-fg-subtle font-normal">· {ACTION_LABEL[usage.action].toLowerCase()}</span></>} />
      )}
      {usage.cost != null && (
        <KeyValue label="Spend" value={<>{formatMoney(usage.cost, usage.currency)} <span className="text-fg-subtle font-normal">· {usage.price_per_gb} {usage.currency}/GB, at the current rate</span></>} />
      )}
      <KeyValue label="Period" value={<span title={period}>{formatDateTime(usage.period_start)} <span className="text-fg-subtle font-normal">· resets {timeUntil(usage.period_end)}</span></span>} />
      {usage.reset_at && <KeyValue label="Counting since" value={<span title="Usage was reset by hand inside this period">{formatDateTime(usage.reset_at)}</span>} />}

      <div className="pt-1">
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs text-fg-muted tabular-nums">{formatBytesDecimal(rangeBytes)} over {range}</span>
          <Segmented options={CHART_RANGES.map((r) => ({ value: r, label: r }))} value={range} onChange={setRange} size="sm" />
        </div>
        {points.length === 0 ? (
          <p className="text-fg-subtle text-xs text-center h-[90px] flex items-center justify-center">No traffic in this range</p>
        ) : (
          <ResponsiveContainer width="100%" height={90}>
            <AreaChart data={points} margin={{ top: 4, right: 4, left: -8, bottom: 0 }}>
              <CartesianGrid strokeDasharray="2 4" stroke={gridColor} vertical={false} />
              <XAxis dataKey="time" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={(v: number) => tick(v, range)} tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} minTickGap={40} />
              <YAxis tick={{ fontSize: 10, fill: tickColor }} axisLine={false} tickLine={false} tickFormatter={(v: number) => formatBytesDecimal(v, 0)} width={52} />
              <Tooltip labelFormatter={(v: number) => new Date(v).toLocaleString()} formatter={(v: number) => [formatBytesDecimal(v), 'Traffic']} contentStyle={tooltipStyle} />
              <Area type="monotone" dataKey="bytes" name="Traffic" stroke={lineColor} strokeWidth={2} fill={lineColor} fillOpacity={0.08} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      {confirmReset && (
        <ConfirmDialog
          title="Reset traffic usage?"
          message={<>Usage for <b className="text-fg">{connector.name}</b> starts over from now. History is kept; only the count against the limit restarts. Use this after a vendor top-up or a plan change mid-period.</>}
          confirmLabel="Reset"
          danger={false}
          onCancel={() => setConfirmReset(false)}
          onConfirm={() => resetMutation.mutate()}
          isLoading={resetMutation.isPending}
        />
      )}
    </InspectorSection>
  )
}
